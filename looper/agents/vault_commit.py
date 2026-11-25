"""One confined, verified vault commit and fast-forward transaction for every writer."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import secrets
import stat
import threading
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from . import vault

logger = logging.getLogger(__name__)


class CommitState(StrEnum):
    COMMITTED = "committed"
    UNCHANGED = "unchanged"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CommitResult:
    state: CommitState
    commit: str | None = None
    reason: str | None = None
    branch: str | None = None
    worktree: Path | None = None


class TransactionBlocked(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def default_worktree_root() -> Path:
    return Path.home() / "worktrees" / "looper-vault"


def check_worktree_root(root: Path, repo: Path, runtime_root: Path | None = None) -> Path:
    if not root.is_absolute():
        raise ValueError("worktree_root must be absolute")
    resolved = root.resolve()
    if resolved.is_relative_to(repo.resolve()):
        raise ValueError("worktree_root must be outside the vault repository")
    if runtime_root is not None and resolved.is_relative_to(runtime_root.resolve()):
        raise ValueError("worktree_root must be outside the runtime directory")
    # Do not let a configured path acquire symlink components between validation and creation.
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("worktree_root must not pass through symbolic links")
    return resolved


def thread_lock(config: vault.VaultConfig):
    with vault._LOCKS_LOCK:
        return vault._LOCKS.setdefault(str(config.path), threading.Lock())


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
            data = stream.read(2 * 1024 * 1024 + 64 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024 + 64 * 1024 or b"\0" in data:
            raise ValueError("a memory target must be bounded UTF-8 text")
        data.decode("utf-8")
        return data
    finally:
        for fd in reversed(fds):
            os.close(fd)


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
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), mode)
            os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            os.close(fd)
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


def _remove(repo: Path, worktree: Path, branch: str, *, merged: bool) -> None:
    """Only call for a registered, owned attempt; never remove an arbitrary directory on a guessed path."""
    try:
        vault._git(repo, "worktree", "remove", *([] if merged else ["--force"]), str(worktree))
        with contextlib.suppress(OSError):
            worktree.parent.rmdir()
    except (OSError, subprocess.SubprocessError):
        logger.warning("vault worktree cleanup deferred")
        return
    try:
        vault._git(repo, "branch", "-d" if merged else "-D", branch)
    except (OSError, subprocess.SubprocessError):
        logger.warning("vault branch cleanup deferred")



def _preflight(repo: Path, branch: str, paths: list[str]) -> None:
    from . import memory_settings
    try:
        current = vault._git(repo, 'symbolic-ref', '--short', '-q', 'HEAD')
    except subprocess.CalledProcessError:
        raise TransactionBlocked('detached_head') from None
    if current != branch:
        raise TransactionBlocked('branch_mismatch')
    for name in memory_settings.OPERATION_FILES:
        if (repo / vault._git(repo, 'rev-parse', '--git-path', name)).exists():
            raise TransactionBlocked('operation_in_progress')
    if paths and vault._git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                            '--ignored=matching', '--', *paths):
        raise TransactionBlocked('dirty_target')


def _verify(repo: Path, commit: str, expected: Mapping[str, bytes], changed: set[str], message: str,
            *, check_worktree: bool = False) -> None:
    paths = set(filter(None, vault._git(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z', commit).split('\0')))
    if paths != changed or vault._git(repo, 'show', '-s', '--format=%B', commit) != message.strip():
        raise TransactionBlocked('commit_mismatch')
    for path, data in expected.items():
        mode = vault._git(repo, 'ls-tree', commit, '--', path).split(' ', 1)[0]
        parent_mode = vault._git(repo, 'ls-tree', f'{commit}^', '--', path).split(' ', 1)[0] or '100644'
        if mode not in ('100644', '100755') or mode != parent_mode or _git_bytes(repo, 'show', f'{commit}:{path}') != data:
            raise TransactionBlocked('commit_mismatch')
        if check_worktree and _read(repo, path) != data:
            raise TransactionBlocked('commit_mismatch')


def _transaction(config: vault.VaultConfig, paths: list[str], message: str, *,
                prepare: Callable, runtime_root: Path | None = None, worktree_root: Path | None = None,
                worktree: Path | None = None, branch: str | None = None, checked_branch: str | None = None,
                preflight: Callable | None = None, verify: Callable | None = None,
                refresh_on_advance: bool = False) -> CommitResult:
    """Synchronous transaction; preparation and caller-specific verification run under the shared locks.

    A blocked fast-forward preserves its owned branch and worktree for operator recovery.
    Nothing modifies another checkout's index, working files or history except a successful fast-forward.
    """
    with thread_lock(config):
        for path in paths:
            if vault._path_problem(path):
                raise ValueError("vault commit paths must be relative, visible and confined")
            _read(config.path, path)  # recheck the live root and every target without following links
        repo = vault._git_root_sync(config.path)
        if repo is None:
            return CommitResult(CommitState.BLOCKED, reason='not_git')
        relative = config.path.relative_to(repo)
        repo_paths = [str(relative / path) for path in paths]
        where = check_worktree_root(worktree_root or config.worktree_root or default_worktree_root(), repo, runtime_root)
        name = f'vault-{secrets.token_hex(8)}'
        worktree = worktree or where / name / repo.name
        branch = branch or f'looper/vault/{name}'
        # The same repository lock also serializes different configured subdirectories and processes.
        lock_path = Path(vault._git(repo, 'rev-parse', '--git-common-dir'))
        if not lock_path.is_absolute():
            lock_path = repo / lock_path
        fd = os.open(lock_path / 'looper-vault.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            def attempt(refreshes=0):
                try:
                    target_branch = checked_branch or vault._git(repo, 'symbolic-ref', '--short', '-q', 'HEAD')
                except subprocess.CalledProcessError:
                    return CommitResult(CommitState.BLOCKED, reason='detached_head')
                check = preflight or (lambda: _preflight(repo, target_branch, repo_paths))
                base = vault._git(repo, 'rev-parse', 'HEAD')
                created = merged = preserve = False
                commit_sha = None
                try:
                    if preflight is None:
                        _preflight(repo, target_branch, [])
                    else:
                        check()
                    worktree.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        vault._git(repo, 'worktree', 'add', '-b', branch, str(worktree), base)
                        created = True
                    except (OSError, subprocess.CalledProcessError):
                        raise TransactionBlocked('worktree_failed') from None
                    after, before = prepare(vault.VaultConfig(worktree / relative, (), config.rules_file))
                    expected = {str(relative / path): data for path, data in after.items()}
                    changed = {str(relative / path) for path in after if after[path] != before[path]}
                    if not changed:
                        return CommitResult(CommitState.UNCHANGED, branch=branch, worktree=worktree)
                    for path, data in after.items():
                        if data != before[path]:
                            _replace(worktree / relative, path, data)
                    if any(_read(worktree, path) != data for path, data in expected.items()):
                        raise TransactionBlocked('commit_mismatch')
                    vault._git(worktree, 'add', '--', *sorted(changed))
                    message_file = Path(vault._git(worktree, 'rev-parse', '--git-path', 'looper-memory-message'))
                    if not message_file.is_absolute():
                        message_file = worktree / message_file
                    msg_fd = os.open(message_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    try:
                        with os.fdopen(msg_fd, 'w', encoding='utf-8', closefd=False) as stream:
                            stream.write(message)
                    finally:
                        os.close(msg_fd)
                    try:
                        try:
                            vault._git(worktree, '-c', 'commit.gpgsign=false', 'commit', '-F', str(message_file), '--', *sorted(changed))
                        except subprocess.CalledProcessError as error:
                            raise TransactionBlocked('hook_rejected' if error.returncode == 1 else 'git_failed') from None
                    finally:
                        message_file.unlink(missing_ok=True)
                    commit_sha = vault._git(worktree, 'rev-parse', 'HEAD')
                    checker = verify or (lambda root, sha, exp, changed, **kw: _verify(root, sha, exp, changed, message, **kw))
                    checker(worktree, commit_sha, expected, changed, check_worktree=True)
                    if vault._git(worktree, 'status', '--porcelain=v1', '--untracked-files=all'):
                        raise TransactionBlocked('commit_mismatch')
                    current = vault._git(repo, 'rev-parse', 'HEAD')
                    if refresh_on_advance and current != base:
                        try:
                            vault._git(repo, 'diff', '--quiet', base, current, '--', *repo_paths)
                        except subprocess.CalledProcessError:
                            raise TransactionBlocked('target_changed') from None
                        if refreshes < 3:
                            # The memory writer replays a disjoint update on the latest base without rebasing
                            # or rewriting the operator's history. Its confirmation and verification run again.
                            _remove(repo, worktree, branch, merged=False)
                            created = False
                            return attempt(refreshes + 1)
                    preserve = True
                    marker = Path(vault._git(worktree, 'rev-parse', '--git-path', 'looper-vault-blocked'))
                    if not marker.is_absolute():
                        marker = worktree / marker
                    marker.touch()
                    check()
                    modes = {}
                    for path in expected:
                        try:
                            modes[path] = stat.S_IMODE((repo / path).lstat().st_mode)
                        except FileNotFoundError:
                            pass
                    # Preserve before invoking Git: timeout and failure both need operator recovery.
                    preserve = True
                    try:
                        vault._git(repo, 'merge', '--ff-only', branch)
                    except subprocess.CalledProcessError:
                        raise TransactionBlocked('git_failed') from None
                    merged = True
                    if vault._git(repo, 'rev-parse', 'HEAD') != commit_sha:
                        raise TransactionBlocked('commit_mismatch')
                    checker(repo, commit_sha, expected, changed)
                    for path, mode in modes.items():
                        # Do not follow a link introduced concurrently by another editor.
                        target_fd = os.open(repo / path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        try:
                            os.fchmod(target_fd, mode)
                        finally:
                            os.close(target_fd)
                    if any(_read(repo, path) != data for path, data in expected.items()):
                        raise TransactionBlocked('commit_mismatch')
                    preserve = False
                    marker.unlink(missing_ok=True)
                    return CommitResult(CommitState.COMMITTED, commit_sha, branch=branch, worktree=worktree)
                except TransactionBlocked as exc:
                    return CommitResult(CommitState.BLOCKED, commit_sha, exc.reason, branch if preserve else None,
                                        worktree if preserve else None)
                except subprocess.CalledProcessError:
                    return CommitResult(CommitState.BLOCKED, commit_sha, 'git_failed', branch if preserve else None,
                                        worktree if preserve else None)
                except subprocess.TimeoutExpired:
                    return CommitResult(CommitState.BLOCKED, commit_sha, 'git_timeout', branch if preserve else None,
                                        worktree if preserve else None)
                finally:
                    if created and not preserve:
                        _remove(repo, worktree, branch, merged=merged)
            return attempt()
        finally:
            os.close(fd)


def transaction(config: vault.VaultConfig, paths: list[str], message: str, **kwargs) -> CommitResult:
    try:
        return _transaction(config, paths, message, **kwargs)
    except subprocess.TimeoutExpired:
        return CommitResult(CommitState.BLOCKED, reason="git_timeout")
    except subprocess.CalledProcessError:
        return CommitResult(CommitState.BLOCKED, reason="git_failed")


async def finish(pending):
    """Cancellation waits for filesystem changes and durable operator notification."""
    task = asyncio.create_task(pending)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def commit(config: vault.VaultConfig, files: Mapping[str, bytes], message: str, *,
                 runtime_root: Path | None = None, worktree_root: Path | None = None,
                 expected: Mapping[str, bytes | None] | None = None) -> CommitResult:
    for path, data in files.items():
        if vault._path_problem(path):
            raise ValueError('vault commit paths must be relative, visible and confined')
        if not isinstance(data, bytes) or b'\0' in data:
            raise ValueError('vault commit requires UTF-8 text bytes')
        data.decode('utf-8')
    def prepare(work_config):
        before = {path: _read(work_config.path, path) for path in files}
        if expected is not None and any(before[path] != expected[path] for path in files):
            raise ValueError('vault target changed since export')
        return dict(files), before
    async def run():
        result = await asyncio.to_thread(transaction, config, list(files), message, prepare=prepare,
                                         runtime_root=runtime_root, worktree_root=worktree_root)
        if result.state is CommitState.BLOCKED and result.branch:
            from . import vault_commit_attention
            repo = await asyncio.to_thread(vault._git_root_sync, config.path)
            await vault_commit_attention.record(result, repo=repo)
        return result
    return await finish(run())
