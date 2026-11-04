"""Directory moves preserve project files, references and git worktrees on PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path

import pytest

from looper.agents import projects as api
from looper.db import projects, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _project(pool, old, *, name="Move example"):
    old.mkdir()
    (old / "subdir").mkdir()
    (old / "subdir" / "kept.txt").write_text("keep these files\n")
    return await projects.create_project(name=name, work_dir=str(old), pool=pool)


async def _schedule(pool, project_id, work_dir, *, schedule_id="command", kind="command"):
    await pool.execute(
        "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, work_dir, kind, command) "
        "VALUES ($1,$2,$1,'0 * * * *','UTC','Do the scheduled work',$3,$4,$5)",
        schedule_id, project_id, str(work_dir), kind, "true" if kind == "command" else None,
    )


async def _rows(pool):
    """Compare whole rows, so a refusal cannot silently update unrelated state."""
    return {
        table: [dict(row) for row in await pool.fetch(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("projects", "schedules", "tasks", "task_worktrees", "task_steps", "llm_records", "schedule_runs",
                      "task_user_gates", "project_activity")
    }


@pytest.mark.parametrize("existing_empty", [False, True], ids=["new-path", "empty-directory"])
def test_move_preserves_files_rewrites_schedules_and_keeps_task_records(monkeypatch, tmp_path, existing_empty):
    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        if existing_empty:
            new.mkdir()
        await _schedule(pool, project["id"], old)
        await _schedule(pool, project["id"], old / "subdir", schedule_id="agent", kind="agent")
        await _schedule(pool, project["id"], str(old) + "-other", schedule_id="outside")
        await _schedule(pool, project["id"], "", schedule_id="default", kind="agent")
        await _schedule(pool, project["id"], "", schedule_id="inherited-command")
        other = await projects.create_project(name="Other", work_dir=str(old), pool=pool)
        await _schedule(pool, other["id"], old, schedule_id="other-project")
        for task_id, status in (("queued", "pending"), ("waiting", "waiting"), ("finished", "completed")):
            await tasks.create_task(task_id=task_id, requirement="Keep the recorded paths", project_id=project["id"],
                                    work_dir=str(old / "subdir"), workspace_dir=str(old), status=status, pool=pool)
        await pool.execute("INSERT INTO task_steps (id,task_id,step_index,instruction,status,kind) "
                           "VALUES ('wait-step','waiting',0,'Wait for input','pending','wait')")
        await pool.execute("INSERT INTO task_waits (step_id,task_id,topic,spec,state) "
                           "VALUES ('wait-step','waiting','Waiting','{}','waiting')")
        recorded_tasks = [dict(row) for row in await pool.fetch("SELECT * FROM tasks ORDER BY id")]
        preview = await api.preview_project_move(project, {"to": str(new)}, db=pool)
        impact = api.project_move_impact(project, preview)
        assert "default" in impact and "inherited-command" in impact
        result = await api.move_project_dir(project["id"], {"to": str(new)})

        assert result["project"]["work_dir"] == str(new)
        assert (result["old_dir"], result["new_dir"], result["link_old"]) == (str(old), str(new), True)
        assert old.is_symlink() and old.resolve() == new.resolve()
        assert (new / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)
        paths = {row["id"]: row["work_dir"] for row in await pool.fetch("SELECT id,work_dir FROM schedules")}
        assert paths == {"command": str(new), "agent": str(new / "subdir"), "outside": str(old) + "-other",
                         "default": "", "inherited-command": "", "other-project": str(old)}
        assert [dict(row) for row in await pool.fetch("SELECT * FROM tasks ORDER BY id")] == recorded_tasks
        activity = await pool.fetch("SELECT kind,summary FROM project_activity WHERE project_id=$1", project["id"])
        assert [dict(row) for row in activity] == [
            {"kind": "directory_moved", "summary": f"directory moved from {old} to {new}"}]

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize(("name", "directory"), [
    (" ..Hello / WORLD---v1.. ", "hello-world-v1"), ("Keep_This.v1", "keep_this.v1"), ("中文项目", None),
])
def test_move_without_destination_uses_the_creation_directory_name(monkeypatch, tmp_path, name, directory):
    old, root = tmp_path / "old", tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(api, "_root", str(root))

    async def check(pool):
        project = await _project(pool, old, name=name)
        result = await api.move_project_dir(project["id"], {})
        expected = root / (directory or project["id"])
        assert result["new_dir"] == str(expected)
        assert expected.is_dir() and (expected / "subdir" / "kept.txt").is_file()
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(expected)

    run_db(check, monkeypatch, timeout=30)


def test_move_can_omit_old_path_link(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        result = await api.move_project_dir(project["id"], {"to": str(new), "link_old": False})
        assert result["link_old"] is False
        assert not os.path.lexists(old)
        assert (new / "subdir" / "kept.txt").is_file()
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize(("case", "message"), [
    ("no-directory", "directory"), ("same", "same|current|unchanged"),
    ("relative", "absolute"), ("inside", "inside|within"),
    ("occupied", "empty|exists"), ("file", "empty|directory|exists"),
    ("parent-missing", "parent|exist"), ("parent-readonly", "writ"),
    ("cross-device", "filesystem|file system|device"),
    ("step-running", "progress|running|active|step"), ("run-open", "progress|running|active|run"),
    ("step-deciding", "progress|running|active|step"), ("step-review", "progress|running|active|step"),
    ("command-running", "progress|running|active|schedule"), ("schedule-gating", "gate|gating|progress"),
    ("gate-open", "gate|progress"), ("gate-discussing", "gate|progress"),
    ("no-root", "root|destination|to"),
])
def test_move_refusals_preserve_files_and_rows(monkeypatch, tmp_path, case, message):
    old, new = tmp_path / "old", tmp_path / "new"
    monkeypatch.setattr(api, "_root", "")

    async def check(pool):
        project = await _project(pool, old)
        await _schedule(pool, project["id"], old)
        data = {"to": str(new)}
        if case == "no-directory":
            await projects.update_project(project["id"], work_dir="", pool=pool)
            data["to"] = "relative-destination"
        elif case == "same":
            data["to"] = str(old)
        elif case == "relative":
            data["to"] = "relative-destination"
        elif case == "inside":
            data["to"] = str(old / "inside")
        elif case == "occupied":
            new.mkdir()
            (new / "unrelated.txt").write_text("leave me alone")
        elif case == "file":
            new.write_text("leave me alone")
        elif case == "parent-missing":
            data["to"] = str(tmp_path / "missing-parent" / "new")
        elif case == "parent-readonly":
            access = os.access
            monkeypatch.setattr(api.os, "access", lambda path, mode, *args, **kwargs:
                                False if Path(path) == new.parent else access(path, mode, *args, **kwargs))
        elif case == "cross-device":
            stat = os.stat

            def other_device(path, *args, **kwargs):
                result = stat(path, *args, **kwargs)
                if Path(path) == new.parent:
                    values = list(result)
                    values[2] += 1  # st_dev
                    return os.stat_result(values)
                return result

            monkeypatch.setattr(api.os, "stat", other_device)
        elif case == "no-root":
            data = {}
        elif case in ("command-running", "schedule-gating"):
            await pool.execute("INSERT INTO schedule_runs (id,schedule_id,scheduled_for,trigger,status) "
                               "VALUES ('schedule-run','command',now(),'manual',$1)",
                               "running" if case == "command-running" else "gating")
        else:
            await tasks.create_task(task_id="busy", requirement="Busy task", project_id=project["id"],
                                    work_dir=str(old), status="running", pool=pool)
            if case == "run-open":
                await pool.execute("INSERT INTO llm_records (agent_id,task_id,status) "
                                   "VALUES ('planner-busy','busy','running')")
            else:
                await pool.execute("INSERT INTO task_steps (id,task_id,step_index,instruction,status,kind) "
                                   "VALUES ('busy-step','busy',0,'Work',$1,$2)",
                                   {"step-running": "dispatched", "step-deciding": "deciding",
                                    "step-review": "completed"}.get(case, "pending"),
                                   "work" if case.startswith("step-") else "gate")
                if case.startswith("gate-"):
                    await pool.execute("INSERT INTO task_user_gates (step_id,task_id,topic,state) "
                                       "VALUES ('busy-step','busy','Choose',$1)", case.removeprefix("gate-"))
        if case in ("no-directory", "same", "relative", "inside", "occupied", "file", "parent-missing",
                    "parent-readonly", "cross-device"):
            # Path errors must be returned before the otherwise-blocking open run.
            await tasks.create_task(task_id="later-refusal", requirement="Open run", project_id=project["id"],
                                    work_dir=str(old), status="running", pool=pool)
            await pool.execute("INSERT INTO llm_records (agent_id,task_id,status) "
                               "VALUES ('planner-later','later-refusal','running')")
        before = await _rows(pool)
        with pytest.raises(api.RequestError, match=message) as error:
            await api.move_project_dir(project["id"], data)
        assert error.value.status in (api.HTTP_400, api.HTTP_409)
        assert await _rows(pool) == before
        assert old.is_dir() and not old.is_symlink()
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        if case == "occupied":
            assert (new / "unrelated.txt").read_text() == "leave me alone"
        elif case == "file":
            assert new.read_text() == "leave me alone"
        else:
            assert not new.exists()

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("link_old", [True, False])
@pytest.mark.parametrize("existing_empty", [False, True], ids=["new-path", "empty-directory"])
def test_failed_database_commit_restores_directory_and_all_rows(monkeypatch, tmp_path, link_old, existing_empty):
    import asyncpg

    old, new = tmp_path / "old", tmp_path / "new"
    renamed = []
    rename = os.rename

    def track_rename(source, destination, *args, **kwargs):
        result = rename(source, destination, *args, **kwargs)
        renamed.append((Path(source), Path(destination)))
        return result

    monkeypatch.setattr(api.os, "rename", track_rename)

    async def check(pool):
        project = await _project(pool, old)
        if existing_empty:
            new.mkdir()
        await _schedule(pool, project["id"], old)
        await _schedule(pool, project["id"], old / "subdir", schedule_id="agent", kind="agent")
        await tasks.create_task(task_id="stopped-task", requirement="Keep worktree paths on rollback",
                                project_id=project["id"], work_dir=str(old / "checkout"), status="stopped", pool=pool)
        await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                           "VALUES ('stopped-task',$1,$2,'looper/stopped-task/task','base')",
                           str(old), str(old / "checkout"))
        await pool.execute("""
            CREATE FUNCTION refuse_directory_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'directory commit refused' USING ERRCODE = '23514'; END $$;
            CREATE CONSTRAINT TRIGGER refuse_directory_commit AFTER UPDATE ON projects
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION refuse_directory_commit();
        """)
        before = await _rows(pool)
        with pytest.raises((asyncpg.CheckViolationError, api.RequestError), match="directory commit refused"):
            await api.move_project_dir(project["id"], {"to": str(new), "link_old": link_old})
        assert await _rows(pool) == before
        assert renamed == [(old, new), (new, old)]
        assert old.is_dir() and not old.is_symlink()
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        if existing_empty:
            assert new.is_dir() and not list(new.iterdir())
        else:
            assert not os.path.lexists(new)

    run_db(check, monkeypatch, timeout=30)


def test_failed_old_path_symlink_restores_files_and_rows(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        await _schedule(pool, project["id"], old)
        before = await _rows(pool)
        observed = []

        def fail_link(target, link, **kwargs):
            observed.append((Path(target), Path(link)))
            assert new.is_dir() and not old.exists()
            raise OSError("cannot create old path symlink")

        monkeypatch.setattr(api.os, "symlink", fail_link)
        with pytest.raises(api.RequestError, match="cannot create old path symlink") as error:
            await api.move_project_dir(project["id"], {"to": str(new)})
        assert error.value.status == api.HTTP_400
        assert observed == [(new, old)]
        assert await _rows(pool) == before
        assert old.is_dir() and not old.is_symlink()
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert not os.path.lexists(new)

    run_db(check, monkeypatch, timeout=30)


def test_file_inventory_cap_is_explicit_in_confirmation(monkeypatch, tmp_path):
    from looper.agents import project_move

    old, new = tmp_path / "old", tmp_path / "new"
    monkeypatch.setattr(project_move, "FILE_LIMIT", 2)

    async def check(pool):
        project = await _project(pool, old)
        (old / "second.txt").write_bytes(b"second")
        (old / "third.txt").write_bytes(b"third")
        total_bytes = sum(path.stat().st_size for path in old.rglob("*") if path.is_file())
        before = await _rows(pool)
        preview = await api.preview_project_move(project, {"to": str(new)}, db=pool)
        text = api.project_move_impact(project, preview)
        assert preview.truncated and preview.files == 3
        assert preview.bytes == total_bytes
        assert "more than 2 files" in text
        assert f"至少 {total_bytes} 字节" in text
        assert await _rows(pool) == before
        assert old.is_dir() and not new.exists()

    run_db(check, monkeypatch, timeout=30)


def test_inventory_bounds_directory_only_trees(monkeypatch, tmp_path):
    from looper.agents import project_move

    old, new = tmp_path / "old", tmp_path / "new"
    monkeypatch.setattr(project_move, "ENTRY_LIMIT", 2)

    async def check(pool):
        old.mkdir()
        directory = old
        for index in range(10):
            directory /= f"directory-{index}"
            directory.mkdir()
        project = await projects.create_project(name="Directory tree", work_dir=str(old), pool=pool)
        scans = []
        scandir = os.scandir

        def bounded_scan(path):
            scans.append(path)
            assert len(scans) <= 2, "inventory exceeded its directory entry budget"
            return scandir(path)

        monkeypatch.setattr(project_move.os, "scandir", bounded_scan)
        preview = await api.preview_project_move(project, {"to": str(new)}, db=pool)
        assert preview.truncated and preview.files == 0 and preview.bytes == 0
        assert "扫描达到上限" in api.project_move_impact(project, preview)
        assert scans
        assert not new.exists()

    run_db(check, monkeypatch, timeout=30)


def _git(directory, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
                          check=True, capture_output=True, text=True, timeout=10).stdout.strip()


@pytest.mark.parametrize("nested", [False, True], ids=["project-repo", "nested-repo"])
def test_move_repairs_external_linked_task_worktree(monkeypatch, tmp_path, nested):
    old, new, linked = tmp_path / "old", tmp_path / "new", tmp_path / "task-worktree"

    async def check(pool):
        project = await _project(pool, old)
        repo = old / "repository" if nested else old
        repo.mkdir(exist_ok=True)
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=Directory move test", "-c", "user.email=move@example.invalid",
             "commit", "--allow-empty", "-qm", "Initial files")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "worktree", "add", "-qb", "looper/task-example/task", str(linked))
        await tasks.create_task(task_id="task-example", requirement="Keep its worktree", project_id=project["id"],
                                work_dir=str(linked), workspace_dir=str(repo), status="stopped", pool=pool)
        await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                           "VALUES ('task-example',$1,$2,'looper/task-example/task',$3)", str(repo), str(linked), base)
        # Positive control: this really is a working linked checkout before the move.
        assert _git(linked, "rev-parse", "HEAD") == base
        result = await api.move_project_dir(project["id"], {"to": str(new), "link_old": False})
        assert not old.exists()
        assert result["repairs"]
        assert _git(linked, "status", "--porcelain") == ""
        assert _git(linked, "rev-parse", "HEAD") == base
        moved_repo = new / "repository" if nested else new
        assert str(linked.resolve()) in _git(moved_repo, "worktree", "list", "--porcelain")
        assert str(moved_repo.resolve()) in (linked / ".git").read_text()

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("layout", ["external-worktree", "both-inside", "linked-source"])
def test_deleting_stopped_task_after_move_preserves_uncommitted_files(monkeypatch, tmp_path, layout):
    old, new = tmp_path / "old", tmp_path / "new"
    repo = tmp_path / "repository" if layout == "linked-source" else old
    linked = (old if layout == "linked-source" else
              old / "checkout" if layout == "both-inside" else tmp_path / "checkout")
    branch = "looper/stopped-task/task"

    async def check(pool):
        repo.mkdir()
        (repo / "tracked.txt").write_text("original\n")
        _git(repo, "init", "-q")
        _git(repo, "add", "tracked.txt")
        _git(repo, "-c", "user.name=Directory move test", "-c", "user.email=move@example.invalid",
             "commit", "-qm", "Initial files")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "worktree", "add", "-qb", branch, str(linked))
        (linked / "tracked.txt").write_text("uncommitted edit\n")
        (linked / "untracked.txt").write_text("uncommitted new file\n")
        assert "tracked.txt" in _git(linked, "status", "--porcelain")
        assert "?? untracked.txt" in _git(linked, "status", "--porcelain")
        project = await projects.create_project(name="Preserve stopped work", work_dir=str(old), pool=pool)
        await tasks.create_task(task_id="stopped-task", requirement="Keep unfinished work",
                                project_id=project["id"], work_dir=str(linked), workspace_dir=str(repo),
                                status="stopped", pool=pool)
        await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                           "VALUES ('stopped-task',$1,$2,$3,$4)", str(repo), str(linked), branch, base)

        result = await api.move_project_dir(project["id"], {"to": str(new), "link_old": False})
        assert result["repairs"] and all(row["ok"] for row in result["repairs"])
        assert not old.exists()
        moved_repo = repo if layout == "linked-source" else new
        moved_linked = new if layout == "linked-source" else new / "checkout" if layout == "both-inside" else linked
        assert (moved_linked / "tracked.txt").read_text() == "uncommitted edit\n"
        assert (moved_linked / "untracked.txt").read_text() == "uncommitted new file\n"

        assert await api.delete_task("stopped-task", None) == {"deleted": "stopped-task"}
        assert await tasks.get_task("stopped-task", pool=pool) is None
        assert _git(moved_repo, "show", f"{branch}:tracked.txt") == "uncommitted edit"
        assert _git(moved_repo, "show", f"{branch}:untracked.txt") == "uncommitted new file"
        assert not moved_linked.exists()
        assert str(moved_linked.resolve()) not in _git(moved_repo, "worktree", "list", "--porcelain")

    run_db(check, monkeypatch, timeout=30)


def test_git_repair_failure_is_reported_after_successful_move(monkeypatch, tmp_path):
    from looper.agents import step_workspaces

    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        _git(old, "init", "-q")
        await _schedule(pool, project["id"], old)
        attempted = []

        async def fail_repair(directory, *args, **kwargs):
            attempted.append((Path(directory), args))
            raise step_workspaces.GitError("simulated worktree repair failure")

        monkeypatch.setattr(step_workspaces, "_git", fail_repair)
        result = await api.move_project_dir(project["id"], {"to": str(new)})
        assert attempted == [(new, ("worktree", "repair"))]
        assert result["repairs"] == [{"repository": str(new), "ok": False,
                                      "output": "simulated worktree repair failure"}]
        assert old.is_symlink() and old.resolve() == new.resolve()
        assert (new / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)
        assert await pool.fetchval("SELECT work_dir FROM schedules WHERE id='command'") == str(new)
        assert await pool.fetchval("SELECT count(*) FROM project_activity WHERE project_id=$1", project["id"]) == 1

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize(("layout", "managed"), [
    ("linked-source", True), ("both-inside", True), ("both-inside", False),
])
def test_move_repairs_moved_linked_checkout(monkeypatch, tmp_path, layout, managed):
    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        if layout == "linked-source":
            repo, linked = tmp_path / "external-main", old
        else:
            old.mkdir()
            repo, linked = old / "repository", old / "linked"
            # Discover the linked checkout first, before its common repository.
            linked.mkdir()
        repo.mkdir()
        (repo / "kept.txt").write_text("tracked content\n")
        _git(repo, "init", "-q")
        _git(repo, "add", "kept.txt")
        _git(repo, "-c", "user.name=Directory move test", "-c", "user.email=move@example.invalid",
             "commit", "-qm", "Initial files")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "worktree", "add", "-qb", "looper/moving-task/task", str(linked))
        assert _git(linked, "status", "--porcelain") == ""
        project = await projects.create_project(name="Moved linked checkout", work_dir=str(old), pool=pool)
        if managed:
            await tasks.create_task(task_id="moving-task", requirement="Preserve linked checkout",
                                    project_id=project["id"], work_dir=str(linked), workspace_dir=str(repo),
                                    status="stopped", pool=pool)
            await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                               "VALUES ('moving-task',$1,$2,'looper/moving-task/task',$3)", str(repo), str(linked), base)
        result = await api.move_project_dir(project["id"], {"to": str(new), "link_old": False})
        moved_linked = new if layout == "linked-source" else new / "linked"
        moved_main = repo if layout == "linked-source" else new / "repository"
        assert not old.exists()
        assert result["repairs"] and all(row["ok"] for row in result["repairs"])
        assert _git(moved_linked, "status", "--porcelain") == ""
        assert _git(moved_linked, "rev-parse", "HEAD") == base
        assert _git(moved_main, "status", "--porcelain") == ""
        registered = _git(moved_main, "worktree", "list", "--porcelain")
        assert str(moved_linked.resolve()) in registered
        assert str(old.resolve()) not in registered
        assert str(moved_main.resolve()) in (moved_linked / ".git").read_text()
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)

    run_db(check, monkeypatch, timeout=30)


def test_existing_database_adds_project_activity_idempotently(monkeypatch, tmp_path):
    from looper.db import connection

    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        await pool.execute("DROP TABLE project_activity")
        await pool.execute("DELETE FROM schema_migrations WHERE name='project_activity'")
        before = dict(await pool.fetchrow("SELECT * FROM projects WHERE id=$1", project["id"]))
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
            assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                           "WHERE name='project_activity')")
            assert await conn.fetchval("SELECT to_regclass('project_activity')") is None
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert dict(await pool.fetchrow("SELECT * FROM projects WHERE id=$1", project["id"])) == before
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        await api.move_project_dir(project["id"], {"to": str(new)})
        assert await pool.fetchval("SELECT count(*) FROM project_activity WHERE project_id=$1", project["id"]) == 1
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)

    run_db(check, monkeypatch, timeout=30)


def test_waiting_planner_cannot_open_a_run_between_validation_and_move(monkeypatch, tmp_path):
    from looper.agents import project_move
    from looper.db import llm_records

    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        await tasks.create_task(task_id="waiting-planner", requirement="Wait before waking",
                                project_id=project["id"], work_dir=str(old), status="waiting", pool=pool)
        entered, release = threading.Event(), threading.Event()
        inventory = project_move._inventory

        def paused_inventory(directory):
            entered.set()
            assert release.wait(10), "move inventory was not released"
            return inventory(directory)

        monkeypatch.setattr(project_move, "_inventory", paused_inventory)
        moving = opening = None
        try:
            async with pool.acquire() as writer:
                pid = await writer.fetchval("SELECT pg_backend_pid()")
                moving = asyncio.create_task(api.move_project_dir(project["id"], {"to": str(new)}))
                assert await asyncio.to_thread(entered.wait, 5), "move did not reach inventory"
                opening = asyncio.create_task(llm_records.start_record(
                    agent_id="planner-waiting", task_id="waiting-planner", pool=writer))
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(opening), timeout=0.1)
                assert await pool.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=$1 "
                    "AND relation='llm_records'::regclass AND NOT granted)", pid)
                assert not old.is_symlink() and not new.exists()
                release.set()
                result = await moving
                record_id = await opening
                assert result["project"]["work_dir"] == str(new)
                assert await writer.fetchval("SELECT status FROM llm_records WHERE id=$1", record_id) == "running"
                assert old.is_symlink() and (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        finally:
            release.set()
            pending = [task for task in (moving, opening) if task is not None]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    run_db(check, monkeypatch, timeout=30)
