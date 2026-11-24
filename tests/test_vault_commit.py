"""Shared commit transactions use only temporary synthetic repositories."""
import asyncio
import subprocess
import threading
from unittest.mock import AsyncMock

import pytest

from looper.agents import vault, vault_commit
from looper.agents.vault_commit import default_worktree_root as original_default_worktree_root
from tests.memory_support import synthetic_vault


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def configured(tmp_path):
    repo = synthetic_vault(tmp_path)
    return vault.VaultConfig(repo, (), None, worktree_root=tmp_path / 'worktrees')


def test_transaction_commits_only_explicit_files_and_removes_worktree(configured):
    repo = configured.path
    (repo / 'other.md').write_text('operator staged change\n')
    git(repo, 'add', '--', 'other.md')
    result = asyncio.run(vault_commit.commit(configured, {'mem/new.md': b'new\r\n'}, 'Synthetic source'))
    assert result.state is vault_commit.CommitState.COMMITTED
    assert result.commit == git(repo, 'rev-parse', 'HEAD')
    assert (repo / 'mem/new.md').read_bytes() == b'new\r\n'
    assert git(repo, 'diff', '--cached', '--name-only') == 'other.md'
    assert git(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD') == 'mem/new.md'
    assert not result.worktree.exists()
    assert not git(repo, 'branch', '--list', result.branch)


def test_ff_block_keeps_owned_branch_and_notifies_without_touching_operator_work(configured, monkeypatch):
    from looper.agents import vault_commit_attention
    repo = configured.path
    original = vault._git
    notify = AsyncMock()
    monkeypatch.setattr(vault_commit_attention, 'record', notify)
    def dirty_before_merge(root, *args):
        if args[:2] == ('merge', '--ff-only'):
            (repo / 'mem/new.md').write_bytes(b'operator unsaved\n')
        return original(root, *args)
    monkeypatch.setattr(vault, '_git', dirty_before_merge)
    head = git(repo, 'rev-parse', 'HEAD')
    result = asyncio.run(vault_commit.commit(configured, {'mem/new.md': b'new\n'}, 'Synthetic source'))
    assert result.state is vault_commit.CommitState.BLOCKED
    assert result.worktree.exists() and git(repo, 'rev-parse', result.branch) == result.commit
    assert git(repo, 'rev-parse', 'HEAD') == head
    assert (repo / 'mem/new.md').read_bytes() == b'operator unsaved\n'
    assert git(repo, 'stash', 'list') == ''
    notify.assert_awaited_once_with(result, repo=repo)


def test_concurrent_transactions_are_serialized(configured, monkeypatch):
    original = vault._git
    entered, release = threading.Event(), threading.Event()
    commits = []
    def paused(root, *args):
        if 'commit' in args:
            commits.append(root)
            if len(commits) == 1:
                entered.set()
                assert release.wait(5)
        return original(root, *args)
    monkeypatch.setattr(vault, '_git', paused)
    async def race():
        first = asyncio.create_task(vault_commit.commit(configured, {'mem/a.md': b'a'}, 'a'))
        assert await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(vault_commit.commit(configured, {'mem/b.md': b'b'}, 'b'))
        await asyncio.sleep(.05)
        assert len(commits) == 1
        release.set()
        return await asyncio.gather(first, second)
    results = asyncio.run(race())
    assert all(r.state is vault_commit.CommitState.COMMITTED for r in results)
    assert git(configured.path, 'rev-list', '--count', 'HEAD') == '3'


def test_default_and_configured_worktree_roots_are_outside_vault_and_runtime(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(vault_commit.Path, 'home', lambda: tmp_path / 'home')
    default = original_default_worktree_root()
    assert default == tmp_path / 'home/worktrees/looper-vault'
    for root in (configured.path / 'trees', tmp_path / 'runtime/trees'):
        with pytest.raises(ValueError):
            vault_commit.check_worktree_root(root, configured.path, tmp_path / 'runtime')


def test_expected_base_refuses_concurrent_overwrite(configured):
    with pytest.raises(ValueError, match='changed'):
        asyncio.run(vault_commit.commit(configured, {'other.md': b'replacement'}, 'source', expected={'other.md': None}))
    assert (configured.path / 'other.md').read_text() == 'Unrelated synthetic note\n'
