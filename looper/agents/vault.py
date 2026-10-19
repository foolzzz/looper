"""The operator's knowledge vault: its setting, bounded reads and confined, single-note writes.

Projects opt in. Files stay in the operator's directory; Looper commits only
the note it wrote, under a lock for that vault, after checking its sha256.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import ntpath
import os
import re
import secrets
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .. import child_env
from ..db import projects as db_projects, system_settings, tasks as db_tasks
from .ops import ErrorCode, OpError
from .project_execution import ProjectExecution

SETTING_KEY = "knowledge_vault"
READ_MAX_BYTES = WRITE_MAX_BYTES = 64 * 1024
RUN_WRITE_MAX_BYTES = 256 * 1024
SEARCH_FILE_MAX_BYTES = 512 * 1024
SEARCH_MAX_FILES = 10_000
SEARCH_MAX_BYTES = 32 * 1024 * 1024
SEARCH_MAX_SECONDS = 5.0
SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 30
SNIPPET_MAX_CHARS = REASON_MAX_CHARS = 200
RULES_MAX_CHARS = 16_000
GIT_TIMEOUT_SECONDS = 30

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_LOCK = threading.Lock()


@dataclass(frozen=True)
class VaultConfig:
    path: Path
    writable: tuple[str, ...]
    rules_file: str | None

    def folders(self) -> str:
        return ", ".join(f"{w}/" for w in self.writable)


def _path_problem(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return "must be a string"
    if not raw:
        return "must not be empty"
    if raw.startswith("/") or ntpath.splitdrive(raw)[0]:
        return "must be relative to the vault"
    if "\\" in raw:
        return "must use /, not backslashes"
    if any(not part or part in (".", "..") or part.startswith(".") for part in raw.split("/")):
        return "must have no empty, . or .. parts, or parts starting with ."
    if "\0" in raw:
        return "must not contain NUL"
    return None


def check_config(raw: Mapping) -> VaultConfig:
    """The stored setting checked against the directory as it is now; raises ``ValueError`` on a problem."""
    if not isinstance(raw, Mapping):
        raise ValueError("the knowledge vault setting must be an object")
    for key in raw:
        if key not in ("path", "writable", "rules_file"):
            raise ValueError(f"unknown knowledge vault key {key!r}")
    path = raw.get("path")
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    if not os.path.isabs(path):
        raise ValueError("path must be absolute")
    if "\0" in path:
        raise ValueError("path must not contain NUL")
    root = Path(path).resolve()
    if not root.exists():
        raise ValueError(f"the vault path {path} does not exist")
    if not root.is_dir():
        raise ValueError(f"the vault path {path} is not a directory")
    writable = raw.get("writable")
    if writable is None:
        writable = []
    if not isinstance(writable, list) or any(not isinstance(w, str) for w in writable):
        raise ValueError("writable must be a list of strings")
    folders: list[str] = []
    for item in writable:
        name = item.strip().rstrip("/")
        if problem := _path_problem(name):
            raise ValueError(f"writable folder {item!r} {problem}")
        if name in folders:
            raise ValueError(f"duplicate writable folder {name}")
        folder = root / name
        parts = PurePosixPath(name).parts
        if any(root.joinpath(*parts[:depth]).is_symlink() for depth in range(1, len(parts) + 1)):
            raise ValueError(f"writable folder {name} is or passes through a symbolic link; Looper writes only "
                             "to real folders")
        if not folder.resolve().is_relative_to(root):
            raise ValueError(f"writable folder {name} leaves the vault (a symbolic link?)")
        if not folder.is_dir():
            raise ValueError(f"writable folder {name} must exist and be a directory")
        folders.append(PurePosixPath(name).as_posix())
    rules = raw.get("rules_file")
    if rules is None or rules == "":
        rules = None
    else:
        if isinstance(rules, str):
            rules = rules.strip().rstrip("/")
        if problem := _path_problem(rules):
            raise ValueError(f"rules_file {rules!r} {problem}")
        target = root / rules
        if not target.resolve().is_relative_to(root):
            raise ValueError(f"rules file {rules} leaves the vault (a symbolic link?)")
        if not target.is_file():
            raise ValueError(f"rules file {rules} must exist and be a regular file")
    return VaultConfig(root, tuple(folders), rules)


async def load() -> tuple[VaultConfig | None, str | None]:
    """The setting, rechecked now; a stale setting is a problem, never an active vault."""
    stored = await system_settings.get_settings([SETTING_KEY])
    if SETTING_KEY not in stored:
        return None, None
    try:
        raw = json.loads(stored[SETTING_KEY])
        config = check_config(raw)
        if str(config.path) != raw.get("path"):
            # It was saved resolved: a link somewhere on the path now leads elsewhere.
            raise ValueError(f"the vault path {raw.get('path')} now leads to {config.path} (a symbolic link?)")
        return config, None
    except (ValueError, OSError, RuntimeError) as exc:
        return None, f"the knowledge vault setting no longer holds: {exc}"


async def active() -> VaultConfig | None:
    return (await load())[0]


async def for_project(project: Mapping | None) -> VaultConfig | None:
    if not ProjectExecution.from_row(project).vault_on:
        return None
    return await active()


async def for_task(task_id: str) -> tuple[VaultConfig | None, str]:
    task = await db_tasks.get_task(task_id)
    project = await db_projects.get_project(task["project_id"]) if task and task.get("project_id") else None
    if not ProjectExecution.from_row(project).vault_on:
        return None, "this task's project has the knowledge vault off"
    config, problem = await load()
    return config, "" if config else problem or "the knowledge vault is not configured"


async def settings_view() -> dict:
    config, problem = await load()
    raw: Mapping = {}
    if config:
        raw = {"path": str(config.path), "writable": list(config.writable), "rules_file": config.rules_file}
    elif problem:
        stored = await system_settings.get_settings([SETTING_KEY])
        try:
            value = json.loads(stored.get(SETTING_KEY, "null"))
            if isinstance(value, Mapping):
                raw = value
        except ValueError:
            pass
    path = raw.get("path")
    is_git = await _git_root(Path(path)) is not None if isinstance(path, str) and os.path.isabs(path) else False
    return {"configured": config is not None, "path": path, "writable": raw.get("writable") or [],
            "rules_file": raw.get("rules_file"), "git": is_git, "problem": problem}


async def update_settings(body: Mapping) -> dict:
    from . import projects

    try:
        if ("path" in body and body["path"] is None and set(body) <= {"path", "writable", "rules_file"}
                and body.get("writable") in (None, []) and body.get("rules_file") in (None, "")):
            await system_settings.put_settings({SETTING_KEY: None})
        else:
            config = check_config(body)
            await system_settings.put_settings({SETTING_KEY: json.dumps({
                "path": str(config.path), "writable": list(config.writable), "rules_file": config.rules_file})})
    except ValueError as exc:
        raise projects.RequestError(projects.HTTP_400, str(exc)) from None
    return await settings_view()


def _relative(config: VaultConfig, raw: str, *, what: str, resolve: bool = True) -> PurePosixPath:
    if not isinstance(raw, str):
        raise OpError(ErrorCode.INVALID, f"{what} must be a string")
    while raw.startswith("./"):
        raw = raw[2:]
    if problem := _path_problem(raw):
        raise OpError(ErrorCode.INVALID, f"{what} {raw!r} {problem}")
    rel = PurePosixPath(raw)
    if resolve:
        resolved = (config.path / rel).resolve()
        if not resolved.is_relative_to(config.path):
            raise OpError(ErrorCode.FORBIDDEN, f"{rel} leaves the vault")
        if any(part.startswith(".") for part in resolved.relative_to(config.path).parts):
            raise OpError(ErrorCode.FORBIDDEN, f"{rel} points into a hidden path of the vault")
    return rel


async def search(config: VaultConfig, query: str, limit: int | None = None) -> dict:
    """Search Markdown paths and text in a thread, within the scan and snippet limits."""
    if not isinstance(query, str) or not (words := query.lower().split()):
        raise OpError(ErrorCode.INVALID, "query must contain at least one word")
    if limit is None:
        limit = SEARCH_DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SEARCH_MAX_LIMIT:
        raise OpError(ErrorCode.INVALID, f"limit must be from 1 to {SEARCH_MAX_LIMIT}")

    def scan() -> dict:
        results: list[dict] = []
        scanned, total_bytes, truncated = 0, 0, False
        started = time.monotonic()
        for directory, dirs, files in os.walk(config.path, followlinks=False):
            if time.monotonic() - started >= SEARCH_MAX_SECONDS:
                truncated = True
                break
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink())
            for filename in sorted(files):
                if (time.monotonic() - started >= SEARCH_MAX_SECONDS
                        or total_bytes >= SEARCH_MAX_BYTES or scanned >= SEARCH_MAX_FILES):
                    truncated = True
                    break
                target = Path(directory) / filename
                if filename.startswith(".") or not filename.endswith(".md") or target.is_symlink():
                    continue
                try:
                    if not target.is_file() or target.stat().st_size > SEARCH_FILE_MAX_BYTES:
                        continue
                    scanned += 1
                    with target.open("rb") as stream:
                        data = stream.read(min(SEARCH_FILE_MAX_BYTES, SEARCH_MAX_BYTES - total_bytes))
                    total_bytes += len(data)
                    text = data.decode("utf-8", errors="replace")
                except OSError:
                    continue
                path = target.relative_to(config.path).as_posix()
                lower = text.lower()
                if not all(word in path.lower() or word in lower for word in words):
                    continue
                first = min((lower.find(word) for word in words if word in lower), default=0)
                snippet = re.sub(r"\s+", " ", text[max(0, first - 60):]).strip()[:SNIPPET_MAX_CHARS]
                results.append({"path": path, "hits": sum(lower.count(word) for word in words), "snippet": snippet})
            if (total_bytes >= SEARCH_MAX_BYTES or scanned >= SEARCH_MAX_FILES
                    or time.monotonic() - started >= SEARCH_MAX_SECONDS):
                truncated = True
            if truncated:
                break
        results.sort(key=lambda r: (-r["hits"], r["path"]))
        return {"results": results[:limit], "matches": len(results), "scanned": scanned, "truncated": truncated}

    return await asyncio.to_thread(scan)


async def read(config: VaultConfig, path: str) -> dict:
    def read_file() -> dict:
        rel = _relative(config, path, what="path")
        target = config.path / rel
        if not target.exists():
            raise OpError(ErrorCode.NOT_FOUND, f"{rel} does not exist")
        if not target.is_file():
            raise OpError(ErrorCode.INVALID, f"{rel} is not a regular file")
        data = target.read_bytes()
        if b"\0" in data[:8192]:
            raise OpError(ErrorCode.INVALID, f"{rel} is not a text file (NUL in the first 8 KiB)")
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise OpError(ErrorCode.INVALID, f"{rel} is not a UTF-8 text file") from None
        return {"path": rel.as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                "content": data[:READ_MAX_BYTES].decode("utf-8", errors="ignore"),
                "truncated": len(data) > READ_MAX_BYTES}

    return await asyncio.to_thread(read_file)


async def rules_text(config: VaultConfig) -> tuple[str | None, str | None]:
    if not config.rules_file:
        return None, None
    try:
        content = (await read(config, config.rules_file))["content"]
        if len(content) > RULES_MAX_CHARS:
            content = content[:RULES_MAX_CHARS] + "\n[… clipped]"
        return content, None
    except Exception as exc:
        return None, f"the vault's rules file {config.rules_file} could not be read: {exc}"


def _run_git(command: list[str], *, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """One git command in a process group of its own, so a timeout ends the hooks it started too; its output is
    decoded with replacement, whatever a hook prints."""
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(command, proc.returncode, out.decode("utf-8", errors="replace"),
                                       err.decode("utf-8", errors="replace"))


def _git(root: Path, *args: str) -> str:
    env = {**child_env.inherited(), "GIT_TERMINAL_PROMPT": "0"}
    command = ["git", "--literal-pathspecs", "-C", str(root), *args]
    for attempt in range(3):
        proc = _run_git(command, env=env)
        if not proc.returncode:
            return proc.stdout.strip()
        if "index.lock" not in proc.stderr or not ({"add", "commit"} & set(args)) or attempt == 2:
            lines = proc.stderr.splitlines()
            raise subprocess.CalledProcessError(proc.returncode, command, output=proc.stdout,
                                                stderr=lines[0][:200] if lines else "")
        time.sleep(0.25 * (attempt + 1))
    raise AssertionError("unreachable")


def _git_root_sync(root: Path) -> Path | None:
    try:
        return Path(_git(root, "rev-parse", "--show-toplevel"))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


async def _git_root(root: Path) -> Path | None:
    try:
        return await asyncio.to_thread(_git_root_sync, root)
    except subprocess.TimeoutExpired:
        return None


async def write(config: VaultConfig, path: str, content: str, expected_sha256: str | None, reason: str, *,
                task_id: str, who: str) -> dict:
    """Replace one note after a sha256 check, then commit that path only, keeping other staged changes."""
    rel = _relative(config, path, what="path", resolve=False)
    if rel.suffix != ".md":
        raise OpError(ErrorCode.INVALID, "only Markdown notes (.md) can be written")
    folder = next((PurePosixPath(w) for w in config.writable
                   if rel.parts[:len(PurePosixPath(w).parts)] == PurePosixPath(w).parts), None)
    if folder is None:
        raise OpError(ErrorCode.FORBIDDEN,
                      f"{path} is not in a writable folder of the vault; the writable folders are {config.folders()}"
                      if config.writable else "the vault is read only: no folder is writable")
    if not isinstance(content, str) or "\0" in content:
        raise OpError(ErrorCode.INVALID, "content must be a string with no NUL characters")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise OpError(ErrorCode.INVALID, "content must be valid UTF-8 text") from None
    if len(data) > WRITE_MAX_BYTES:
        raise OpError(ErrorCode.INVALID, f"content has {len(data)} bytes; at most {WRITE_MAX_BYTES} bytes (64 KiB) "
                                         "can be written")
    if not isinstance(reason, str) or "\n" in reason or "\r" in reason or not 1 <= len(reason.strip()) <= REASON_MAX_CHARS:
        raise OpError(ErrorCode.INVALID, f"reason must be one line, from 1 to {REASON_MAX_CHARS} characters")
    reason = reason.strip()

    def replace_note() -> dict:
        fds: list[int] = []
        try:
            try:
                fd = os.open(config.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise OpError(ErrorCode.FORBIDDEN, "the vault folder changed since its setting was checked "
                                                       "(a symbolic link?)") from None
                raise
            fds.append(fd)
            opened_root, named_root = os.fstat(fd), os.stat(config.path)
            if ((opened_root.st_dev, opened_root.st_ino) != (named_root.st_dev, named_root.st_ino)
                    or os.path.realpath(config.path) != str(config.path)):
                raise OpError(ErrorCode.FORBIDDEN, "the vault folder changed since its setting was checked "
                                                   "(a symbolic link?)")
            for index, part in enumerate(rel.parts[:-1]):
                try:
                    try:
                        next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    except FileNotFoundError:
                        if index < len(folder.parts):
                            raise
                        try:
                            os.mkdir(part, 0o755, dir_fd=fd)
                        except FileExistsError:
                            pass
                        next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise OpError(ErrorCode.FORBIDDEN,
                                      f"{rel} leaves the writable folder (a symbolic link?)") from None
                    raise
                fds.append(next_fd)
                fd = next_fd
            name = rel.name
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            mode, current, old = 0o644, None, None
            if info is not None:
                if not stat.S_ISREG(info.st_mode):
                    raise OpError(ErrorCode.FORBIDDEN, f"{rel} must be a regular file, not a symbolic link")
                if info.st_size > READ_MAX_BYTES:
                    raise OpError(ErrorCode.FORBIDDEN,
                                  f"{rel} is larger than 64 KiB; it cannot be rewritten through this tool")
                try:
                    current_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise OpError(ErrorCode.FORBIDDEN,
                                      f"{rel} must be a regular file, not a symbolic link") from None
                    raise
                try:
                    with os.fdopen(current_fd, "rb", closefd=False) as stream:
                        opened = os.fstat(stream.fileno())
                        if not stat.S_ISREG(opened.st_mode):
                            raise OpError(ErrorCode.FORBIDDEN, f"{rel} must be a regular file, not a symbolic link")
                        old = stream.read(READ_MAX_BYTES + 1)
                finally:
                    os.close(current_fd)
                if len(old) > READ_MAX_BYTES:
                    raise OpError(ErrorCode.FORBIDDEN,
                                  f"{rel} is larger than 64 KiB; it cannot be rewritten through this tool")
                current = hashlib.sha256(old).hexdigest()
                mode = stat.S_IMODE(opened.st_mode)
            if expected_sha256 is None and info is not None:
                raise OpError(ErrorCode.CONFLICT, f"{rel} exists (sha256 {current}): read it and pass its sha256")
            if expected_sha256 is not None and info is None:
                raise OpError(ErrorCode.CONFLICT, f"{rel} does not exist: pass null for a new note")
            if expected_sha256 != current:
                raise OpError(ErrorCode.CONFLICT,
                              f"{rel} changed since you read it (now sha256 {current}): read it again")
            if old == data:
                return {"path": rel.as_posix(), "sha256": current, "bytes": len(data), "commit": None,
                        "unchanged": True, "note": "the note already has this text: nothing written"}
            temporary = f".looper-{secrets.token_hex(8)}.tmp"
            tfd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
            try:
                try:
                    with os.fdopen(tfd, "wb", closefd=False) as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(tfd)
                        os.fchmod(tfd, mode)
                finally:
                    os.close(tfd)
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
                raise
            return {"path": rel.as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data), "commit": None}
        finally:
            for fd in reversed(fds):
                os.close(fd)

    with _LOCKS_LOCK:
        lock = _LOCKS.setdefault(str(config.path), threading.Lock())

    def finish_write() -> dict:
        with lock:
            result = replace_note()
            if result.get("unchanged"):
                return result
            try:
                if _git_root_sync(config.path) is None:
                    result["note"] = "the vault is not a git repository: written without a commit"
                    result["git_status"] = "not_repository"
                    return result
                _git(config.path, "add", "--", rel.as_posix())
                _git(config.path, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m",
                     f"looper: {task_id} {who}: {reason}", "--", rel.as_posix())
                result["commit"] = _git(config.path, "rev-parse", "HEAD")
            except subprocess.TimeoutExpired:
                result.update(git_status="timed_out", note="written, but git timed out")
            except subprocess.CalledProcessError as exc:
                result.update(git_status="failed", git_exit=exc.returncode,
                              note="written, but the commit failed: " + (exc.stderr or f"git exit {exc.returncode}"))
            except OSError:
                result.update(git_status="failed", git_exit=None, note="written, but git could not be started")
            return result

    # Cancelling the await must wait for the entire transaction and its bookkeeping.
    pending = asyncio.create_task(asyncio.to_thread(finish_write))
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
    result = pending.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
