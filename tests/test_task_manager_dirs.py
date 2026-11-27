"""Directory checks and the outside lock for a direct task Manager session."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from looper.agents.task_manager_dirs import (
    DirectoryLock,
    Kind,
    LockHeld,
    Problem,
    Reason,
    admit,
    check,
    inspect,
    trusted_paths,
)
from looper.agents import task_manager_dirs, task_manager_safety
from looper.agents.tiers import task_manager as tm
from looper.ops_config import FILE

_IN_PLACE = {"workspace_mode": "in_place"}
_WORKTREE_TEXT = (
    "v1 runs the task Manager's direct mode only for in-place tasks; this task works in its own worktree. "
    "Use the background (orchestrated) mode for it."
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    return path


def _commit(path: Path) -> None:
    (path / "README").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-q", "--no-gpg-sign", "-m", "seed")


def _real(path: Path | str) -> str:
    return os.path.realpath(path)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, dict[str, Path]]:
    """Scratch home, Claude config and runtime, each in its own directory."""
    home = tmp_path / "home"
    claude = tmp_path / "only-claude" / "config"
    xdg = tmp_path / "only-xdg" / "config"
    runtime = tmp_path / "only-runtime" / "root"
    environment = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude),
        "XDG_CONFIG_HOME": str(xdg),
    }
    expected = {
        "ssh": home / ".ssh",
        "aws": home / ".aws",
        "gnupg": home / ".gnupg",
        "config": xdg,
        "credentials": claude / ".credentials.json",
        "claude_json": claude / ".claude.json",
        "runtime": runtime,
        "ops_file": runtime / FILE,
        "claude_dir": claude,
    }
    return environment, runtime, expected


def _check(tmp_path: Path, work_dir: Path | str, **overrides: object) -> Problem | None:
    environment = overrides.pop("environment", {"HOME": str(tmp_path / "home")})
    runtime_root = overrides.pop("runtime_root", tmp_path / "runtime")
    task = overrides.pop("task", _IN_PLACE)
    live = overrides.pop("live", [])
    assert not overrides
    return check(task, str(work_dir), environment=environment, runtime_root=runtime_root, live=live)


def test_a_repository_root_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    assert _check(tmp_path, repo) is None


def test_the_operator_home_is_refused_even_when_it_is_a_repository_root(tmp_path: Path) -> None:
    home = _repo(tmp_path / "home")
    problem = _check(tmp_path, home, environment={"HOME": str(home)})
    assert problem == Problem(
        Reason.WORKSPACE_IS_HOME, None,
        f"{_real(home)} is the operator's home directory. Open the task on a repository that is not the home directory.")


def test_a_system_directory_is_refused_and_a_scratch_directory_is_not(tmp_path: Path) -> None:
    for work_dir in (Path("/usr/bin"), Path("/")):
        problem = _check(tmp_path, work_dir)
        assert problem is not None
        assert problem.reason is Reason.WORKSPACE_IN_SYSTEM_DIR
        assert problem.kind is None
        assert _real(work_dir) in problem.text
        assert "system directory" in problem.text
    repo = _repo(tmp_path / "repo")
    assert _check(tmp_path, repo) is None


@pytest.mark.parametrize("name", (
    "ssh", "aws", "gnupg", "config", "credentials", "claude_json", "runtime", "ops_file", "claude_dir",
))
def test_each_trusted_path_is_protected_even_when_missing(name: str, tmp_path: Path) -> None:
    environment, runtime, expected = _layout(tmp_path)
    entry = _real(expected[name])
    found = trusted_paths(environment, runtime)
    assert entry in {_real(path) for path in found}
    place = Path(entry)
    match = next(path for path in found if place == path or place in path.parents)
    problem = check(_IN_PLACE, entry, environment=environment, runtime_root=runtime, live=[])
    assert problem == Problem(
        Reason.WORKSPACE_CONTAINS_TRUSTED_CONFIG, None,
        f"{place} contains trusted configuration {match}. "
        f"Open the task on a repository that does not contain {match}.")


def test_ancestors_of_trusted_configuration_are_refused_and_a_sibling_passes(tmp_path: Path) -> None:
    environment, runtime, expected = _layout(tmp_path)
    claude_parent = expected["claude_dir"].parent
    runtime_parent = runtime.parent
    for work_dir in (claude_parent, runtime_parent, runtime):
        problem = check(_IN_PLACE, str(work_dir), environment=environment, runtime_root=runtime, live=[])
        assert problem is not None
        assert problem.reason is Reason.WORKSPACE_CONTAINS_TRUSTED_CONFIG
        assert _real(work_dir) in problem.text
    sibling = _repo(tmp_path / "sibling")
    assert check(_IN_PLACE, str(sibling), environment=environment, runtime_root=runtime, live=[]) is None


def test_default_config_locations_are_protected_when_the_environment_does_not_override_them(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    environment = {"HOME": str(home)}
    found = {_real(path) for path in trusted_paths(environment, runtime)}
    for entry in (
        home / ".ssh", home / ".aws", home / ".gnupg", home / ".config",
        home / ".claude" / ".credentials.json", home / ".claude.json", home / ".claude",
        runtime, runtime / FILE,
    ):
        assert _real(entry) in found


def test_a_subdirectory_a_bare_repository_and_a_plain_directory_are_not_a_repository_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    child = repo / "sub"
    child.mkdir()
    subdirectory = _check(tmp_path, child)
    assert subdirectory == Problem(
        Reason.WORKSPACE_NOT_REPO_ROOT, Kind.SUBDIRECTORY,
        f"{_real(child)} is a subdirectory of the git repository {_real(repo)}. Open the task on the repository root.")

    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "--bare", "-q")
    bare_problem = _check(tmp_path, bare)
    assert bare_problem is not None
    assert bare_problem.reason is Reason.WORKSPACE_NOT_REPO_ROOT
    assert bare_problem.kind is Kind.BARE
    assert _real(bare) in bare_problem.text
    assert "non-bare" in bare_problem.text

    plain = tmp_path / "plain"
    plain.mkdir()
    plain_problem = _check(tmp_path, plain)
    assert plain_problem == Problem(
        Reason.WORKSPACE_NOT_REPO_ROOT, Kind.NOT_GIT,
        f"{_real(plain)} is not a git repository. Open the task on the root of a non-bare git repository.")


def test_a_linked_worktree_and_a_symlinked_git_directory_are_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _commit(repo)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked))
    linked_problem = _check(tmp_path, linked)
    assert linked_problem is not None
    assert linked_problem.reason is Reason.WORKTREE_UNSUPPORTED
    assert linked_problem.kind is Kind.LINKED_WORKTREE
    assert _real(linked) in linked_problem.text
    assert "shared .git" in linked_problem.text

    other = _repo(tmp_path / "other")
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / ".git").symlink_to(other / ".git", target_is_directory=True)
    alias_problem = _check(tmp_path, alias)
    assert alias_problem is not None
    assert alias_problem.reason is Reason.WORKTREE_UNSUPPORTED
    assert alias_problem.kind is Kind.LINKED_WORKTREE


def test_a_worktree_task_is_refused_before_any_other_check(tmp_path: Path) -> None:
    home = _repo(tmp_path / "home")
    problem = _check(tmp_path, "/", task={"workspace_mode": "worktree"}, environment={"HOME": str(home)})
    assert problem == Problem(
        Reason.WORKTREE_UNSUPPORTED, Kind.WORKSPACE_MODE,
        f"/: {_WORKTREE_TEXT}")


def test_an_overlapping_live_session_is_refused_and_a_sibling_passes(tmp_path: Path) -> None:
    parent = _repo(tmp_path / "parent")
    child = parent / "child"
    child.mkdir()
    _git(child, "init", "-q")
    sibling = _repo(tmp_path / "sibling")
    contained = _check(tmp_path, child, live=[("t2", str(parent))])
    containing = _check(tmp_path, parent, live=[("t2", str(child))])
    same = _check(tmp_path, parent, live=[("t2", str(parent))])
    for problem, work_dir in ((contained, child), (containing, parent), (same, parent)):
        assert problem is not None
        assert problem.reason is Reason.WORKSPACE_OVERLAPS_SESSION
        assert problem.kind is None
        assert "t2" in problem.text
        assert _real(work_dir) in problem.text
    assert _check(tmp_path, sibling, live=[("t2", str(parent))]) is None


def _flip_name(name: str) -> str:
    for index, char in enumerate(name):
        if char.isalpha():
            flipped = char.lower() if char.isupper() else char.upper()
            return name[:index] + flipped + name[index + 1:]
    raise AssertionError(f"no letter to flip in {name}")


def _case_variant(path: Path) -> Path:
    return path.with_name(_flip_name(path.name))


def _same_directory(path: Path, variant: Path) -> bool:
    try:
        left, right = path.stat(), variant.stat()
    except OSError:
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _skip_when_case_sensitive(tmp_path: Path) -> None:
    probe = tmp_path / "CaseFoldProbe"
    probe.mkdir()
    if not _same_directory(probe, _case_variant(probe)):
        pytest.skip("the filesystem under tmp_path is case-sensitive")


def _assert_same_directory(path: Path, variant: Path) -> None:
    assert str(path) != str(variant)
    assert _same_directory(path, variant)


def test_a_case_variant_of_the_home_directory_is_refused(tmp_path: Path) -> None:
    _skip_when_case_sensitive(tmp_path)
    home = _repo(tmp_path / "home")
    variant = _case_variant(home)
    _assert_same_directory(home, variant)
    problem = _check(tmp_path, variant, environment={"HOME": str(home)})
    assert problem == Problem(
        Reason.WORKSPACE_IS_HOME, None,
        f"{_real(variant)} is the operator's home directory. "
        "Open the task on a repository that is not the home directory.")


def test_a_case_variant_of_a_system_directory_is_refused(tmp_path: Path) -> None:
    _skip_when_case_sensitive(tmp_path)
    system = Path("/usr/bin")
    variant = Path("/USR/bin")
    _assert_same_directory(system, variant)
    problem = _check(tmp_path, variant)
    assert problem is not None
    assert problem.reason is Reason.WORKSPACE_IN_SYSTEM_DIR
    assert problem.kind is None
    assert _real(variant) in problem.text
    assert "system directory" in problem.text


def test_a_case_variant_of_a_trusted_config_ancestor_is_refused(tmp_path: Path) -> None:
    _skip_when_case_sensitive(tmp_path)
    environment, runtime, expected = _layout(tmp_path)
    ancestor = expected["claude_dir"].parent
    ancestor.mkdir(parents=True)
    variant = _case_variant(ancestor)
    _assert_same_directory(ancestor, variant)
    problem = check(_IN_PLACE, str(variant), environment=environment, runtime_root=runtime, live=[])
    assert problem is not None
    assert problem.reason is Reason.WORKSPACE_CONTAINS_TRUSTED_CONFIG
    assert _real(variant) in problem.text


def test_a_case_variant_of_a_live_session_overlaps(tmp_path: Path) -> None:
    _skip_when_case_sensitive(tmp_path)
    parent = _repo(tmp_path / "parent")
    variant = _case_variant(parent)
    _assert_same_directory(parent, variant)
    child = parent / "child"
    child.mkdir()
    _git(child, "init", "-q")
    contained = _check(tmp_path, child, live=[("t2", str(variant))])
    containing = _check(tmp_path, variant, live=[("t2", str(child))])
    for problem, work_dir in ((contained, child), (containing, variant)):
        assert problem is not None
        assert problem.reason is Reason.WORKSPACE_OVERLAPS_SESSION
        assert problem.kind is None
        assert "t2" in problem.text
        assert _real(work_dir) in problem.text


def test_a_second_lock_on_a_case_variant_is_refused(tmp_path: Path) -> None:
    _skip_when_case_sensitive(tmp_path)
    repo = _repo(tmp_path / "repo")
    variant = _case_variant(repo)
    _assert_same_directory(repo, variant)
    runtime = tmp_path / "runtime"
    first = DirectoryLock.acquire(runtime, str(repo), "t1")
    try:
        with pytest.raises(LockHeld) as raised:
            DirectoryLock.acquire(runtime, str(variant), "t2")
        assert raised.value.holder == "t1"
        locks = list((runtime / "task-manager-locks").iterdir())
        assert len(locks) == 1
        st = repo.stat()
        assert locks[0].name == f"{st.st_dev}-{st.st_ino}.lock"
    finally:
        first.release()


def _lock_path(runtime: Path, work_dir: Path) -> Path:
    st = os.stat(work_dir)
    return runtime / "task-manager-locks" / f"{st.st_dev}-{st.st_ino}.lock"


def test_the_directory_lock_names_the_holder_and_lives_outside_the_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    first = DirectoryLock.acquire(runtime, str(repo), "t1")
    lock_path = _lock_path(runtime, repo)
    assert lock_path.is_file()
    assert runtime in lock_path.parents
    assert repo not in lock_path.parents
    assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert lock_path.read_text(encoding="utf-8") == "t1"
    with pytest.raises(LockHeld) as raised:
        DirectoryLock.acquire(runtime, str(repo), "t2")
    assert raised.value.holder == "t1"
    first.release()
    assert lock_path.is_file()
    second = DirectoryLock.acquire(runtime, str(repo), "t2")
    assert lock_path.read_text(encoding="utf-8") == "t2"
    second.release()


def test_a_lock_held_by_a_child_process_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    lock_path = _lock_path(runtime, repo)
    lock_path.parent.mkdir(parents=True)
    held = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.ftruncate(fd, 0)\n"
        "os.write(fd, b'child')\n"
        "sys.stdout.write('ready\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", held, str(lock_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        with pytest.raises(LockHeld) as raised:
            DirectoryLock.acquire(runtime, str(repo), "t1")
        assert raised.value.holder == "child"
    finally:
        process.kill()
        process.wait(timeout=5)


def _inspect(tmp_path: Path, work_dir: Path | str, live=()):
    return inspect(_IN_PLACE, str(work_dir), environment={"HOME": str(tmp_path / "home")},
                   runtime_root=tmp_path / "runtime", live=live)


def _admit(tmp_path: Path, work_dir: Path | str, expected, live=()):
    return admit(_IN_PLACE, str(work_dir), "t1", environment={"HOME": str(tmp_path / "home")},
                 runtime_root=tmp_path / "runtime", live=live, expected=expected)


def test_a_checked_directory_is_admitted_with_its_lock(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    problem, identity = _inspect(tmp_path, repo)
    assert problem is None and identity == (os.stat(repo).st_dev, os.stat(repo).st_ino)
    lock, refused = _admit(tmp_path, repo, identity)
    assert refused is None and lock is not None
    with pytest.raises(LockHeld):
        DirectoryLock.acquire(tmp_path / "runtime", str(repo), "t2")
    lock.release()


def test_a_directory_replaced_after_the_check_is_refused_and_its_lock_is_released(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _, identity = _inspect(tmp_path, repo)
    repo.rename(tmp_path / "repo.old")
    _repo(tmp_path / "repo")  # a different directory now has the same path
    lock, problem = _admit(tmp_path, repo, identity)
    assert lock is None
    assert problem is not None and problem.reason is Reason.WORKSPACE_UNAVAILABLE
    assert problem.kind is Kind.CHANGED_AFTER_CHECK
    DirectoryLock.acquire(tmp_path / "runtime", str(repo), "again").release()


def test_a_path_swapped_for_a_symlink_to_the_home_directory_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    home = _repo(tmp_path / "home")
    _, identity = _inspect(tmp_path, repo)
    repo.rename(tmp_path / "repo.old")
    repo.symlink_to(home, target_is_directory=True)
    lock, problem = _admit(tmp_path, repo, identity)
    assert lock is None and problem is not None
    assert problem.reason is Reason.WORKSPACE_UNAVAILABLE and problem.kind is Kind.CHANGED_AFTER_CHECK


def test_the_whole_check_runs_again_under_the_lock(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _, identity = _inspect(tmp_path, repo)
    (repo / ".git").rename(repo / ".git.old")  # same directory, no longer a repository root
    lock, problem = _admit(tmp_path, repo, identity)
    assert lock is None and problem is not None
    assert problem.reason is Reason.WORKSPACE_NOT_REPO_ROOT and problem.kind is Kind.NOT_GIT
    DirectoryLock.acquire(tmp_path / "runtime", str(repo), "again").release()


def test_a_directory_that_vanished_is_a_typed_refusal(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _, identity = _inspect(tmp_path, repo)
    repo.rename(tmp_path / "repo.old")
    lock, problem = _admit(tmp_path, repo, identity)
    assert lock is None and problem is not None
    assert problem.reason is Reason.WORKSPACE_UNAVAILABLE and problem.kind is Kind.NOT_A_DIRECTORY


def test_an_os_error_while_locking_or_checking_is_a_typed_refusal(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path / "repo")
    _, identity = _inspect(tmp_path, repo)

    def refuse(*args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(DirectoryLock, "acquire", classmethod(refuse))
    lock, problem = _admit(tmp_path, repo, identity)
    assert lock is None and problem is not None
    assert problem.reason is Reason.WORKSPACE_UNAVAILABLE and problem.kind is Kind.IO_ERROR
    assert "PermissionError" in problem.text
    monkeypatch.undo()
    monkeypatch.setattr(task_manager_dirs, "check", refuse)
    problem, identity = _inspect(tmp_path, repo)
    assert identity is None and problem is not None and problem.kind is Kind.IO_ERROR
    lock, problem = _admit(tmp_path, repo, (0, 0))
    assert lock is None and problem is not None and problem.kind is Kind.CHANGED_AFTER_CHECK
    DirectoryLock.acquire(tmp_path / "runtime", str(repo), "again").release()


def test_the_directory_reasons_stay_in_step_with_the_safety_and_refusal_enums() -> None:
    reasons = {reason.value for reason in Reason}
    assert reasons <= {reason.value for reason in task_manager_safety.Reason}
    assert reasons <= {reason.value for reason in tm.RefuseReason}
    workspace = {value for value in (r.value for r in task_manager_safety.Reason)
                 if value.startswith(("workspace_", "worktree_"))}
    assert workspace == reasons
    assert {r.value for r in tm.RefuseReason if r.value.startswith(("workspace_", "worktree_"))} == reasons
    from looper.agents.v1_domains import manager_metrics
    work_mode = manager_metrics.SUMMARY["properties"]["work_mode"]["properties"]["task_manager_launch_refusals_total"]
    published = set(work_mode["items"]["properties"]["reason"]["enum"])
    assert reasons <= published


def test_a_caller_cancelled_while_the_lock_is_taken_leaves_no_lock(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import threading

    repo = _repo(tmp_path / "repo")
    gate, entered = threading.Event(), threading.Event()
    real = DirectoryLock.acquire.__func__

    def slow(cls, *args, **kwargs):
        entered.set()
        gate.wait(10)
        return real(cls, *args, **kwargs)

    monkeypatch.setattr(DirectoryLock, "acquire", classmethod(slow))
    identity = task_manager_dirs._directory_identity(task_manager_dirs._realpath(str(repo)))

    async def main() -> None:
        task = asyncio.ensure_future(task_manager_dirs.admit_off_loop(
            _IN_PLACE, str(repo), "t1", environment={"HOME": str(tmp_path / "home")},
            runtime_root=tmp_path / "runtime", live=(), expected=identity))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        await asyncio.sleep(0.5)
        real(DirectoryLock, tmp_path / "runtime", str(repo), "probe").release()  # LockHeld if the lock leaked

    asyncio.run(main())
