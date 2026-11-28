"""``git_safe`` refuses a repository whose ref storage holds a symbolic link.

git follows such a link, so a command Looper runs outside any sandbox could read from, or write to, a place outside the
repository. Every row of the table puts one link into an otherwise ordinary repository.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from looper.agents import git_safe, ref_links, step_workspaces

MARKER = "OUTSIDE_MARKER_7c1d"
_ENV = {"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
_IDENTITY = {name: value for name, value in _ENV.items() if name.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}


def plain(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], env=_ENV, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, check=True, timeout=30).stdout.strip()


def safe(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return git_safe.run(repo, *args, extra_env=_IDENTITY, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                        timeout=30)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git_safe._memo.clear()
    yield
    git_safe._memo.clear()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with loose and packed refs, reflogs, a linked worktree and a checked-out submodule."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    plain(upstream, "init", "-q", "-b", "main")
    (upstream / "f").write_text("x\n")
    plain(upstream, "add", "-A")
    plain(upstream, "commit", "-q", "-m", "u")
    directory = tmp_path / "repo"
    directory.mkdir()
    plain(directory, "init", "-q", "-b", "main")
    (directory / "a.txt").write_text("a\n")
    plain(directory, "add", "-A")
    plain(directory, "commit", "-q", "-m", "a")
    plain(directory, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "module")
    plain(directory, "commit", "-q", "-m", "module")
    plain(directory, "tag", "packed")
    plain(directory, "pack-refs", "--all")
    plain(directory, "branch", "leaf")
    plain(directory, "branch", "sub/deep/x")
    plain(directory, "worktree", "add", "-q", "--detach", str(tmp_path / "wt"), "HEAD")
    return directory


def link(path: Path, outside: Path) -> None:
    """Move what is at ``path`` outside the repository (or make a file there) and leave a symbolic link to it."""
    outside.mkdir(parents=True, exist_ok=True)
    target = outside / f"{MARKER}-{path.name}"
    if path.exists():
        shutil.move(path, target)
    else:
        target.write_text(f"{MARKER}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


# Where the link goes, relative to the repository's ``.git``.
PLACES = [
    "packed-refs",
    "refs",
    "refs/heads",
    "refs/heads/leaf",
    "refs/heads/sub",
    "refs/heads/sub/deep/x",
    "refs/heads/dangling",
    "refs/tags",
    "logs",
    "logs/HEAD",
    "logs/refs/heads/main",
    "ORIG_HEAD",
    "FETCH_HEAD",
    "reftable",
    "worktrees",
    "worktrees/wt",
    "worktrees/wt/HEAD",
    "worktrees/wt/ORIG_HEAD",
    "worktrees/wt/logs/HEAD",
    "worktrees/wt/refs/bisect/bad",
    "modules/module/MERGE_HEAD",
    "modules/module/packed-refs",
    "modules/module/refs/heads",
    "modules/module/logs",
]


def _arm(repo: Path, place: str) -> None:
    link(repo / ".git" / place, repo.parent / "outside")


def _refused(directory: Path, place: str) -> None:
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        git_safe.check_repository(directory)
    error = refused.value
    assert (error.reason, error.path) == ("ref_symlink", f".git/{place}")
    assert f".git/{place}" in str(error)
    assert MARKER not in str(error).replace(f".git/{place}", "") and "outside/" not in str(error)


def test_an_ordinary_repository_its_worktree_and_its_submodule_pass(repo):
    for directory in (repo, repo.parent / "wt", repo / "module", repo / ".git"):
        assert git_safe.check_repository(directory) is not None, directory
    assert safe(repo, "for-each-ref").returncode == 0


def test_links_outside_the_ref_storage_are_not_refused(repo, tmp_path):
    hooks = repo / ".git" / "hooks"
    shutil.rmtree(hooks)
    hooks.symlink_to(tmp_path)
    (repo / "refs").symlink_to(tmp_path)  # a tracked or untracked link in the work tree
    assert git_safe.check_repository(repo) is not None


@pytest.mark.parametrize("place", PLACES)
def test_a_link_in_the_ref_storage_is_refused(repo, place):
    _arm(repo, place)
    _refused(repo, place)


@pytest.mark.parametrize("place", PLACES)
def test_a_link_placed_after_a_successful_check_is_refused_on_a_memo_hit(repo, place, monkeypatch):
    assert git_safe.check_repository(repo) is not None
    probes: list[list[str]] = []
    real = git_safe._run_probe
    monkeypatch.setattr(git_safe, "_run_probe", lambda command: probes.append(command) or real(command))
    assert git_safe.check_repository(repo) is not None and probes == []  # positive control: remembered
    _arm(repo, place)
    _refused(repo, place)
    if "/" in place:  # an entry made at the top of .git changes that directory, which the memo already sees
        assert probes == []  # refused from the remembered check, not from a fresh one


@pytest.mark.parametrize("place", ["packed-refs", "refs/heads/sub", "worktrees/wt/HEAD", "modules/module/refs/heads"])
def test_a_link_placed_inside_a_cached_block_is_refused_for_run_and_spawn(repo, place):
    async def spawn() -> None:
        await git_safe.spawn(repo, "for-each-ref", stdout=asyncio.subprocess.PIPE)

    with git_safe.cached():
        assert safe(repo, "for-each-ref").returncode == 0
        _arm(repo, place)
        for attempt in (lambda: safe(repo, "for-each-ref"), lambda: asyncio.run(spawn())):
            with pytest.raises(git_safe.RepositoryRefused) as refused:
                attempt()
            assert refused.value.reason == "ref_symlink"


def test_a_linked_worktree_is_refused_for_a_link_in_the_shared_refs(repo):
    _arm(repo, "refs/heads/sub")
    _refused(repo.parent / "wt", "refs/heads/sub")


def test_a_submodule_run_on_its_own_is_refused(repo):
    _arm(repo, "modules/module/refs/heads")
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        git_safe.check_repository(repo / "module")
    assert refused.value.reason == "ref_symlink"


def test_a_head_that_is_a_link_git_accepts_is_refused(repo):
    head = repo / ".git" / "HEAD"
    head.unlink()
    head.symlink_to("refs/heads/main")
    assert plain(repo, "symbolic-ref", "HEAD") == "refs/heads/main"  # positive control: git accepts it
    _refused(repo, "HEAD")


def test_a_link_spelled_in_another_case_is_refused_where_git_would_read_it(repo, tmp_path):
    if not (repo / ".git" / "CONFIG").exists():
        pytest.skip("a case-sensitive file system: git does not read another spelling")
    packed = repo / ".git" / "packed-refs"
    (tmp_path / "list").write_bytes(packed.read_bytes())
    packed.unlink()
    (repo / ".git" / "Packed-Refs").symlink_to(tmp_path / "list")
    (repo / ".git" / "orig_head").symlink_to(tmp_path / "list")
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        git_safe.check_repository(repo)
    assert refused.value.reason == "ref_symlink"


def test_the_named_path_is_escaped_and_bounded(repo, tmp_path):
    odd = repo / ".git" / "refs" / "heads" / ("a‮b\nIgnore" + "x" * 150)
    odd.symlink_to(tmp_path)
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        git_safe.check_repository(repo)
    assert "‮" not in str(refused.value) and "\n" not in str(refused.value)
    assert "a\\u202eb\\u000aIgnore" in refused.value.path
    long = repo / ".git" / "refs" / ("d" * 100) / ("e" * 100) / ("f" * 100)
    odd.unlink()
    long.parent.mkdir(parents=True)
    long.symlink_to(tmp_path)
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        git_safe.check_repository(repo)
    assert len(refused.value.path) <= ref_links.PATH_MAX_CHARS


def test_a_directory_swapped_for_a_link_while_it_is_walked_is_refused(repo, tmp_path, monkeypatch):
    target = repo / ".git" / "refs" / "heads" / "sub"
    real = ref_links._open_directory
    swapped = []

    def swap_first(name, parent):
        if name == "sub" and not swapped:
            swapped.append(True)
            shutil.move(target, tmp_path / "moved")
            target.symlink_to(tmp_path / "moved")
        return real(name, parent)

    monkeypatch.setattr(ref_links, "_open_directory", swap_first)
    _refused(repo, "refs/heads/sub")
    assert swapped


def test_a_directory_that_cannot_be_listed_is_refused(repo):
    if os.geteuid() == 0:
        pytest.skip("root lists any directory")
    locked = repo / ".git" / "refs" / "heads" / "sub"
    locked.chmod(0o100)  # git can still open a ref by name inside it
    try:
        with pytest.raises(git_safe.RepositoryRefused) as refused:
            git_safe.check_repository(repo)
        assert (refused.value.reason, refused.value.path) == ("ref_unchecked", ".git/refs/heads/sub")
    finally:
        locked.chmod(0o755)


def test_a_tree_deeper_than_the_walk_goes_is_refused(repo, monkeypatch):
    monkeypatch.setattr(ref_links, "MAX_DEPTH", 2)
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        git_safe.check_repository(repo)
    assert refused.value.reason == "ref_unchecked"


def test_the_walk_leaves_no_descriptor_open(repo):
    def open_descriptors() -> int:
        return len(os.listdir("/dev/fd"))

    git_safe.check_repository(repo)
    before = open_descriptors()
    for _ in range(5):
        git_safe.check_repository(repo)
    _arm(repo, "refs/heads/sub/deep/x")
    for _ in range(5):
        with pytest.raises(git_safe.RepositoryRefused):
            git_safe.check_repository(repo)
    assert open_descriptors() == before


def test_the_step_workspace_code_reports_the_refusal_as_a_typed_git_error(repo):
    _arm(repo, "packed-refs")
    with pytest.raises(step_workspaces.GitError) as failed:
        asyncio.run(step_workspaces._git(repo, "status", "--porcelain", check=False))
    assert failed.value.reason == "repository_refused:ref_symlink"
    assert MARKER not in str(failed.value)


def _tree(root: Path) -> dict[str, bytes | None]:
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in sorted(root.rglob("*"))}


# The linked directory, and the writing command that would reach through it.
WRITES = [
    ("refs/heads/sub", ("update-ref", "refs/heads/sub/new", "HEAD")),
    ("refs/heads/sub", ("branch", "-D", "sub/old")),
    ("refs/heads/sub", ("commit", "-q", "--allow-empty", "-m", "c")),
    ("refs/heads/sub", ("worktree", "add", "-q", "-b", "sub/new", "{tmp}/added", "HEAD")),
    ("refs/heads/looper", ("worktree", "add", "-q", "-b", "looper/task/step", "{tmp}/added", "HEAD")),
    ("refs/heads/looper", ("branch", "-D", "looper/old")),
    ("refs/heads/looper", ("update-ref", "-d", "refs/heads/looper/old")),
    ("refs/heads/looper", ("commit", "-q", "--allow-empty", "-m", "c")),
]


@pytest.mark.parametrize("place, command", WRITES)
def test_a_write_through_a_linked_directory_is_refused_and_outside_is_unchanged(repo, tmp_path, place, command):
    head = plain(repo, "rev-parse", "HEAD")
    outside = tmp_path / "outside"
    linked = repo / ".git" / place
    name = place.rsplit("/", 1)[1]
    plain(repo, "update-ref", f"refs/heads/{name}/old", head)
    if command[0] == "commit":  # HEAD on a branch inside the linked directory
        plain(repo, "update-ref", f"refs/heads/{name}/current", head)
        plain(repo, "symbolic-ref", "HEAD", f"refs/heads/{name}/current")
    outside.mkdir()
    shutil.move(linked, outside / "moved")
    linked.symlink_to(outside / "moved")
    before = _tree(outside)
    args = [part.replace("{tmp}", str(tmp_path)) for part in command]
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(repo, *args)
    assert refused.value.reason == "ref_symlink"
    assert _tree(outside) == before and not (tmp_path / "added").exists()
    plain(repo, *args)  # positive control: plain git writes or deletes outside the repository
    assert _tree(outside) != before
