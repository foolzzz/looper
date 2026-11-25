"""Blocked shared commits remain visible through the real operator summary."""
import subprocess

import pytest

from looper.agents import attention, vault, vault_commit, vault_commit_attention
from tests.db_support import DSN, run_db
from tests.memory_support import synthetic_vault

pytestmark = pytest.mark.skipif(not DSN, reason='LOOPER_TEST_DATABASE_URL is not set')


def test_dirty_checkout_keeps_transaction_and_durable_attention(monkeypatch, tmp_path):
    repo = synthetic_vault(tmp_path)
    config = vault.VaultConfig(repo, (), None, worktree_root=tmp_path / 'worktrees')
    target = repo / 'other.md'
    target.write_bytes(b'operator uncommitted change\n')
    before = subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'])
    async def check(pool):
        result = await vault_commit.commit(config, {'other.md': b'new committed note\n'}, 'Synthetic source')
        assert result.state is vault_commit.CommitState.BLOCKED and result.reason == 'dirty_target'
        assert result.worktree.exists()
        assert (result.worktree / 'other.md').read_bytes() == b'new committed note\n'
        notices = await vault_commit_attention.pending(db=pool)
        assert len(notices) == 1 and notices[0]['branch'] == result.branch
        summary = await attention.collect()
        section = next(s for s in summary['sections'] if s['kind'] == 'vault_commits')
        assert section['state'] == 'ok' and section['count'] == 1
        assert section['items'][0]['facts']['branch'] == result.branch
        assert target.read_bytes() == b'operator uncommitted change\n'
        assert subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain']) == before
        assert subprocess.check_output(['git', '-C', str(repo), 'stash', 'list']) == b''
        # A service restart uses the durable notice, and does not clean up the owned attempt.
        assert (await vault_commit_attention.pending(db=pool))[0]['commit'] == result.commit
    run_db(check, monkeypatch, timeout=40)


def test_worktree_setting_roundtrips_and_refuses_vault_internal_root(monkeypatch, tmp_path):
    from looper.agents.projects import RequestError
    repo = synthetic_vault(tmp_path)
    async def check(pool):
        value = {'path': str(repo), 'worktree_root': str(tmp_path / 'worktrees')}
        saved = await vault.update_settings(value)
        assert saved['worktree_root'] == value['worktree_root']
        assert (await vault.active()).worktree_root == tmp_path / 'worktrees'
        assert not (tmp_path / 'worktrees').exists()
        with pytest.raises(RequestError) as failure:
            await vault.update_settings({**value, 'worktree_root': str(repo / 'trees')})
        assert failure.value.status == "400 Bad Request"
        assert (await vault.settings_view())['worktree_root'] == value['worktree_root']
        assert not (await vault.update_settings({"path": None, "worktree_root": None}))["configured"]
    run_db(check, monkeypatch)
