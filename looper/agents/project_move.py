"""Explicit, same-filesystem project moves, shared by operator and Manager entry points."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg

from ..db import connection, projects as db_projects
from ..db.tasks import ACTIVE_STATUSES
from . import operator_notes, projects, step_workspaces
from .project_execution import ProjectExecution

logger = logging.getLogger(__name__)
FILE_LIMIT = 10_000
ENTRY_LIMIT = 50_000
CHANGED = "the project or its schedules changed; run the move again to ask for confirmation"


@dataclass(frozen=True)
class ProjectMove:
    old: str
    new: str
    schedules: tuple[dict[str, Any], ...]
    version: str
    files: int
    bytes: int
    truncated: bool
    link_old: bool
    inherited_schedules: tuple[dict[str, Any], ...] = ()


def _under(path: str, root: str) -> Path | None:
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return None


def _destination(project: Mapping[str, Any], data: Mapping[str, Any]) -> tuple[Path, Path]:
    fail = projects.RequestError
    if not project["work_dir"]:
        raise fail(projects.HTTP_400, "the project has no directory to move")
    old = Path(os.path.abspath(project["work_dir"]))
    if not old.is_dir():
        raise fail(projects.HTTP_400, "the project's directory does not exist")
    raw = data.get("to")
    if raw is None:
        if not projects.projects_root():
            raise fail(projects.HTTP_400, "give an absolute to directory or configure projects.root")
        base = Path(projects.projects_root()) / projects.project_directory_name(project["name"], project["id"])
        candidate, suffix = base, 2
        while os.path.lexists(candidate):
            candidate = base.with_name(f"{base.name}-{suffix}")
            suffix += 1
        raw = str(candidate)
    if not isinstance(raw, str) or not raw.strip():
        raise fail(projects.HTTP_400, "to must be an absolute directory")
    new = Path(raw)
    if not new.is_absolute():
        raise fail(projects.HTTP_400, "to must be an absolute directory")
    new = Path(os.path.abspath(new))
    if os.path.lexists(new):
        raise fail(projects.HTTP_409, "to already exists; choose a path that does not exist")
    if new.resolve().is_relative_to(old.resolve()):
        raise fail(projects.HTTP_400, "to must not be inside the current directory")
    if not new.parent.is_dir() or not os.access(new.parent, os.W_OK | os.X_OK):
        raise fail(projects.HTTP_400, "to's parent does not exist or is not writable")
    if old.stat().st_dev != new.parent.stat().st_dev:
        raise fail(projects.HTTP_400, "to is on another filesystem; move the directory by hand (no cross-device copy)")
    # Renaming a symlink would move the link rather than the directory and its files.
    if old.is_symlink():
        raise fail(projects.HTTP_400, "the project's directory is a symbolic link; use its actual directory first")
    return old, new


def _inventory(old: Path) -> tuple[int, int, bool]:
    """Count files without following links; bound directory-only trees as well as file-heavy trees."""
    files = size = entries = 0
    pending = [old]
    while pending:
        with os.scandir(pending.pop()) as scan:
            for entry in scan:
                entries += 1
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                else:
                    files += 1
                    size += entry.stat(follow_symlinks=False).st_size
                if files > FILE_LIMIT or entries >= ENTRY_LIMIT:
                    return files, size, True
    return files, size, False


async def _check_directory_dependencies(db: Any, project_id: str, old: Path) -> None:
    # A project's default is not the only dependency: stopped tasks may own checkouts
    # elsewhere while their cleanup still needs the repository being moved. This
    # project's task, step and template paths also stay unchanged by a move.
    rows = await db.fetch(
        "SELECT work_dir AS path FROM projects WHERE id <> $1 UNION "
        "SELECT unnest(ARRAY[work_dir, workspace_dir]) FROM tasks UNION "
        "SELECT unnest(ARRAY[w.repo_dir, w.path]) FROM task_worktrees w JOIN tasks t ON t.id = w.task_id "
        "WHERE t.project_id IS DISTINCT FROM $1 AND w.state = 'active' UNION "
        "SELECT unnest(ARRAY[w.repo_dir, w.path]) FROM step_workspaces w JOIN tasks t ON t.id = w.task_id "
        "WHERE t.project_id = $1 OR w.state NOT IN ('merged', 'discarded') UNION "
        "SELECT work_dir FROM schedules WHERE project_id IS DISTINCT FROM $1 UNION "
        "SELECT work_dir FROM manager_task_templates", project_id)
    if any(_under(row["path"], str(old)) is not None for row in rows):
        raise projects.RequestError(projects.HTTP_409,
                                    "a task, workspace, template or another project depends on this directory; "
                                    "keep the old path link or "
                                    "relocate those dependencies before moving")


async def preview(project: Mapping[str, Any], data: Mapping[str, Any], *, db: Any = None,
                  inventory: tuple[int, int, bool] | None = None) -> ProjectMove:
    from .tiers.planner import _IN_FLIGHT

    try:
        old, new = _destination(project, data)
    except OSError as exc:
        raise projects.RequestError(projects.HTTP_400, f"cannot move project directory: {exc}") from None
    db = db or connection.get_pool()
    busy = await db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM tasks WHERE project_id = $1 AND status = ANY($3::text[])) "
        "OR EXISTS (SELECT 1 FROM tasks t JOIN task_steps s ON s.task_id = t.id "
        "WHERE t.project_id = $1 AND s.status = ANY($2::text[])) "
        "OR EXISTS (SELECT 1 FROM tasks t JOIN llm_records r ON r.task_id = t.id "
        "WHERE t.project_id = $1 AND r.status = 'running') "
        "OR EXISTS (SELECT 1 FROM schedules s JOIN schedule_runs r ON r.schedule_id = s.id "
        "WHERE s.project_id = $1 AND (r.status = 'gating' OR (s.kind = 'command' AND r.status = 'running'))) "
        "OR EXISTS (SELECT 1 FROM tasks t JOIN task_user_gates g ON g.task_id = t.id "
        "WHERE t.project_id = $1 AND g.state IN ('open', 'discussing'))",
        project["id"], list(_IN_FLIGHT), list(ACTIVE_STATUSES))
    if busy:
        raise projects.RequestError(projects.HTTP_409,
                                    "the project has a task, command schedule run or gate in progress; wait for it to finish")
    link_old = data.get("link_old", True)
    if not isinstance(link_old, bool):
        raise projects.RequestError(projects.HTTP_400, "link_old must be a boolean")
    if not link_old:
        await _check_directory_dependencies(db, project["id"], old)
    rows = await db.fetch(
        "SELECT s.*, COALESCE(t.work_dir, '') AS template_work_dir FROM schedules s "
        "LEFT JOIN manager_task_templates t ON t.manager_id = s.manager_id AND t.key = s.template_key "
        "WHERE s.project_id = $1 ORDER BY s.id", project["id"])
    schedules = tuple(dict(row) for row in rows if _under(row["work_dir"], str(old)) is not None)
    inherited = tuple(dict(row) for row in rows if not row["work_dir"] and not row["template_work_dir"])
    # Include every schedule (including inherited directories), and the source inode: execution
    # revalidates the exact project/directory/configuration shown at confirmation time.
    version_data = {"project": {key: project[key] for key in ('id', 'name', 'work_dir', 'updated_at')},
                    "old": str(old), "new": str(new), "link_old": link_old,
                    "source": (old.stat().st_dev, old.stat().st_ino), "schedules": [dict(row) for row in rows]}
    version = hashlib.sha256(json.dumps(version_data, sort_keys=True, default=str).encode()).hexdigest()
    try:
        files, size, truncated = inventory if inventory is not None else await asyncio.to_thread(_inventory, old)
    except OSError as exc:
        raise projects.RequestError(projects.HTTP_400, f"cannot inspect project directory: {exc}") from None
    return ProjectMove(str(old), str(new), schedules, version, files, size, truncated, link_old, inherited)


def impact(project: Mapping[str, Any], move: ProjectMove) -> str:
    count = (f"超过 {FILE_LIMIT} 个文件（more than {FILE_LIMIT} files）" if move.files > FILE_LIMIT else
             f"至少 {move.files} 个文件（扫描达到上限）" if move.truncated else f"{move.files} 个文件")
    size = ("至少 " if move.truncated else "") + f"{move.bytes} 字节"
    lines = [f"要移动项目「{project['name']}」的目录和全部文件吗？", f"- 目录：{move.old} → {move.new}",
             f"- 文件：{count}，总大小：{size}", "以下定时任务的目录会随之移动："]
    for row in move.schedules:
        relative = _under(row["work_dir"], move.old)
        kind = "命令" if row["kind"] == "command" else "Agent"
        lines.append(f"- {kind}定时任务「{row['name']}」：{row['work_dir']} → {Path(move.new) / relative}")
    for row in move.inherited_schedules:
        kind = "命令" if row["kind"] == "command" else "Agent"
        lines.append(f"- {kind}定时任务「{row['name']}」（继承项目目录）：{move.old} → {move.new}")
    if not move.schedules and not move.inherited_schedules:
        lines.append("- 没有定时任务使用此目录。")
    lines.append("旧路径会保留为指向新目录的符号链接。" if move.link_old else
                 "不保留旧路径的符号链接，仍引用旧路径的任务、脚本和作业需要自行更新。")
    lines.append("不会中断任何运行；有任务、命令或 gate 正在进行时会拒绝移动。")
    return "\n".join(lines)


def _repositories(root: Path) -> list[Path]:
    def failed(exc: OSError) -> None:
        raise exc

    found = []
    for directory, dirs, files in os.walk(root, onerror=failed):
        if ".git" in dirs:
            found.append(Path(directory))
        elif ".git" in files:
            with (Path(directory) / ".git").open() as stream:
                git_dir = Path(stream.readline(4096).strip().removeprefix("gitdir: "))
            # A submodule's .git/modules/<name> is not a linked worktree.
            # Running repair on it would also rewrite its relative .git pointer.
            if git_dir.parent.name == "worktrees":
                found.append(Path(directory))
        if ".git" in dirs:
            dirs.remove(".git")
    # Repair common repositories before linked checkouts whose .git files may still name the old path.
    return sorted(found, key=lambda path: (not (path / ".git").is_dir(), str(path)))


async def _repair(move: ProjectMove, worktrees: list[Any], source: str,
                  repositories: list[Path] | OSError) -> list[dict[str, Any]]:
    repairs = []
    if isinstance(repositories, OSError):
        logger.warning("cannot discover moved repositories in %s: %s", move.new, repositories)
        return [{"repository": move.new, "ok": False, "output": str(repositories)}]
    repositories = [Path(move.new) / repo.relative_to(move.old) for repo in repositories]
    checkouts = [*repositories, *(Path(row["path"]) for row in worktrees)]
    for repo in repositories:
        paths = []
        # Both ends of a link may have moved together, including worktrees Looper did not create.
        # Git cannot discover a checkout at its new path from its old registration alone.
        if (repo / ".git").is_dir():
            for checkout in checkouts:
                marker = checkout / ".git"
                if not marker.is_file():
                    continue
                try:
                    with marker.open() as stream:
                        pointer = stream.readline(4096).strip().removeprefix("gitdir: ")
                    git_dir = Path(pointer)
                    if not git_dir.is_absolute():
                        git_dir = checkout / git_dir
                    relative = _under(str(git_dir), source)
                    if relative is not None:
                        git_dir = Path(move.new) / relative
                    if git_dir.resolve().is_relative_to((repo / ".git" / "worktrees").resolve()):
                        paths.append(str(checkout))
                except (OSError, UnicodeError) as exc:
                    logger.warning("cannot inspect moved worktree %s: %s", checkout, exc)
        try:
            run = await step_workspaces._git(repo, "worktree", "repair", *sorted(set(paths)))
            output = (run.stdout + run.stderr).decode("utf-8", "replace").strip()
            result = {"repository": str(repo), "ok": True, "output": output}
            logger.info("repaired moved repository %s: %s", repo, output)
        except (step_workspaces.GitError, OSError) as exc:
            result = {"repository": str(repo), "ok": False, "output": str(exc)}
            logger.warning("cannot repair moved repository %s: %s", repo, exc)
        repairs.append(result)
    return repairs


def _move_notes(root: Path, old_key: str, new: str) -> dict[str, Any]:
    try:
        new_key = operator_notes.notes_key(new)
        old_name, new_name = operator_notes.directory_name(old_key), operator_notes.directory_name(new_key)
        result = {"moved": False, "from": old_name, "to": new_name}
        if old_key == new_key:
            return {**result, "reason": "notes key is unchanged"}
        if not (root / old_name).is_dir():
            return {**result, "reason": "source notes do not exist"}
        if os.path.lexists(root / new_name):
            return {**result, "reason": f"{new_name} already exists"}
        os.rename(root / old_name, root / new_name)
        return {**result, "moved": True}
    except Exception as exc:
        logger.warning("cannot move operator notes for %s: %s", new, exc)
        return {"moved": False, "reason": str(exc)}


async def move(project_id: str, data: Mapping[str, Any], *, expected_move_version: str | None = None,
               manager_id: str | None = None) -> dict[str, Any]:
    from .scheduler import quiesce_project

    lock = projects._project_delete_locks.setdefault(project_id, asyncio.Lock())
    if lock.locked():
        raise projects.RequestError(projects.HTTP_409, "the project is already being moved or deleted")
    async with lock, quiesce_project(project_id):
        renamed = linked = False
        change = None
        try:
            project = await db_projects.get_project(project_id)
            if project is None:
                raise projects.RequestError(projects.HTTP_404, "project not found")
            try:
                scanned = await preview(project, data)
            except projects.RequestError as exc:
                if expected_move_version is not None and exc.status == projects.HTTP_400:
                    raise projects.RequestError(projects.HTTP_409, CHANGED) from None
                raise
            if expected_move_version is not None and scanned.version != expected_move_version:
                raise projects.RequestError(projects.HTTP_409, CHANGED)
            try:
                repositories = await asyncio.to_thread(_repositories, Path(scanned.old))
            except OSError as exc:
                repositories = exc
            # Git key discovery can spawn a subprocess; keep it outside the table locks too.
            old_key = None
            notes = {"moved": False, "reason": "operator notes are off"}
            if ProjectExecution.from_row(project).notes_on:
                try:
                    root = operator_notes.notes_root()
                    notes = {"moved": False, "reason": "notes root does not exist"}
                    if root.is_dir():
                        old_key = await asyncio.to_thread(operator_notes.notes_key, scanned.old)
                except Exception as exc:
                    notes = {"moved": False, "reason": str(exc)}
            async with connection.transaction() as conn:
                # Existing waiting Planners can wake without Router admission. Fence their database
                # writes as well as new starts, until paths and rows agree. Acquire before project
                # rows, since a task insert can hold its table lock while checking the project FK.
                await conn.execute("SET LOCAL lock_timeout = '5s'")
                await conn.execute("LOCK TABLE tasks, task_steps, llm_records, schedule_runs, task_user_gates, "
                                   "projects, schedules, task_worktrees, step_workspaces, manager_task_templates "
                                   "IN SHARE ROW EXCLUSIVE MODE")
                row = await conn.fetchrow("SELECT * FROM projects WHERE id = $1 FOR UPDATE", project_id)
                if row is None:
                    raise projects.RequestError(projects.HTTP_404, "project not found")
                if manager_id is not None and await db_projects.used_outside_manager(project_id, manager_id, pool=conn):
                    raise projects.RequestError(projects.HTTP_409, "the project is used outside this Manager")
                await conn.fetch("SELECT id FROM schedules WHERE project_id = $1 ORDER BY id FOR UPDATE", project_id)
                # Use the counted view here too so preview and execution hash the same fields.
                project = await db_projects.get_project(project_id, pool=conn)
                try:
                    change = await preview(project, {**data, "to": scanned.new}, db=conn,
                                           inventory=(scanned.files, scanned.bytes, scanned.truncated))
                except projects.RequestError as exc:
                    if expected_move_version is not None and exc.status == projects.HTTP_400:
                        raise projects.RequestError(projects.HTTP_409, CHANGED) from None
                    raise
                if change.version != scanned.version:
                    raise projects.RequestError(projects.HTTP_409, CHANGED)
                task_worktrees = await conn.fetch(
                    "SELECT w.task_id, w.repo_dir, w.path FROM task_worktrees w JOIN tasks t ON t.id = w.task_id "
                    "WHERE t.project_id = $1 AND w.state = 'active'", project_id)
                worktrees = await conn.fetch(
                    "SELECT w.repo_dir, w.path FROM task_worktrees w JOIN tasks t ON t.id = w.task_id "
                    "WHERE t.project_id = $1 AND w.state = 'active' UNION "
                    "SELECT w.repo_dir, w.path FROM step_workspaces w JOIN tasks t ON t.id = w.task_id "
                    "WHERE t.project_id = $1 AND w.state NOT IN ('merged', 'discarded')", project_id)
                # Capture canonical ancestry while the old directory and its symlinks still exist.
                # Resolving after rename loses aliases (e.g. /tmp versus /private/tmp).
                source = str(Path(change.old).resolve())

                def moved(path: str) -> str:
                    relative = _under(path, source)
                    return str(Path(change.new) / relative) if relative is not None else path

                task_worktrees = [{**dict(row), "repo_dir": moved(row["repo_dir"]), "path": moved(row["path"])}
                                  for row in task_worktrees]
                worktrees = [{"repo_dir": moved(row["repo_dir"]), "path": moved(row["path"])} for row in worktrees]
                schedule_paths = {row["id"]: moved(row["work_dir"]) for row in change.schedules}
                # Database checks above yield; a destination may appear while they run.
                if os.path.lexists(change.new):
                    raise projects.RequestError(projects.HTTP_409,
                                                "to already exists; choose a path that does not exist")
                os.rename(change.old, change.new)
                renamed = True
                if change.link_old:
                    os.symlink(change.new, change.old, target_is_directory=True)
                    linked = True
                if not await db_projects.update_project(project_id, work_dir=change.new, pool=conn):
                    raise RuntimeError("the project directory could not be updated")
                # These are live cleanup paths, unlike the task's historical directories.
                # A stale repo_dir makes deletion skip committing the checkout's pending files.
                for worktree in task_worktrees:
                    await conn.execute("UPDATE task_worktrees SET repo_dir = $2, path = $3 WHERE task_id = $1",
                                       worktree["task_id"], worktree["repo_dir"], worktree["path"])
                for schedule_id, new_dir in schedule_paths.items():
                    await conn.execute("UPDATE schedules SET work_dir = $2, updated_at = now() WHERE id = $1",
                                       schedule_id, new_dir)
                activity_seq = await db_projects.record_directory_move(project_id, change.old, change.new, pool=conn)
                updated = projects.project_view(await db_projects.get_project(project_id, pool=conn))
        except BaseException as exc:
            # Include commit errors and cancellation: a rolled-back row must never point at moved files.
            if renamed and change is not None:
                if linked:
                    Path(change.old).unlink()
                os.rename(change.new, change.old)
            if isinstance(exc, OSError):
                raise projects.RequestError(projects.HTTP_400, f"cannot move project directory: {exc}") from None
            if isinstance(exc, (asyncpg.LockNotAvailableError, asyncpg.DeadlockDetectedError)):
                raise projects.RequestError(projects.HTTP_409, "project execution state is changing; retry the move") from None
            raise
        repairs = await _repair(change, worktrees, source, repositories)
        if old_key is not None:
            notes = await asyncio.to_thread(_move_notes, root, old_key, change.new)
        note = (f"notes moved: {notes['from']} → {notes['to']}" if notes["moved"] else
                f"notes not moved: {notes['reason']}")
        try:
            await connection.get_pool().execute("UPDATE project_activity SET summary = summary || $2 WHERE seq = $1",
                                                activity_seq, f"; {note}")
        except Exception:
            logger.warning("cannot record notes outcome for project %s: %s", project_id, note, exc_info=True)
        logger.info("project %s directory moved from %s to %s (link_old=%s)",
                    project_id, change.old, change.new, change.link_old)
        return {"project": updated, "old_dir": change.old, "new_dir": change.new,
                "link_old": change.link_old, "repairs": repairs, "notes": notes}
