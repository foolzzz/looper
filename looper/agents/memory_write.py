"""Confirmed memory -> an owned vault worktree -> a verified commit -> fast-forward.

The database is staging; the vault stays authoritative. No model participates in this path. An editor's unsaved
buffer can still overwrite a later write, and agents with shell access share the operator's OS permissions: these
are supported-operation boundaries, not a sandbox (memory-write-path RFC, section 6.6).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import secrets
import stat
import subprocess
import threading
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..db import connection, memory as store
from . import memory_model as model, memory_settings, vault
from .advice_lint import _SECRETS

logger = logging.getLogger(__name__)
TICK_SECONDS = 10
_ATTEMPT = re.compile(r"M-[0-9]{4,18}-a[1-9][0-9]*")
_BRANCH_PREFIX = "looper/memory/"
_active: MemoryWriteService | None = None


def wake() -> None:
    """Wake the local writer after a committed DM confirmation or retry."""
    if _active is not None:
        _active.wake()


class WriteFailure(Exception):
    """A fixed, public-safe reason: never git's output or a note's contents."""

    def __init__(self, state: str, reason: str):
        self.state, self.reason = model.State(state), model.Reason(reason)
        super().__init__(self.reason.value)


def subject(settings: model.MemorySettings, row: Mapping[str, Any]) -> str:
    return f"{settings.commit_prefix}: {model.format_id(row['id'])} {row['kind']} {row['title']}"


def validate_changes(config: vault.VaultConfig, settings: model.MemorySettings, kind: model.Kind | str,
                     changes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Recheck the typed whitelist, rules, links, new-note frontmatter/index and secret bounds at each entry."""
    parsed = model.parse_changes(changes)
    target = settings.targets.get(model.Kind(kind))
    if target is None:
        raise ValueError("the memory kind has no configured target")
    rules = {*(PurePosixPath(p) for p in settings.rules_files),
             *([PurePosixPath(config.rules_file)] if config.rules_file else [])}
    root = PurePosixPath(settings.root)
    index = root / target.index if target.index else None
    for change in parsed:
        path = PurePosixPath(change["path"])
        if not path.is_relative_to(root):
            raise ValueError("a change leaves the memory root")
        rel = path.relative_to(root)
        if path in rules or path.name.upper() in {"AGENTS.MD", "CLAUDE.MD"}:
            raise ValueError("rules files are never memory targets")
        if model._passes_link(config.path, str(path)):
            raise ValueError("a change passes through a symbolic link")
        op = model.ChangeOp(change["op"])
        if target.shape is model.Shape.ENTRY:
            if str(rel) not in target.files or op not in (model.ChangeOp.APPEND_LINE, model.ChangeOp.REPLACE_LINE):
                raise ValueError("an entry target permits only line operations on its configured pages")
        elif not any(rel.is_relative_to(folder) for folder in target.folders):
            raise ValueError("a change is outside the kind's configured folders")
        if path == index and op is not model.ChangeOp.APPEND_LINE:
            raise ValueError("an index accepts append_line only")
        if op is model.ChangeOp.CREATE_NOTE:
            import yaml

            if any(char.isspace() for char in str(path)):
                raise ValueError("a new note's path cannot contain whitespace")
            if target.shape is not model.Shape.NOTE:
                raise ValueError("an entry target cannot create notes")
            if index and not any(item["op"] == "append_line" and item["path"] == str(index) for item in parsed):
                raise ValueError("a new note must update its index in the same revision")
            lines = change["content"].splitlines()
            if not lines or lines[0] != "---" or "---" not in lines[1:]:
                raise ValueError("a new note must have YAML frontmatter")
            try:
                frontmatter = yaml.safe_load("\n".join(lines[1:lines.index("---", 1)]))
            except yaml.YAMLError:
                raise ValueError("a new note's frontmatter is invalid YAML") from None
            if not isinstance(frontmatter, dict) or any(key not in frontmatter for key in settings.required_frontmatter):
                raise ValueError("a new note's frontmatter lacks required keys")
        if any(_SECRETS.search(change[key]) for key in ("content", "line", "new") if key in change):
            raise ValueError("Looper does not store credentials in memory")
    return parsed


def _read(root: Path, path: str) -> bytes | None:
    fds = vault._open_real_path(root)
    try:
        for part in PurePosixPath(path).parts[:-1]:
            try:
                fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
            except FileNotFoundError:
                return None
        try:
            fd = os.open(PurePosixPath(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
        except FileNotFoundError:
            return None
        fds.append(fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("a memory target must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(vault.READ_MAX_BYTES + 1)
        if len(data) > vault.READ_MAX_BYTES or b"\0" in data:
            raise ValueError("a memory target must be bounded UTF-8 text")
        data.decode("utf-8")
        return data
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _apply(change: Mapping[str, Any], before: bytes | None) -> bytes:
    op = model.ChangeOp(change["op"])
    if op is model.ChangeOp.CREATE_NOTE:
        if before is not None:
            raise WriteFailure("conflict", "base_changed")
        return change["content"].encode("utf-8")
    if before is None:
        raise WriteFailure("conflict", "base_changed" if op is model.ChangeOp.REPLACE_NOTE else "anchor_missing")
    if op is model.ChangeOp.REPLACE_NOTE:
        if hashlib.sha256(before).hexdigest() != change["base_sha256"]:
            raise WriteFailure("conflict", "base_changed")
        return change["content"].encode("utf-8")
    # Keep line endings and every unaffected byte, including final-newline and blank-line conventions.
    lines = before.decode("utf-8").splitlines(keepends=True)
    bare = [line.rstrip("\r\n") for line in lines]
    if op is model.ChangeOp.REPLACE_LINE:
        if bare.count(change["old"]) != 1:
            raise WriteFailure("conflict", "anchor_missing")
        at = bare.index(change["old"])
        lines[at] = change["new"] + lines[at][len(bare[at]):]
    else:
        start, end = 0, len(lines)
        if change["under"] is not None:
            if bare.count(change["under"]) != 1:
                raise WriteFailure("conflict", "anchor_missing")
            start = bare.index(change["under"]) + 1
            heading = re.match(r"^(#{1,6})\s", change["under"])
            level = len(heading[1]) if heading else 6
            end = next((i for i in range(start, len(lines))
                        if (match := re.match(r"^(#{1,6})\s", bare[i])) and len(match[1]) <= level), len(lines))
        if change["line"] in bare[start:end]:
            return before
        at = next((i + 1 for i in range(end - 1, start - 1, -1) if bare[i].strip()), start)
        ending = "\r\n" if b"\r\n" in before else "\n"
        if at and not lines[at - 1].endswith(("\r", "\n")):
            lines[at - 1] += ending
        lines.insert(at, change["line"] + ending)
    return "".join(lines).encode("utf-8")


def replay(root: Path, changes: Sequence[Mapping[str, Any]]) -> tuple[dict[str, bytes], dict[str, bytes | None]]:
    """Preview sequential operations against current files without writing. Paths are knowledge-vault-relative."""
    before: dict[str, bytes | None] = {}
    after: dict[str, bytes] = {}
    for change in model.parse_changes(changes):
        path = change["path"]
        if path not in before:
            before[path] = _read(root, path)
        after[path] = _apply(change, after.get(path, before[path]))
    return after, before


def _replace(root: Path, path: str, data: bytes) -> None:
    """Directory-fd walk, no links, bounded regular files, atomic fsynced replacement preserving permissions."""
    fds = vault._open_real_path(root)
    try:
        for part in PurePosixPath(path).parts[:-1]:
            try:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fds[-1])
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
            fds.append(fd)
        parent, name = fds[-1], PurePosixPath(path).name
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("a memory target must be a regular file")
            mode = stat.S_IMODE(info.st_mode)
        except FileNotFoundError:
            mode = 0o644
        temp = f".looper-{secrets.token_hex(8)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), mode)
            os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=parent)
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _git_bytes(root: Path, *args: str) -> bytes:
    # vault._git strips output: blob verification must keep final newlines and arbitrary UTF-8 bytes intact.
    from .. import child_env

    done = vault._run_git(["git", "--literal-pathspecs", "-C", str(root), *args],
                          env={**child_env.inherited(), "GIT_TERMINAL_PROMPT": "0"})
    if done.returncode:
        raise subprocess.CalledProcessError(done.returncode, args)
    return done.stdout.encode("utf-8")


def _preflight(repo: Path, settings: model.MemorySettings, paths: list[str]) -> None:
    try:
        branch = vault._git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    except subprocess.CalledProcessError:
        raise WriteFailure("blocked", "detached_head") from None
    if branch != settings.branch:
        raise WriteFailure("blocked", "branch_mismatch")
    for name in memory_settings.OPERATION_FILES:
        if (repo / vault._git(repo, "rev-parse", "--git-path", name)).exists():
            raise WriteFailure("blocked", "operation_in_progress")
    if paths and vault._git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching", "--", *paths):
        raise WriteFailure("blocked", "dirty_target")


def _trailers(message: str) -> dict[str, str]:
    """Only the final trailer paragraph counts; duplicate keys are invalid, and prefixes never match ids."""
    paragraph = message.strip().rsplit("\n\n", 1)[-1]
    result = {}
    for line in paragraph.splitlines():
        key, sep, value = line.partition(": ")
        if not sep or key in result:
            return {}
        result[key] = value
    return result


def _expected_trailers(row: Mapping[str, Any]) -> dict[str, str]:
    return {"Looper-Candidate": model.format_id(row["id"]), "Looper-Revision": str(row["revision"]),
            "Looper-Attempt": str(row["attempts"]), "Looper-Content-Sha256": row["content_hash"]}


def commit_message(settings: model.MemorySettings, found: Mapping[str, Any]) -> str:
    row = found["candidate"]
    sources = []
    for source in found["sources"]:
        if source["kind"] == "journal":
            sources.append(f"任务 {source['task_id']}")
        elif source["kind"] == "lead":
            sources.append(f"线索 lead-{source['lead_id']} · {source.get('lead_chat') or '消息会话'}")
        else:
            when = source["said_at"].strftime("%Y-%m-%d %H:%M UTC") if source["said_at"] else ""
            sources.append(f"{source['channel']} · {when}（Manager 对话）")
    confirm = found["confirmation"]
    at = confirm["created_at"].strftime("%Y-%m-%d %H:%M UTC")
    trailers = "\n".join(f"{key}: {value}" for key, value in _expected_trailers(row).items())
    return f"{subject(settings, row)}\n\n来源：{'；'.join(sources)}\n确认：{at}，{confirm['via']}\n\n{trailers}\n"


def _verify(repo: Path, commit: str, expected: Mapping[str, bytes], paths: set[str],
            row: Mapping[str, Any], settings: model.MemorySettings, *, check_worktree: bool = False) -> None:
    changed = set(filter(None, vault._git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit).split("\0")))
    message = vault._git(repo, "show", "-s", "--format=%B", commit)
    if changed != paths or _trailers(message) != _expected_trailers(row) or message.splitlines()[0] != subject(settings, row):
        raise WriteFailure("blocked", "commit_mismatch")
    for path, data in expected.items():
        mode = vault._git(repo, "ls-tree", commit, "--", path).split(" ", 1)[0]
        parent_mode = vault._git(repo, "ls-tree", f"{commit}^", "--", path).split(" ", 1)[0] or "100644"
        if mode not in ("100644", "100755") or mode != parent_mode or _git_bytes(repo, "show", f"{commit}:{path}") != data:
            raise WriteFailure("blocked", "commit_mismatch")
        if check_worktree and _read(repo, path) != data:
            raise WriteFailure("blocked", "commit_mismatch")


def _names(row: Mapping[str, Any], repo: Path, settings: model.MemorySettings, runtime_root: Path):
    name = f"{model.format_id(row['id'])}-a{row['attempts']}"
    where = memory_settings.worktree_root(settings, runtime_root)
    assert where is not None
    return where / name / repo.name, _BRANCH_PREFIX + name


def _remove(repo: Path, worktree: Path, branch: str, *, merged: bool) -> None:
    """Only call for a registered, owned attempt; never remove an arbitrary directory on a guessed path."""
    try:
        vault._git(repo, "worktree", "remove", *([] if merged else ["--force"]), str(worktree))
        with contextlib.suppress(OSError):
            worktree.parent.rmdir()
    except (OSError, subprocess.SubprocessError):
        logger.warning("memory worktree cleanup deferred")
        return
    try:
        vault._git(repo, "branch", "-d" if merged else "-D", branch)
    except (OSError, subprocess.SubprocessError):
        logger.warning("memory branch cleanup deferred")


def _write(config: vault.VaultConfig, settings: model.MemorySettings, found: Mapping[str, Any],
           repo: Path, runtime_root: Path) -> str | None:
    row, revision = found["candidate"], found["revision"]
    changes = validate_changes(config, settings, row["kind"], revision["changes"])
    if model.content_hash(row["id"], subject(settings, row), changes) != row["confirmed_hash"]:
        raise WriteFailure("blocked", "commit_mismatch")
    relative_vault = config.path.relative_to(repo)
    paths = [str(relative_vault / change["path"]) for change in changes]
    _preflight(repo, settings, paths)
    try:
        # check-ignore does not support --literal-pathspecs; the existing stdin helper treats paths literally.
        ignored = memory_settings._ignored(repo, [Path(path) for path in paths], by_patterns=False)
    except RuntimeError as error:
        if isinstance(error.__cause__, subprocess.TimeoutExpired):
            raise WriteFailure("blocked", "git_timeout") from None
        raise WriteFailure("blocked", "git_failed") from None
    if ignored:
        raise WriteFailure("blocked", "vault_problem")
    where = memory_settings.worktree_root(settings, runtime_root)
    if where is None or memory_settings._worktree_problem(where, repo):
        raise WriteFailure("blocked", "vault_problem")
    base = vault._git(repo, "rev-parse", f"refs/heads/{settings.branch}")
    worktree, branch = _names(row, repo, settings, runtime_root)
    created, merged = False, False
    try:
        try:
            worktree.parent.mkdir(parents=True, exist_ok=True)
            vault._git(repo, "worktree", "add", "-b", branch, str(worktree), base)
            created = True
        except (OSError, subprocess.CalledProcessError):
            raise WriteFailure("blocked", "worktree_failed") from None
        work_config = vault.VaultConfig(worktree / relative_vault, (), config.rules_file)
        # Git does not track empty folders. The configured folders were checked in the main checkout; fd-based
        # replay/replacement below checks links and creates missing parents only for these allowed target paths.
        validate_changes(work_config, settings, row["kind"], changes)
        after, before = replay(work_config.path, changes)
        if all(data == before[path] for path, data in after.items()):
            return None
        expected = {str(relative_vault / path): data for path, data in after.items()}
        changed = {str(relative_vault / path) for path in after if after[path] != before[path]}
        for path, data in after.items():
            if data != before[path]:
                _replace(work_config.path, path, data)
        if any(_read(worktree, path) != data for path, data in expected.items()):
            raise WriteFailure("blocked", "commit_mismatch")
        vault._git(worktree, "add", "--", *sorted(changed))
        # Keep the message out of the index and the worktree: hooks cannot accidentally add it.
        message_file = Path(vault._git(worktree, "rev-parse", "--git-path", "looper-memory-message"))
        if not message_file.is_absolute():
            message_file = worktree / message_file
        fd = os.open(message_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(commit_message(settings, found))
        try:
            try:
                vault._git(worktree, "-c", "commit.gpgsign=false", "commit", "-F", str(message_file), "--", *sorted(changed))
            except subprocess.CalledProcessError as error:
                raise WriteFailure("blocked", "hook_rejected" if error.returncode == 1 else "git_failed") from None
        finally:
            message_file.unlink(missing_ok=True)
        commit = vault._git(worktree, "rev-parse", "HEAD")
        _verify(worktree, commit, expected, changed, row, settings, check_worktree=True)
        if vault._git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
            raise WriteFailure("blocked", "commit_mismatch")
        current = vault._git(repo, "rev-parse", f"refs/heads/{settings.branch}")
        if current != base:
            try:
                vault._git(repo, "diff", "--quiet", base, current, "--", *paths)
            except subprocess.CalledProcessError:
                raise WriteFailure("conflict", "target_changed") from None
            vault._git(worktree, "-c", "commit.gpgsign=false", "rebase", f"refs/heads/{settings.branch}")
            commit = vault._git(worktree, "rev-parse", "HEAD")
            _verify(worktree, commit, expected, changed, row, settings, check_worktree=True)
        _preflight(repo, settings, paths)
        try:
            vault._git(repo, "merge", "--ff-only", branch)
        except subprocess.CalledProcessError:
            _preflight(repo, settings, paths)
            raise WriteFailure("blocked", "git_failed") from None
        merged = True
        if vault._git(repo, "rev-parse", "HEAD") != commit:
            raise WriteFailure("blocked", "commit_mismatch")
        _verify(repo, commit, expected, changed, row, settings)
        return commit
    finally:
        if created:
            _remove(repo, worktree, branch, merged=merged)


def _thread_lock(config: vault.VaultConfig) -> threading.Lock:
    with vault._LOCKS_LOCK:
        return vault._LOCKS.setdefault(str(config.path), threading.Lock())


class MemoryWriteService:
    """One writer per git repository across processes, polling and immediately woken after confirmation.

    Stop waits for the current filesystem transaction, keeping both locks until its database readback is durable.
    Cancelling a to_thread future would release the advisory lock while git was still running.
    """

    def __init__(self, runtime_root: Path, *, db: Any = None, drain: Any = None):
        self.runtime_root, self.db, self.drain = runtime_root, db, drain
        self._woken = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._busy = False
        self._pass_lock = asyncio.Lock()
        self._reconciled: set[str] = set()

    def wake(self) -> None:
        self._woken.set()

    def drain_waiting(self) -> int:
        return int(self._busy)

    async def start(self) -> None:
        global _active
        if self._task is None:
            _active = self
            self._stopping = False
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        global _active
        if _active is self:
            _active = None
        self._stopping = True
        self.wake()
        if self._task is not None:
            await asyncio.shield(self._task)
            self._task = None

    async def _loop(self) -> None:
        while not self._stopping:
            self._woken.clear()
            try:
                processed = await self.run_once()
            except Exception:
                logger.warning("memory write pass failed; deferred")
                processed = False
            if not processed and not self._stopping:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._woken.wait(), TICK_SECONDS)

    @asynccontextmanager
    async def _locked(self):
        # Check the raw switch first: disabled means no filesystem inspection, git, cleanup or state transitions.
        raw = await memory_settings.stored(self.db)
        if raw is None or raw.get("enabled") is not True:
            yield None
            return
        config, _ = await vault.load()
        git_timeout = False
        if config is None:
            # A broken vault cannot supply a repository lock. Use the configured path only as a lock identity.
            key_text = "invalid-vault"
            repo = None
        else:
            try:
                repo = await asyncio.to_thread(vault._git_root_sync, config.path)
            except subprocess.TimeoutExpired:
                repo, git_timeout = None, True
            key_text = str(repo.resolve() if repo else config.path)
        key = int.from_bytes(hashlib.sha256(key_text.encode()).digest()[:8], "big", signed=True)
        pool = self.db or connection.get_pool()
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1::bigint)", key):
                yield None
                return
            try:
                # Reload after acquiring the cross-process lock: settings may have changed while waiting.
                latest = await memory_settings.stored(conn)
                latest_config, _ = await vault.load()
                if latest != raw or latest_config != config:
                    yield None
                    return
                try:
                    settings = model.check_settings(raw, config) if config else None
                    problem = None if settings else "vault_problem"
                except (ValueError, OSError):
                    settings, problem = None, "vault_problem"
                if git_timeout:
                    problem = "git_timeout"
                yield conn, config, settings, problem, repo
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1::bigint)", key)

    async def _move(self, conn, cid, to, action, **kwargs):
        async with conn.transaction():
            return await store.transition(conn, cid, to, action=action, actor="system", via="system",
                                          now=datetime.now(timezone.utc), **kwargs)

    async def _finish(self, conn, row, commit):
        async with conn.transaction():
            await store.transition(conn, row["id"], "written", action="write", actor="system", via="system",
                                   now=datetime.now(timezone.utc), commit_sha=commit)
            await conn.execute("UPDATE message_leads SET state = 'promoted', promoted_kind = 'vault_proposal', "
                               "promoted_ref = $2, superseded_by = NULL, marked_via = NULL, marked_note = NULL, "
                               "check_verdict = CASE WHEN check_verdict = 'drop' THEN NULL ELSE check_verdict END, "
                               "updated_at = now() WHERE id IN "
                               "(SELECT lead_id FROM memory_candidate_sources WHERE candidate_id = $1)",
                               row["id"], model.format_id(row["id"]))
        from . import memory_closeout
        await memory_closeout.note_written(conn, row, commit)

    async def _failure(self, conn, row, failure):
        action = {model.State.BLOCKED: "block", model.State.CONFLICT: "conflict"}[failure.state]
        await self._move(conn, row["id"], failure.state, action, reason=failure.reason, note=failure.reason.value)
        logger.warning("memory candidate=%s state=%s reason=%s", model.format_id(row["id"]), failure.state, failure.reason)
        if failure.state is model.State.CONFLICT:
            # Queue at most one automatic redraft, even across restarts; PR4 consumes drafting.
            count = await conn.fetchval("SELECT count(*) FROM memory_candidate_events WHERE candidate_id = $1 "
                                        "AND action = 'redraft' AND actor_kind = 'system'", row["id"])
            if count == 0:
                await self._move(conn, row["id"], "drafting", "redraft")

    async def run_once(self) -> bool:
        async with self._pass_lock:
            if self.drain is not None and self.drain.draining:
                return False
            raw = await memory_settings.stored(self.db)
            if raw is None or raw.get("enabled") is not True:
                return False
            self._busy = True
            task = asyncio.create_task(self._run_once())
            cancelled = False
            try:
                # Every cancellation must wait for git AND durable readback before releasing either lock.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        cancelled = True
                result = task.result()
                if cancelled:
                    raise asyncio.CancelledError
                return result
            finally:
                self._busy = False

    async def _run_once(self) -> bool:
        if self.drain is not None and self.drain.draining:
            return False
        async with self._locked() as context:
            if context is None:
                return False
            conn, config, settings, problem, repo = context
            if config is None or settings is None or repo is None:
                reason = ("git_timeout" if problem == "git_timeout" else
                          "vault_problem" if config is None or settings is None else "not_git")
                for record in await conn.fetch("SELECT id FROM memory_candidates WHERE destination='vault' AND state='writing'"):
                    async with self._candidate(conn, record["id"]) as found:
                        if found is not None:
                            await self._failure(conn, found["candidate"], WriteFailure("blocked", reason))
            if repo is not None and settings is not None:
                identity = str(repo)
                if identity not in self._reconciled:
                    await self._reconcile(conn, config, settings, repo)
                    self._reconciled.add(identity)
                await self._cleanup(conn, config, settings, repo)
            if self._stopping or (self.drain is not None and self.drain.draining):
                return False
            async with conn.transaction():
                candidate = await conn.fetchrow("SELECT id, attempts FROM memory_candidates WHERE destination = 'vault' "
                                          "AND state = 'confirmed' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1")
                if candidate is None:
                    return False
                cid = candidate["id"]
                await store.transition(conn, cid, "writing", action="claim", actor="system", via="system",
                                             now=datetime.now(timezone.utc),
                                             note=f"{_BRANCH_PREFIX}{model.format_id(cid)}-a{candidate['attempts'] + 1}")
            await self._attempt(conn, config, settings, problem, repo, cid)
            return True

    @asynccontextmanager
    async def _candidate(self, conn, cid):
        async with conn.transaction():
            # Keep the operator's two configuration rows stable across the long filesystem transaction.
            await conn.fetch("SELECT key FROM system_settings WHERE key = ANY($1::text[]) ORDER BY key FOR SHARE",
                             [model.SETTING_KEY, vault.SETTING_KEY])
            # Lock leads before the candidate: deleting a lead cascades in that order too. Keeping the candidate
            # locked through the filesystem write makes source deletion either win first, or purge a written row.
            await conn.fetch("SELECT id FROM message_leads WHERE id IN "
                             "(SELECT lead_id FROM memory_candidate_sources WHERE candidate_id = $1) "
                             "ORDER BY id FOR UPDATE", cid)
            row = await conn.fetchrow("SELECT * FROM memory_candidates WHERE id = $1 FOR UPDATE", cid)
            yield await store.get(cid, db=conn) if row is not None and row["state"] == "writing" else None

    async def _attempt(self, conn, config, settings, problem, repo, cid):
        async with self._candidate(conn, cid) as found:
            if found is None:
                return
            row = found["candidate"]
            try:
                if problem == "git_timeout":
                    raise WriteFailure("blocked", "git_timeout")
                if config is None or settings is None or problem:
                    raise WriteFailure("blocked", "vault_problem")
                if repo is None:
                    raise WriteFailure("blocked", "not_git")
                current_config, _ = await vault.load()
                raw = await memory_settings.stored(conn)
                if current_config != config or raw is None or model.check_settings(raw, config) != settings:
                    raise WriteFailure("blocked", "vault_problem")
                found["confirmation"] = dict(await conn.fetchrow("SELECT * FROM memory_candidate_events WHERE id = $1",
                                                                 row["confirmed_event_id"]))
                for source in found["sources"]:
                    if source["kind"] == "lead":
                        source["lead_chat"] = await conn.fetchval(
                            "SELECT string_agg(DISTINCT NULLIF(c.name, ''), ', ') FROM message_lead_sources s "
                            "JOIN source_events e ON e.seq = s.event_seq JOIN source_chats c "
                            "ON (c.source_id, c.chat_id) = (e.source_id, e.chat_id) WHERE s.lead_id = $1",
                            source["lead_id"])

                def write():
                    with _thread_lock(config):
                        return _write(config, settings, found, repo, self.runtime_root)

                commit = await asyncio.to_thread(write)
                if commit is None:
                    await self._move(conn, cid, "dropped", "drop", reason="duplicate")
                else:
                    await self._finish(conn, row, commit)
            except WriteFailure as error:
                await self._failure(conn, row, error)
            except subprocess.TimeoutExpired:
                await self._failure(conn, row, WriteFailure("blocked", "git_timeout"))
            except subprocess.CalledProcessError:
                await self._failure(conn, row, WriteFailure("blocked", "git_failed"))
            except (ValueError, OSError):
                await self._failure(conn, row, WriteFailure("blocked", "vault_problem"))

    async def reconcile(self) -> None:
        async with self._locked() as context:
            if context is not None:
                conn, config, settings, _, repo = context
                if repo is not None and settings is not None:
                    await self._reconcile(conn, config, settings, repo)

    async def _reconcile(self, conn, config, settings, repo):
        # Implemented below, sharing the exact verifier used before and after a normal merge.
        rows = await conn.fetch("SELECT * FROM memory_candidates WHERE destination = 'vault' AND state = 'writing'")
        for record in rows:
            async with self._candidate(conn, record["id"]) as found:
                if found is None:
                    continue
                row = found["candidate"]
                try:
                    commit = await asyncio.to_thread(_recover_commit, config, settings, found, repo)
                    if commit:
                        await self._finish(conn, row, commit)
                    elif row["attempts"] < 3:
                        await self._move(conn, row["id"], "confirmed", "recover")
                    else:
                        await self._failure(conn, row, WriteFailure("blocked", "interrupted"))
                except WriteFailure as error:
                    await self._failure(conn, row, error)
                except subprocess.TimeoutExpired:
                    await self._failure(conn, row, WriteFailure("blocked", "git_timeout"))
                except subprocess.CalledProcessError:
                    await self._failure(conn, row, WriteFailure("blocked", "git_failed"))
                except (ValueError, OSError):
                    await self._failure(conn, row, WriteFailure("blocked", "vault_problem"))

    async def cleanup(self) -> None:
        async with self._locked() as context:
            if context is not None:
                conn, config, settings, _, repo = context
                if repo is not None and settings is not None:
                    await self._cleanup(conn, config, settings, repo)

    async def _cleanup(self, conn, config, settings, repo):
        active = await conn.fetch("SELECT id, attempts FROM memory_candidates WHERE destination = 'vault' "
                                  "AND state = 'writing'")
        keep = {f"{model.format_id(row['id'])}-a{row['attempts']}" for row in active}
        def clean():
            with _thread_lock(config):
                try:
                    _cleanup(repo, settings, self.runtime_root, keep)
                except (OSError, subprocess.SubprocessError):
                    logger.warning("memory cleanup deferred")
        await asyncio.to_thread(clean)


def _recover_commit(config, settings, found, repo) -> str | None:
    row = found["candidate"]
    with _thread_lock(config):
        changes = validate_changes(config, settings, row["kind"], found["revision"]["changes"])
        if model.content_hash(row["id"], subject(settings, row), changes) != row["confirmed_hash"]:
            raise WriteFailure("blocked", "commit_mismatch")
        log = vault._git(repo, "log", "-500", "--format=%H%x00%B%x00", f"refs/heads/{settings.branch}")
        fields = log.split("\0")
        commit = None
        for at in range(0, len(fields) - 1, 2):
            trailers = _trailers(fields[at + 1])
            if trailers.get("Looper-Candidate") == model.format_id(row["id"]) and trailers.get("Looper-Attempt") == str(row["attempts"]):
                commit = fields[at].strip()
                if trailers != _expected_trailers(row):
                    raise WriteFailure("blocked", "commit_mismatch")
                break
        if commit is None:
            return None
        relative = config.path.relative_to(repo)
        before = {}
        after = {}
        for change in changes:
            path = str(relative / change["path"])
            if path not in before:
                entry = vault._git(repo, "ls-tree", f"{commit}^", "--", path)
                before[path] = _git_bytes(repo, "show", f"{commit}^:{path}") if entry else None
            try:
                after[path] = _apply(change, after.get(path, before[path]))
            except WriteFailure:
                raise WriteFailure("blocked", "commit_mismatch") from None
        changed = {path for path in after if after[path] != before[path]}
        _verify(repo, commit, after, changed, row, settings)
        if vault._git(repo, "rev-parse", "HEAD") != commit:
            raise WriteFailure("blocked", "commit_mismatch")
        for path, data in after.items():
            if _git_bytes(repo, "show", f"HEAD:{path}") != data:
                raise WriteFailure("blocked", "commit_mismatch")
        return commit


def _cleanup(repo, settings, runtime_root, keep):
    root = memory_settings.worktree_root(settings, runtime_root)
    if root is None or memory_settings._worktree_problem(root, repo):
        return
    for record in vault._git(repo, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.split(" ", 1) for line in record.splitlines() if " " in line)
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        name = branch.removeprefix(_BRANCH_PREFIX)
        if not branch.startswith(_BRANCH_PREFIX) or not _ATTEMPT.fullmatch(name) or name in keep:
            continue
        worktree = Path(fields.get("worktree", "/"))
        expected = root / name / repo.name
        if worktree != expected or model._passes_link(root, str(worktree.relative_to(root))):
            continue
        _remove(repo, worktree, branch, merged=False)
    vault._git(repo, "worktree", "prune")
    for branch in vault._git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/looper/memory/").splitlines():
        name = branch.removeprefix(_BRANCH_PREFIX)
        if _ATTEMPT.fullmatch(name) and name not in keep:
            try:
                vault._git(repo, "branch", "-D", branch)
            except subprocess.CalledProcessError:
                logger.warning("memory branch cleanup deferred")
