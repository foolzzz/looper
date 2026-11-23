"""Disposable tree snapshots for explorers whose runtime only restricts tools.

A private index captures the working tree without changing the spawner's index,
branch or stash. The checkout has no repository administration or branch.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


class CheckoutError(RuntimeError):
    """The explorer cannot be isolated from its spawner's checkout."""


@dataclass(frozen=True)
class Checkout:
    repo: Path
    root: Path
    cwd: Path


def _git(directory: Path, *args: str, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(['git', '-C', str(directory), *args], capture_output=True,
                                text=True, check=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckoutError('cannot create explorer checkout') from exc
    return result.stdout.strip()


def prepare(work_dir: Path, target: Path) -> Checkout:
    """Snapshot committed and dirty content at ``target``; refuse outside git.

    Run this blocking filesystem operation in a thread from an async caller.
    The caller owns the target and must remove it on every terminal outcome.
    """
    work_dir, target = work_dir.resolve(), target.absolute()
    repo = repository(work_dir)
    relative = work_dir.relative_to(repo)
    target.mkdir(parents=True, exist_ok=False)
    checkout = Checkout(repo, target, target / relative)
    try:
        with tempfile.TemporaryDirectory(prefix='looper-explorer-index-') as directory:
            env = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory) / 'index')}
            # An empty tree also supports an initialized repository with no commit.
            try:
                _git(repo, 'read-tree', 'HEAD', env=env)
            except CheckoutError:
                _git(repo, 'read-tree', '--empty', env=env)
            _git(repo, 'add', '-A', '--', '.', env=env)
            # Inspect the staged snapshot before extraction, including dangling
            # links and links to directories outside the source repository.
            entries = _git(repo, 'ls-files', '--stage', '-z', env=env).split('\0')
            if any(entry.startswith('120000 ') for entry in entries):
                raise CheckoutError('explorer snapshots cannot contain symbolic links')
            tree = _git(repo, 'write-tree', env=env)
            _git(repo, 'read-tree', tree, env=env)
            _git(repo, 'checkout-index', '--all', f'--prefix={target}{os.sep}', env=env)
        return checkout
    except BaseException:
        remove(checkout)
        raise


def remove(checkout: Checkout) -> None:
    """Remove a complete or partial snapshot; repeated cleanup is harmless."""
    if checkout.root.exists():
        shutil.rmtree(checkout.root)


def repository(work_dir: Path) -> Path:
    """Resolve the source repository or refuse isolation outside git."""
    return Path(_git(work_dir.resolve(), 'rev-parse', '--show-toplevel')).resolve()


def remove_abandoned(root: Path, *, keep: Iterable[str] = ()) -> None:
    """Reclaim snapshots after recovery has identified active or kept spawns."""
    if not root.exists():
        return
    kept = set(keep)
    for directory in root.iterdir():
        if directory.name in kept:
            continue
        if directory.is_symlink():
            directory.unlink()
        elif directory.is_dir():
            shutil.rmtree(directory)
