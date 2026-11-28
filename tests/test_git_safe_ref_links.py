"""``git_safe`` refuses a repository whose git directory holds a symbolic link where git reads refs or its own files.

Every row of a table puts one link into an otherwise ordinary repository. ``check_repository`` walks everything
(the check at the boundaries of a turn or step); ``run`` and ``spawn`` make the fixed check before each command, and
the tests of it first run one command (``_judged``), so that the repository's first full walk is behind them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents import git_safe, ref_links, step_workspaces
from looper.agents.tiers import worker

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
    git_safe._walked.clear()
    yield
    git_safe._memo.clear()
    git_safe._walked.clear()


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


def test_links_where_git_reads_neither_refs_nor_its_own_files_are_not_refused(repo, tmp_path):
    (repo / ".git" / "hooks" / "pre-push").symlink_to(tmp_path)  # inside hooks and objects: not walked
    (repo / ".git" / "objects" / "extra").symlink_to(tmp_path)
    (repo / "refs").symlink_to(tmp_path)  # a tracked or untracked link in the work tree
    assert git_safe.check_repository(repo) is not None
    assert safe(repo, "for-each-ref").returncode == 0


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


# The linked directory, and a writing command.
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
def test_a_writing_command_is_refused_and_leaves_the_linked_place_unchanged(repo, tmp_path, place, command):
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
    plain(repo, *args)  # positive control: the command does write there
    assert _tree(outside) != before


# --- The fixed check before each command ---


def _judged(directory: Path) -> None:
    """The repository's first judgement (a full walk) is behind: what follows is the check before each command."""
    assert safe(directory, "rev-parse", "HEAD").returncode == 0


def _command_refused(directory: Path, args: tuple[str, ...], place: str | None = None) -> None:
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(directory, *args)
    assert refused.value.reason == "ref_symlink", args
    if place is not None:
        assert refused.value.path == f".git/{place}", args
    assert MARKER not in str(refused.value).replace(f".git/{place}", ""), args


@pytest.mark.parametrize("place", ["refs/dwim", "refs/tags/dwim", "refs/heads/dwim", "refs/remotes/dwim",
                                   "refs/remotes/dwim/HEAD"])
def test_a_link_on_any_name_a_short_ref_may_resolve_to_is_refused(repo, place):
    _judged(repo)
    _arm(repo, place)
    _command_refused(repo, ("rev-parse", "--verify", "-q", "dwim"), place)


@pytest.mark.parametrize("command", [
    ("checkout", "-q", "leaf"), ("switch", "-q", "leaf"), ("branch", "-d", "leaf"),
    ("worktree", "add", "-q", "--detach", "{tmp}/another", "HEAD"),
])
def test_a_command_that_reads_other_worktrees_heads_is_refused_for_a_linked_one(repo, tmp_path, command):
    _judged(repo)
    _arm(repo, "worktrees/wt/HEAD")
    _command_refused(repo, tuple(part.replace("{tmp}", str(tmp_path)) for part in command), "worktrees/wt/HEAD")
    assert not (tmp_path / "another").exists()


@pytest.mark.parametrize("entry", ["HEAD", "refs", "logs", "gitdir", "commondir"])
def test_each_entry_of_another_worktree_that_git_reads_is_checked(repo, entry):
    _judged(repo)
    _arm(repo, f"worktrees/wt/{entry}")
    _command_refused(repo, ("for-each-ref",), f"worktrees/wt/{entry}")


REFLOG_READS = [
    ("rev-parse", "main@{1}"), ("rev-parse", "main@{upstream}"), ("rev-parse", "@{-1}"), ("log", "-g", "-1"),
    ("log", "--walk-reflogs", "-1"), ("rev-list", "--reflog", "--max-count=1"), ("reflog",),
    ("--no-pager", "reflog", "show", "main"), ("stash", "list"),
]


@pytest.mark.parametrize("command", REFLOG_READS)
def test_a_command_that_reads_reflogs_walks_everything(repo, command):
    _judged(repo)
    _arm(repo, "logs/refs/heads/main")
    assert safe(repo, "rev-parse", "main").returncode == 0  # control: the fixed check does not go that deep
    _command_refused(repo, command, "logs/refs/heads/main")


PSEUDO_REFS = ["FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD"]


@pytest.mark.parametrize("name", PSEUDO_REFS)
def test_a_linked_root_pseudo_ref_is_refused(repo, name):
    _judged(repo)
    _arm(repo, name)
    _command_refused(repo, ("rev-parse", "--verify", "-q", name), name)


@pytest.mark.parametrize("name", PSEUDO_REFS)
def test_a_linked_root_pseudo_ref_of_the_worktree_the_command_runs_in_is_refused(repo, name):
    tree = repo.parent / "wt"
    _judged(tree)
    _arm(repo, f"worktrees/wt/{name}")
    _command_refused(tree, ("rev-parse", "--verify", "-q", name), f"worktrees/wt/{name}")


@pytest.mark.parametrize("place", ["config.worktree", "shallow", "index", "config", "description", "hooks", "objects",
                                   "info", "info/exclude", "info/attributes", "info/sparse-checkout", "info/grafts",
                                   "a-file-git-may-add"])
def test_a_link_at_an_entry_of_the_root_or_info_is_refused(repo, place):
    _judged(repo)
    _arm(repo, place)
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(repo, "status", "--porcelain")
    assert refused.value.reason == "ref_symlink" and refused.value.path == f".git/{place}"
    _refused(repo, place)  # the full walk refuses what the fixed check does


@pytest.mark.parametrize("place", ["shallow", "info/grafts"])
def test_a_linked_file_git_quotes_in_its_errors_is_refused_without_its_content(repo, place, caplog, capfd):
    _judged(repo)
    _arm(repo, place)
    quoted = subprocess.run(["git", "-C", str(repo), "log", "-1"], env=_ENV, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=30)
    assert MARKER in quoted.stderr  # positive control: git alone prints the line
    with caplog.at_level(logging.DEBUG), pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(repo, "log", "-1")
    assert refused.value.path == f".git/{place}"
    captured = capfd.readouterr()
    assert MARKER not in f"{refused.value}{refused.value!r}{vars(refused.value)}{caplog.text}{captured}"


COMMON = [("rev-parse", "HEAD"), ("status",), ("log", "-1"), ("for-each-ref",), ("diff", "--cached"),
          ("commit", "-q", "--allow-empty", "-m", "c"), ("branch",)]


@pytest.mark.parametrize("seed", range(24))
def test_any_entry_of_the_root_or_info_replaced_by_a_link_refuses_every_command(repo, tmp_path, seed, caplog, capfd):
    git_dir = repo / ".git"
    for name in ("shallow", "ORIG_HEAD", "info/attributes", "info/grafts", "info/sparse-checkout"):
        (git_dir / name).touch()
    entries = sorted(str(path.relative_to(git_dir)) for top in (git_dir, git_dir / "info") for path in top.iterdir())
    place = random.Random(seed).choice(entries)
    print(f"seed {seed}: {place}")
    _judged(repo)
    marker = tmp_path / "marker"
    marker.write_text(f"{MARKER}\n")
    path = git_dir / place
    shutil.rmtree(path) if path.is_dir() else path.unlink()
    path.symlink_to(marker)
    capfd.readouterr()
    for command in COMMON:
        returned = None
        with caplog.at_level(logging.DEBUG), pytest.raises(git_safe.RepositoryRefused) as refused:
            returned = safe(repo, *command)
        error = refused.value
        assert returned is None
        assert MARKER not in f"{error}{error!r}{vars(error)}{caplog.text}", (seed, place, command)
    captured = capfd.readouterr()
    assert MARKER not in captured.out + captured.err
    assert marker.read_text() == f"{MARKER}\n"


def test_a_reading_command_leaves_what_lies_deeper_to_the_full_walk(repo):
    _judged(repo)
    for place in ("logs/refs/heads/main", "worktrees/wt/ORIG_HEAD", "worktrees/wt/refs/bisect/bad"):
        _arm(repo, place)
    assert safe(repo, "for-each-ref").returncode == 0  # the documented residual
    with pytest.raises(git_safe.RepositoryRefused):
        git_safe.check_repository(repo)  # the boundary
    _command_refused(repo, ("for-each-ref",))  # and from then on every command, until it is repaired


def test_a_repository_is_walked_in_full_when_it_is_first_judged(repo):
    _arm(repo, "logs/refs/heads/main")
    for _ in range(2):  # a refused walk does not count as the first
        _command_refused(repo, ("for-each-ref",), "logs/refs/heads/main")


def test_a_directory_git_no_longer_reads_as_a_repository_is_still_checked(repo):
    _judged(repo)
    _arm(repo, "HEAD")
    for directory in (repo, repo / "module" / ".."):
        _command_refused(directory, ("status",), "HEAD")


# The branch checked out in the linked worktree, or named by the command, has a link on its reflog.
REF_WRITES = [
    ("commit", "-q", "--allow-empty", "-m", "c"), ("reset", "-q", "--soft", "HEAD"),
    ("update-ref", "refs/heads/sub/deep/x", "HEAD"), ("branch", "-D", "sub/deep/x"),
    ("worktree", "add", "-q", "{tmp}/another", "sub/deep/x"), ("-c", "a.b=c", "checkout", "-q", "sub/deep/x"),
    ("update-ref", "--stdin"), ("fetch", "-q", "."),
]


@pytest.mark.parametrize("command", REF_WRITES)
@pytest.mark.parametrize("component", ["logs/refs/heads/sub", "logs/refs/heads/sub/deep/x"])
def test_a_command_that_writes_refs_checks_the_reflogs_it_writes(repo, tmp_path, command, component):
    tree = repo.parent / "wt"
    plain(tree, "checkout", "-q", "sub/deep/x")
    if "worktree" in command or "checkout" in command:
        plain(tree, "checkout", "-q", "--detach")
    _judged(tree)
    _arm(repo, component)
    assert safe(tree, "for-each-ref").returncode == 0  # control: a read passes
    _command_refused(tree, tuple(part.replace("{tmp}", str(tmp_path)) for part in command), component)


def test_a_command_that_writes_refs_walks_the_reflogs_of_its_own_worktree(repo):
    tree = repo.parent / "wt"
    _judged(tree)
    _arm(repo, "worktrees/wt/logs/refs/bisect/bad")
    _command_refused(tree, ("commit", "-q", "--allow-empty", "-m", "c"), "worktrees/wt/logs/refs/bisect/bad")


def test_a_long_argument_of_a_writing_command_is_not_taken_for_a_broken_repository(repo):
    _judged(repo)
    assert safe(repo, "commit", "-q", "--allow-empty", "-m", "m" * 5000 + "/../x\n\nbody").returncode == 0


# --- Grafts ---


def test_a_graft_file_changes_nothing_and_prints_nothing(repo):
    head = plain(repo, "rev-parse", "HEAD")
    (repo / ".git" / "info" / "grafts").write_text(f"{head}\n")  # the head has no parent, says the file
    done = safe(repo, "rev-list", "--count", "HEAD")
    assert (done.stdout.strip(), done.stderr) == ("2", "")
    assert git_safe.environment({})["GIT_GRAFT_FILE"] == os.devnull


# --- The Worker's step boundaries ---


def _envelope(work_dir: Path):
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address

    return MessageEnvelope.create(source=planner_address("t"), target=worker_address("worker-t"),
                                  message_type=MessageType.WORK_ITEM, payload={"context": {"work_dir": str(work_dir)}})


@pytest.mark.parametrize("review", [False, True])
def test_a_step_does_not_start_in_a_repository_the_full_walk_refuses(repo, review):
    _judged(repo)
    _arm(repo, "logs/refs/heads/main")  # deeper than the check before a command goes
    pool = worker.WorkerPool.__new__(worker.WorkerPool)
    result = asyncio.run(pool._prepare(_envelope(repo), is_verify=review))
    assert isinstance(result, dict)
    text = str(result)
    assert ".git/logs/refs/heads/main" in text and MARKER not in text.replace(".git/logs/refs/heads/main", "")
    assert result["passed"] is False if review else result["status"] == "failed"


@pytest.mark.parametrize("review", [False, True])
def test_a_step_that_ends_in_a_repository_the_full_walk_refuses_fails(repo, review, monkeypatch):
    _judged(repo)
    pool = worker.WorkerPool.__new__(worker.WorkerPool)
    ctx = SimpleNamespace(work_dir=str(repo), verification=None, is_verify=review, parent_task_id="",
                          worker_id="worker-t", fallback_reason="start_error")
    done = {"passed": True, "findings": []} if review else {"status": "completed", "summary": "done"}

    async def consumed(*args, **kwargs):
        return dict(done)

    monkeypatch.setattr(pool, "_consume_once", consumed, raising=False)

    def consume():
        return asyncio.run(pool._consume(ctx, None, None, start=worker.ConsumeStart.NEW))

    assert consume() == done  # positive control
    _arm(repo, "logs/refs/heads/main")
    result = consume()
    assert result["passed"] is False if review else result["status"] == "failed"
    assert ".git/logs/refs/heads/main" in str(result) and ctx.fallback_reason is None
