"""Explorer snapshots preserve the spawning checkout, including its dirty files."""
from pathlib import Path
import subprocess

import pytest

from looper.agents.explorer_checkout import CheckoutError, prepare, remove


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True).stdout


def test_snapshot_includes_staged_unstaged_untracked_and_deletions(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    git(repo, 'config', 'user.name', 'Test')
    (repo / 'module').mkdir()
    for name in ('edited', 'deleted', 'staged'):
        (repo / 'module' / name).write_text('committed')
    git(repo, 'add', 'module')
    git(repo, 'commit', '-m', 'baseline')
    (repo / 'module/staged').write_text('staged')
    git(repo, 'add', 'module/staged')
    (repo / 'module/staged').write_text('unstaged after stage')
    (repo / 'module/edited').write_text('dirty')
    (repo / 'module/deleted').unlink()
    (repo / 'module/untracked').write_text('new')
    branch, status = git(repo, 'symbolic-ref', 'HEAD'), git(repo, 'status', '--porcelain=v1')
    index = (repo / '.git/index').read_bytes()
    checkout = prepare(repo / 'module', tmp_path / 'explorers/spawn')
    try:
        assert checkout.cwd == checkout.root / 'module'
        assert (checkout.cwd / 'edited').read_text() == 'dirty'
        assert (checkout.cwd / 'staged').read_text() == 'unstaged after stage'
        assert (checkout.cwd / 'untracked').read_text() == 'new'
        assert not (checkout.cwd / 'deleted').exists()
        assert (repo / '.git/index').read_bytes() == index
        assert git(repo, 'symbolic-ref', 'HEAD') == branch
        assert git(repo, 'status', '--porcelain=v1') == status
        (checkout.cwd / 'edited').write_text('isolated')
        assert (repo / 'module/edited').read_text() == 'dirty'
    finally:
        remove(checkout)
    assert not checkout.root.exists()


def test_non_repository_refused_and_failure_leaves_no_checkout(tmp_path):
    target = tmp_path / 'explorers/spawn'
    with pytest.raises(CheckoutError):
        prepare(tmp_path, target)
    assert not target.exists()


@pytest.mark.parametrize('tracked,target_kind', [
    (True, 'file'), (False, 'directory'), (False, 'missing'),
])
def test_snapshot_refuses_symlinks_without_changing_source(tmp_path, tracked, target_kind):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init')
    (repo / 'ordinary').write_text('source')
    git(repo, 'add', 'ordinary')
    outside = tmp_path / 'outside'
    if target_kind == 'file':
        outside.write_text('private')
    elif target_kind == 'directory':
        outside.mkdir()
        (outside / 'private').write_text('private')
    link = repo / 'link\nwith-newline'
    link.symlink_to(outside, target_is_directory=target_kind == 'directory')
    if tracked:
        git(repo, 'add', '--', link.name)
    status = git(repo, 'status', '--porcelain=v1')
    index = (repo / '.git/index').read_bytes()
    target = tmp_path / 'explorers/spawn'
    with pytest.raises(CheckoutError, match='symbolic links'):
        prepare(repo, target)
    assert not target.exists()
    assert link.is_symlink() and link.readlink() == outside
    assert (repo / '.git/index').read_bytes() == index
    assert git(repo, 'status', '--porcelain=v1') == status


def test_snapshot_failure_cleans_partial_target(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init')
    from looper.agents import explorer_checkout
    original = explorer_checkout._git
    def fail_checkout(directory, *args, **kwargs):
        if args[0] == 'checkout-index':
            raise CheckoutError('snapshot failed')
        return original(directory, *args, **kwargs)
    monkeypatch.setattr(explorer_checkout, '_git', fail_checkout)
    target = tmp_path / 'explorers/spawn'
    with pytest.raises(CheckoutError):
        prepare(repo, target)
    assert not target.exists()


def test_recovery_cleanup_preserves_kept_spawns(tmp_path):
    from looper.agents.explorer_checkout import remove_abandoned
    root = tmp_path / 'explorers'
    for name in ('done', 'kept'):
        (root / name).mkdir(parents=True)
        (root / name / 'file').write_text(name)
    remove_abandoned(root, keep={'kept'})
    assert not (root / 'done').exists()
    assert (root / 'kept/file').read_text() == 'kept'
