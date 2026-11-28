"""``looper.agents.git_jail``: every ``git_safe`` command on macOS runs under the Seatbelt profile.

Each channel is armed in a temporary repository. Plain git reads a synthetic denied file, or writes a file outside
the repository (the positive control); through ``git_safe`` the same command is killed, nothing of the file is carried
and the outside file is unchanged. All data is synthetic, under a synthetic HOME.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from looper.agents import git_jail, git_safe, task_manager_sandbox

darwin = pytest.mark.skipif(sys.platform != "darwin", reason="the jail is macOS Seatbelt (sandbox-exec)")

MARKER = "FAKEMARKER"
_IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


@pytest.fixture(autouse=True)
def world(tmp_path, monkeypatch):
    """A synthetic HOME and runtime root; no configuration of the machine's operator."""
    base = Path(os.path.realpath(tmp_path))
    home, runtime = base / "home", base / "runtime"
    (home / ".ssh").mkdir(parents=True)
    runtime.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME", "TRAE_HOME", "COPILOT_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(git_jail, "_runtime_root", runtime)
    monkeypatch.setattr(git_jail, "_config_file", None)
    monkeypatch.setattr(git_jail, "_projects_root", None)
    monkeypatch.setattr(git_jail, "_workspace_roots", set())
    monkeypatch.setattr(git_jail, "_session_denies", set())
    git_safe._memo.clear()
    yield base
    git_safe._memo.clear()


def plain(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ["HOME"], "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", **_IDENTITY}
    return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, check=check, timeout=30)


def safe(directory: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return git_safe.run(directory, *args, extra_env=_IDENTITY, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL, check=check, timeout=30)


def make_repo(directory: Path) -> Path:
    directory.mkdir(parents=True)
    plain(directory, "init", "-q", "-b", "main")
    (directory / "a.txt").write_text("one\n")
    plain(directory, "add", "-A")
    plain(directory, "commit", "-q", "-m", "first")
    return directory


@pytest.fixture
def repo(world) -> Path:
    return make_repo(world / "project")


def secret(world: Path, text: str) -> Path:
    path = world / "home" / ".ssh" / "id_fake"
    path.write_text(text)
    return path


def link(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.symlink_to(target)


# --- Reads of a denied file ---


def _packed_refs(world, repo):
    link(repo / ".git" / "packed-refs", secret(world, f"{MARKER}\n"))
    return ("for-each-ref",), MARKER


def _ref(world, repo):
    oid = "1234567890" * 4
    link(repo / ".git" / "refs" / "heads" / "leak", secret(world, oid + "\n"))
    return ("rev-parse", "refs/heads/leak"), oid


def _shallow(world, repo):
    link(repo / ".git" / "shallow", secret(world, f"{MARKER}\n"))
    return ("log", "--oneline"), MARKER


def _index(world, repo):
    link(repo / ".git" / "index", secret(world, f"{MARKER}-{'x' * 80}\n"))
    return ("status", "--porcelain"), "bad signature"


def _include(world, repo):
    target = secret(world, f"[leaked]\n\tvalue = {MARKER}\n")
    plain(repo, "config", "include.path", str(target))
    return ("config", "--get", "leaked.value"), MARKER


def _rebase_onto(world, repo):
    state = repo / ".git" / "rebase-merge"
    head = plain(repo, "rev-parse", "HEAD").stdout
    link(state / "onto", secret(world, f"{MARKER}\n"))
    for name, text in (("interactive", ""), ("head-name", "refs/heads/main\n"), ("orig-head", head),
                       ("git-rebase-todo", ""), ("done", "")):
        (state / name).write_text(text)
    return ("status",), MARKER


READS = {"packed_refs": _packed_refs, "ref_link": _ref, "shallow": _shallow, "index": _index,
         "config_include": _include, "rebase_onto": _rebase_onto}


@darwin
@pytest.mark.parametrize("channel", READS)
def test_a_denied_file_is_not_read_through_the_repository(channel, world, repo):
    args, shows = READS[channel](world, repo)
    open_run = plain(repo, *args, check=False)
    assert shows in open_run.stdout + open_run.stderr, "positive control: plain git shows the file's content"
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(repo, *args)
    assert refused.value.reason == "sandbox_denied"
    carried = f"{refused.value} {vars(refused.value)} {refused.value.__cause__} {refused.value.__context__}"
    assert shows not in carried and MARKER not in carried


@darwin
def test_spawn_refuses_a_denied_read_too(world, repo):
    args, _ = _packed_refs(world, repo)

    async def go() -> None:
        process = await git_safe.spawn(repo, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()
        await git_safe.recheck_async(process)

    with pytest.raises(git_safe.RepositoryRefused) as refused:
        asyncio.run(go())
    assert refused.value.reason == "sandbox_denied"


@darwin
@pytest.mark.parametrize("name", ["logs/HEAD", "COMMIT_EDITMSG"])
def test_nothing_is_written_outside_the_repository(name, world, repo):
    victim = world / "victim"
    commit = ("commit", "-q", "--allow-empty", "-m", "second")
    victim.write_text("IMPORTANT\n")
    link(repo / ".git" / name, victim)
    plain(repo, *commit, check=False)
    assert victim.read_text() != "IMPORTANT\n", "positive control: plain git writes through the link"
    victim.write_text("IMPORTANT\n")
    link(repo / ".git" / name, victim)
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(repo, *commit)
    assert refused.value.reason == "sandbox_denied"
    assert victim.read_text() == "IMPORTANT\n"


@darwin
@pytest.mark.parametrize("part", ["hooks/x", "info/x", "objects/info/x"])
def test_the_protected_parts_of_the_repository_are_not_written(part, repo):
    (repo / ".git" / part).parent.mkdir(parents=True, exist_ok=True)
    link(repo / ".git" / "COMMIT_EDITMSG", repo / ".git" / part)
    with pytest.raises(git_safe.RepositoryRefused):
        safe(repo, "commit", "-q", "--allow-empty", "-m", "second")
    assert not (repo / ".git" / part).exists()


@darwin
def test_a_denied_path_inside_a_repository_stays_denied(world):
    """A repository that contains a denied path (a home directory kept in git) does not open it."""
    home = world / "home"
    for args in (("init", "-q", "-b", "main"), ("commit", "-q", "--allow-empty", "-m", "first")):
        plain(home, *args)
    link(home / ".git" / "packed-refs", secret(world, f"{MARKER}\n"))
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(home, "for-each-ref")
    assert refused.value.reason == "sandbox_denied"


@darwin
def test_a_repository_under_a_denied_path_is_refused_outside_loopers_places(world):
    hidden = make_repo(world / "home" / ".ssh" / "repo")
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(hidden, "status")
    assert refused.value.reason == "sandbox_denied"


@darwin
def test_a_submodule_link_into_an_unrelated_repository_is_refused(world, repo):
    other = make_repo(world / "other")
    modules = other / ".git" / "modules" / "x"
    plain(world, "init", "-q", "--bare", str(modules))
    plain(world, "--git-dir", str(modules), "config", "core.bare", "false")
    fake = world / "task"
    fake.mkdir()
    (fake / ".git").write_text(f"gitdir: {modules}\n")
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(fake, "status")
    assert refused.value.reason == "git_dir_mismatch"


# --- What Looper really runs passes ---


@darwin
def test_the_commands_looper_runs_pass(world, repo):
    runtime = world / "runtime"
    git_jail.add_workspace_root(runtime)
    for args in (("rev-parse", "HEAD"), ("rev-parse", "--is-inside-work-tree"), ("status", "--porcelain"),
                 ("branch", "--show-current"), ("symbolic-ref", "-q", "HEAD"), ("ls-files", "--stage", "-z"),
                 ("for-each-ref", "refs/heads"), ("log", "--oneline"), ("cat-file", "-p", "HEAD"),
                 ("diff", "--no-ext-diff", "--no-textconv", "HEAD"), ("worktree", "list", "--porcelain"),
                 ("rev-list", "--count", "HEAD"), ("check-ref-format", "--branch", "x")):
        safe(repo, *args)
    step = runtime / "worktrees" / "task" / "step"
    step.parent.mkdir(parents=True)
    safe(repo, "worktree", "add", "-q", "-b", "step", str(step), "HEAD")
    (step / "b.txt").write_text("two\n")
    for args in (("status", "--porcelain"), ("add", "-A"), ("commit", "-q", "-m", "step"), ("log", "--oneline")):
        safe(step, *args)
    safe(repo, "merge", "-q", "--no-ff", "-m", "merge", "step")
    safe(repo, "worktree", "remove", "--force", str(step))
    safe(repo, "worktree", "prune")
    safe(repo, "branch", "-D", "step")
    assert not step.exists() and "step" not in plain(repo, "branch").stdout
    assert not (repo / ".git" / "config.lock").exists()


@darwin
def test_a_private_index_snapshot_and_a_verification_clone_pass(world, repo):
    from looper.agents import explorer_checkout
    from looper.agents.tiers import worker

    runtime = world / "runtime"
    snapshot = explorer_checkout.prepare(repo, runtime / ".local" / "explorers" / "one")
    assert (snapshot.root / "a.txt").read_text() == "one\n"
    clone = Path(worker._verification_checkout(str(repo), runtime / ".local" / "verification" / "key"))
    assert (clone / "a.txt").read_text() == "one\n"
    assert "origin" not in safe(clone, "config", "--list").stdout


@darwin
def test_a_global_filter_that_writes_inside_the_repository_converts(world, repo, monkeypatch):
    upper = world / "upper.sh"
    upper.write_text('#!/bin/sh\nmkdir -p .git/lfs && echo ran >> .git/lfs/log\ntr a-z A-Z\n')
    upper.chmod(0o755)
    config = world / "gitconfig"
    config.write_text(f'[filter "lfsish"]\n\tclean = {upper}\n\trequired = true\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    (repo / ".gitattributes").write_text("*.txt filter=lfsish\n")
    (repo / "c.txt").write_text("hello\n")
    safe(repo, "add", "-A")
    assert safe(repo, "cat-file", "-p", ":c.txt").stdout == "HELLO\n"
    # One that writes outside is killed, and a required filter then fails the command.
    upper.write_text(f'#!/bin/sh\necho ran >> "{world}/outside"\ntr a-z A-Z\n')
    (repo / "d.txt").write_text("hello\n")
    with pytest.raises((subprocess.CalledProcessError, git_safe.RepositoryRefused)):
        safe(repo, "add", "-A")
    assert not (world / "outside").exists()


# --- Write targets are typed and validated ---


def test_a_write_target_that_is_not_typed_is_refused(world):
    with pytest.raises(TypeError):
        git_jail.profile(targets=[str(world / "runtime" / "worktrees" / "x")])


@pytest.mark.parametrize("kind", [git_jail.WorktreeTarget, git_jail.CheckoutPrefix, git_jail.CloneDestination,
                                  git_jail.OwnClone])
def test_a_target_outside_loopers_places_is_refused(kind, world):
    area = world / "runtime" / "worktrees"
    area.mkdir()
    (area / "escape").symlink_to(world / "home")
    for path in (str(world / "elsewhere"), "relative", str(area), str(area / "escape" / "x"),
                 str(world / "home" / ".ssh")):
        with pytest.raises(git_jail.JailRefused):
            git_jail.profile(targets=[kind(path)])
    assert str(area / "task") in git_jail.profile(targets=[kind(str(area / "task"))])


def test_a_worktree_outside_loopers_places_is_removed_only_when_the_repository_registered_it(world, repo):
    linked, other = world / "linked", world / "other"
    plain(repo, "worktree", "add", "-q", "-b", "linked", str(linked), "HEAD")
    other.mkdir()
    (other / ".git").write_text((linked / ".git").read_text())  # names the entry, which names another checkout
    common = str(repo / ".git")
    assert str(linked) in git_jail.profile(common=common, targets=[git_jail.RemoveTarget(str(linked))])
    for path in (other, world / "home", repo):
        with pytest.raises(git_jail.JailRefused):
            git_jail.profile(common=common, targets=[git_jail.RemoveTarget(str(path))])


def test_the_other_targets_are_validated(world, repo):
    for target in (git_jail.PrivateIndex(str(repo / "index")), git_jail.RepairTarget(str(world / "home")),
                   git_jail.BranchSections(str(world / "other"))):
        with pytest.raises(git_jail.JailRefused):
            git_jail.profile(common=str(repo / ".git"), targets=[target])


@darwin
def test_a_caller_cannot_add_a_write_place(world, repo):
    with pytest.raises(TypeError):
        git_safe.run(repo, "status", targets=[str(world)])
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(repo, "worktree", "add", "-q", "--detach", str(world / "elsewhere"), "HEAD")
    assert refused.value.reason == "sandbox_denied" and not (world / "elsewhere").exists()


# --- Typed results ---


@darwin
def test_loopers_own_kill_is_not_the_jails(repo):
    async def go() -> int:
        process = await git_safe.spawn(repo, "cat-file", "--batch", stdin=asyncio.subprocess.PIPE,
                                       stdout=asyncio.subprocess.PIPE)
        process.kill()
        await process.wait()
        await git_safe.recheck_async(process)  # does not raise
        return process.returncode

    assert asyncio.run(go()) == -9


@darwin
@pytest.mark.parametrize("broken", ["profile", "program", "binary"])
def test_a_jail_that_cannot_be_applied_refuses(broken, repo, monkeypatch):
    if broken == "binary":
        monkeypatch.setattr(git_jail, "SANDBOX_EXEC", "/nonexistent/sandbox-exec")
    elif broken == "profile":
        monkeypatch.setattr(git_jail, "profile", lambda *args, **kwargs: "(version 1")
    else:
        monkeypatch.setattr(git_jail, "_program", lambda *args: "/nonexistent/git")
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(repo, "status")
    assert refused.value.reason == "sandbox_unavailable"


# --- The self-check direct mode starts behind ---


@darwin
def test_the_self_check_passes_here():
    assert git_jail.self_check() is None
    assert task_manager_sandbox.platform_problem() is None


@darwin
@pytest.mark.parametrize("name, value", [("SANDBOX_EXEC", "/nonexistent/sandbox-exec"), ("_BASE", "(version 1"),
                                         ("_KILL", "")], ids=["missing", "profile_error", "deny_does_not_kill"])
def test_a_failed_self_check_refuses_direct_mode(name, value, monkeypatch):
    git_jail._compile.cache_clear()
    monkeypatch.setattr(git_jail, name, value)
    try:
        problem = task_manager_sandbox.platform_problem()
    finally:
        git_jail._compile.cache_clear()
    assert problem is not None and "self-check" in problem


def test_without_a_jail_the_command_is_unchanged(repo, monkeypatch):
    monkeypatch.setattr(git_jail, "ENABLED", False)
    command = git_safe._confined(repo, ("status",), None, None)
    assert command == git_safe.argv("status", directory=repo)


# --- Parity with the session ---


def test_the_jail_denies_everything_the_session_is_denied(world, monkeypatch):
    ops = world / "elsewhere" / "ops.toml"
    git_jail.configure(world / "runtime", ops)
    custom = world / "profile-claude"  # named only by the launched session's environment
    session_env = {**os.environ, "CLAUDE_CONFIG_DIR": str(custom), "XDG_CONFIG_HOME": str(world / "xdg")}
    read_deny = task_manager_sandbox.read_deny_paths(session_env, world / "runtime", ops)
    git_jail.add_session_denies(read_deny)
    session = task_manager_sandbox.settings_content(world, read_deny=read_deny)["sandbox"]["filesystem"]["denyRead"]
    assert str(custom) in session
    assert set(session) <= set(git_jail.read_deny())
    text = git_jail.profile()
    assert [path for path in session if f'(deny file-read* (subpath "{path}")' not in text] == []


def test_the_servers_own_deny_list_needs_no_session(world):
    ops = world / "elsewhere" / "ops.toml"
    git_jail.configure(world / "runtime", ops)
    expected = task_manager_sandbox.read_deny_paths(os.environ, world / "runtime", ops)
    assert set(git_jail.read_deny()) == set(expected)
