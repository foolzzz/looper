"""Operator-only export of project_documents into a vault; existing readers stay unchanged."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from . import knowledge, knowledge_directory, vault


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare(rows: Sequence[Mapping[str, Any]], directory: str) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Stable names include the source id, so duplicate titles never overwrite one another."""
    files, report = {}, []
    for row in rows:
        reference = row["source_kind"] == "path"
        stem = PurePosixPath(row["filename"]).stem if row["filename"] else row["title"]
        filename = knowledge.safe_filename(f"{row['id']}-{stem}.md", default=f"{row['id']}.md")
        target = str(PurePosixPath(directory) / filename)
        frontmatter = {"title": row["title"], "summary": row["summary"], "source_document_id": row["id"],
                       "original_sha256": row["sha256"], "source_kind": row["source_kind"]}
        if reference:
            frontmatter["original_path"] = row["path"]
        # JSON strings are YAML scalars too: quotes, newlines and YAML-looking titles stay literal.
        header = "---\n" + "".join(f"{key}: {json.dumps(value, ensure_ascii=False)}\n"
                                    for key, value in frontmatter.items()) + "---\n\n"
        content = (f"Reference to the original project file: {json.dumps(row['path'], ensure_ascii=False)}\n"
                   if reference else row["content"])
        data = (header + content).encode("utf-8")
        if target in files:
            raise ValueError("source documents produce duplicate target filenames")
        files[target] = data
        report.append({"document_id": row["id"], "target": target, "size_bytes": len(data),
                       "over_8kb": len(data) > 8 * 1024, "frontmatter": frontmatter, "reference": reference,
                       "original_path": row["path"] if reference else None, "sha256": _sha(data)})
    return files, report


def _read(root: Path, relative: str) -> bytes | None:
    """Read a target without following links, including links added after settings validation."""
    fds = vault._open_real_path(root)
    try:
        for part in PurePosixPath(relative).parts[:-1]:
            try:
                fds.append(os.open(part, vault._PASS, dir_fd=fds[-1]))
            except FileNotFoundError:
                return None
        try:
            fd = os.open(PurePosixPath(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
        except FileNotFoundError:
            return None
        fds.append(fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("a migration target must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(knowledge.DOCUMENT_MAX_BYTES + 64 * 1024 + 1)
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _backup(runtime_root: Path, project: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, str]:
    data = (json.dumps({"source": "project_documents", "project_id": project["id"],
                        "project_name": project["name"], "documents": rows}, ensure_ascii=False, indent=2,
                       default=lambda value: value.isoformat()) + "\n").encode("utf-8")
    root = runtime_root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fds = vault._open_real_path(root)
    try:
        for part in ("backups", "project-knowledge"):
            try:
                os.mkdir(part, 0o700, dir_fd=fds[-1])
            except FileExistsError:
                pass
            fds.append(os.open(part, vault._PASS, dir_fd=fds[-1]))
        name = f"{secrets.token_hex(16)}.json"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fds[-1])
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return {"path": str(root / "backups" / "project-knowledge" / name), "sha256": _sha(data)}
    finally:
        for fd in reversed(fds):
            os.close(fd)


async def migrate(pool: Any, project_name: str, runtime_root: Path, *, execute: bool = False,
                  on_backup: Callable[[dict[str, str]], None] | None = None) -> dict[str, Any]:
    """Snapshot source rows, preview by default, and export through exactly one vault transaction."""
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        project = await conn.fetchrow("SELECT * FROM projects WHERE lower(name)=lower($1)", project_name)
        if project is None:
            raise ValueError("the project does not exist")
        raw = await conn.fetchval("SELECT value FROM system_settings WHERE key=$1", vault.SETTING_KEY)
        if raw is None:
            raise ValueError("the knowledge vault is not configured")
        stored = json.loads(raw)
        config = vault.check_config(stored, validate_profile=False)
        if stored.get("path") != str(config.path):
            raise ValueError("the configured vault path now resolves elsewhere")
        directory = knowledge_directory.validate(config, project["knowledge_dir"])
        if directory is None:
            raise ValueError("the project has no knowledge_dir")
        rows = [dict(row) for row in await conn.fetch(
            "SELECT * FROM project_documents WHERE project_id=$1 ORDER BY created_at,id", project["id"])]
    files, reports = prepare(rows, directory)
    result = {"state": "dry_run", "project": project["name"], "source": "project_documents", "files": reports}
    if not execute:
        return result
    expected = {path: await asyncio.to_thread(_read, config.path, path) for path in files}
    conflicts = [path for path, data in files.items() if expected[path] is not None and expected[path] != data]
    if conflicts:
        return {**result, "state": "refused", "conflicts": conflicts}
    if runtime_root.resolve().is_relative_to(config.path):
        raise ValueError("migration backups must be outside the vault")
    backup = await asyncio.to_thread(_backup, runtime_root, project, rows)
    result["backup"] = backup
    if on_backup:
        on_backup(backup)
    from . import vault_commit

    committed = await vault_commit.commit(config, files,
        f"Migrate project_documents from project {project['name']} ({project['id']})", runtime_root=runtime_root,
        expected=expected)
    result.update(state=committed.state.value, commit=committed.commit, reason=committed.reason)
    if committed.state.value == "blocked":
        result.update(branch=committed.branch, worktree=str(committed.worktree) if committed.worktree else None)
        return result
    for report in reports:
        actual = await asyncio.to_thread(_read, config.path, report["target"])
        report["actual_sha256"] = _sha(actual) if actual is not None else None
        report["verified"] = report["actual_sha256"] == report["sha256"]
    if not all(report["verified"] for report in reports):
        result["state"] = "verification_failed"
    return result
