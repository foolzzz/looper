"""``looper.agents.git_safe``: git run in a directory an agent writes does not run the repository's own code.

Each mechanism is armed in a temporary repository with a probe that appends to a marker file. The marker fires with
plain git (the positive control: the mechanism is real here) and stays empty through the helper.
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from looper.agents import git_jail, git_safe

REPO_ROOT = Path(__file__).resolve().parent.parent

_PLAIN_ENV = {"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": os.devnull,
              "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
              "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
_SAFE_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
_IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def plain(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Ordinary git: the control."""
    return subprocess.run(["git", "-C", str(repo), *args], env={**_PLAIN_ENV, **_PLAIN_ENV_PATCH}, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, check=check, timeout=30)


def safe(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return git_safe.run(repo, *args, extra_env=_IDENTITY, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                        check=check, timeout=30)


@pytest.fixture
def no_jail(monkeypatch):
    """For a test whose probe records that it ran by writing outside the repository, which the macOS jail kills:
    what is tested here is the overrides and the environment on their own (``tests/test_git_jail.py`` has the jail)."""
    monkeypatch.setattr(git_jail, "ENABLED", False)


@pytest.fixture(autouse=True)
def _fresh_memory():
    git_safe._memo.clear()
    yield
    git_safe._memo.clear()


@pytest.fixture(autouse=True)
def _operator_config(monkeypatch):
    for name, value in _SAFE_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    """An executable that records that it ran, then behaves like ``cat`` (a filter or textconv) or fails (gpg)."""
    script = tmp_path / "probe.sh"
    marker = tmp_path / "marker"
    script.write_text(f'#!/bin/sh\necho "$0 $*" >> "{marker}"\ncase "$1" in -*) exit 1;; esac\n'
                      'if [ -n "$1" ] && [ -f "$1" ]; then cat "$1"; else cat; fi\n')
    script.chmod(0o755)
    return script


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "marker"


def fired(marker: Path) -> list[str]:
    return marker.read_text().splitlines() if marker.exists() else []


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    plain(directory, "init", "-q", "-b", "main")
    (directory / "a.txt").write_text("one\n")
    (directory / "b.dat").write_text("data\n")
    plain(directory, "add", "-A")
    plain(directory, "commit", "-q", "-m", "first")
    return directory


def _stage_change(repo: Path) -> None:
    (repo / "a.txt").write_text("two\n")
    plain(repo, "add", "a.txt")


def _arm_hook(repo, probe):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f'#!/bin/sh\nexec "{probe}" hook\n')
    hook.chmod(0o755)
    _stage_change(repo)


def _arm_hooks_path(repo, probe):
    directory = repo.parent / "elsewhere"
    directory.mkdir()
    hook = directory / "pre-commit"
    hook.write_text(f'#!/bin/sh\nexec "{probe}" hookspath\n')
    hook.chmod(0o755)
    plain(repo, "config", "core.hooksPath", str(directory))
    _stage_change(repo)


def _arm_fsmonitor(repo, probe):
    plain(repo, "config", "core.fsmonitor", str(probe))
    (repo / "a.txt").write_text("changed\n")


def _arm_clean(repo, probe):
    (repo / ".gitattributes").write_text("*.txt filter=evil\n")
    plain(repo, "config", "filter.evil.clean", str(probe))
    (repo / "a.txt").write_text("changed\n")


def _arm_smudge(repo, probe):
    (repo / ".gitattributes").write_text("*.txt filter=evil\n")
    plain(repo, "add", ".gitattributes")
    plain(repo, "commit", "-q", "-m", "attributes")
    plain(repo, "config", "filter.evil.smudge", str(probe))


def _arm_info_attributes(repo, probe):
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.txt filter=evil\n")
    plain(repo, "config", "filter.evil.clean", str(probe))
    (repo / "a.txt").write_text("changed\n")


def _arm_gpg(repo, probe):
    plain(repo, "config", "commit.gpgsign", "true")
    plain(repo, "config", "gpg.program", str(probe))
    _stage_change(repo)


def _arm_textconv(repo, probe):
    (repo / ".gitattributes").write_text("*.dat diff=evil\n")
    plain(repo, "add", ".gitattributes")
    plain(repo, "commit", "-q", "-m", "attributes")
    plain(repo, "config", "diff.evil.textconv", str(probe))
    (repo / "b.dat").write_text("changed\n")


def _arm_diff_external(repo, probe):
    plain(repo, "config", "diff.external", str(probe))
    (repo / "a.txt").write_text("changed\n")


def _arm_ssh(repo, probe):
    plain(repo, "config", "core.sshCommand", str(probe))


def _arm_merge_driver(repo, probe):
    (repo / ".gitattributes").write_text("*.txt merge=evil\n")
    plain(repo, "add", ".gitattributes")
    plain(repo, "commit", "-q", "-m", "attributes")
    plain(repo, "config", "merge.evil.driver", f"{probe} %O %A %B")
    plain(repo, "checkout", "-q", "-b", "side")
    (repo / "a.txt").write_text("side\n")
    plain(repo, "commit", "-q", "-am", "side")
    plain(repo, "checkout", "-q", "main")
    (repo / "a.txt").write_text("main\n")
    plain(repo, "commit", "-q", "-am", "main")


# name -> (arm, git arguments that trigger it)
MECHANISMS: dict[str, tuple[Callable[[Path, Path], None], tuple[str, ...]]] = {
    "hook": (_arm_hook, ("commit", "-q", "-m", "x")),
    "hooks_path": (_arm_hooks_path, ("commit", "-q", "-m", "x")),
    "fsmonitor": (_arm_fsmonitor, ("status", "--porcelain")),
    "filter_clean": (_arm_clean, ("add", "-A")),
    "filter_smudge": (_arm_smudge, ("worktree", "add", "-q", "--detach", "../wt", "HEAD")),
    "gpg_program": (_arm_gpg, ("commit", "-q", "-m", "x")),
    "textconv": (_arm_textconv, ("diff",)),
    "diff_external": (_arm_diff_external, ("diff",)),
    "ssh_command": (_arm_ssh, ("ls-remote", "ssh://127.0.0.1:9/none")),
    "merge_driver": (_arm_merge_driver, ("merge-tree", "--write-tree", "main", "side")),
}


@pytest.mark.parametrize("name", MECHANISMS)
def test_plain_git_runs_the_mechanism(name, repo, probe, marker):
    """Positive control: the mechanism fires here with ordinary git, so the negative below proves something."""
    arm, trigger = MECHANISMS[name]
    arm(repo, probe)
    plain(repo, *trigger, check=False)
    assert fired(marker), f"{name} did not fire with plain git"


@pytest.mark.usefixtures("no_jail")
@pytest.mark.parametrize("name", MECHANISMS)
def test_the_helper_does_not_run_the_mechanism(name, repo, probe, marker):
    arm, trigger = MECHANISMS[name]
    arm(repo, probe)
    done = safe(repo, *trigger)
    assert fired(marker) == [], f"{name} fired through the helper"
    if name in ("hook", "hooks_path", "gpg_program"):
        assert done.returncode == 0, done.stderr  # the commit itself went through, unsigned and hook-free


def test_info_attributes_is_refused_by_the_preflight(repo, probe, marker):
    _arm_info_attributes(repo, probe)
    plain(repo, "add", "-A")
    assert fired(marker), "info/attributes filter did not fire with plain git"
    marker.unlink()
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(repo, "add", "-A")
    assert refused.value.reason == "attributes_driver"
    assert fired(marker) == []


def test_info_attributes_without_drivers_is_allowed(repo):
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("# a comment: filter=x\n*.txt text -diff\n")
    assert safe(repo, "status", "--porcelain").returncode == 0


def test_the_worktrees_shared_info_attributes_is_refused(repo, probe):
    plain(repo, "worktree", "add", "-q", "--detach", str(repo.parent / "wt"), "HEAD")
    _arm_info_attributes(repo, probe)
    with pytest.raises(git_safe.RepositoryRefused):
        safe(repo.parent / "wt", "status")


@pytest.mark.usefixtures("no_jail")
def test_the_environment_does_not_carry_the_servers_secrets(repo, tmp_path, monkeypatch):
    """A hook that does run (here the override is undone on purpose, in the test only) sees no credentials."""
    secrets = {"LOOPER_DATABASE_URL": "postgresql://u:pw@db/x", "PGPASSWORD": "pw", "ANTHROPIC_API_KEY": "key",
               "LOOPER_AGENT_TOKEN": "tok", "GIT_SSH_COMMAND": "ssh -v"}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    dump = tmp_path / "env.txt"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f'#!/bin/sh\nenv > "{dump}"\n')
    hook.chmod(0o755)
    _stage_change(repo)
    # Plain git with the server's environment: the hook sees every secret (positive control).
    subprocess.run(["git", "-C", str(repo), "-c", f"core.hooksPath={hooks}", "commit", "-q", "-m", "x"],
                   env={**os.environ, **_IDENTITY}, capture_output=True, check=True, stdin=subprocess.DEVNULL)
    assert "LOOPER_DATABASE_URL" in dump.read_text()
    dump.unlink()
    # The helper, with a later -c that re-enables the hook: it runs, with the allowlisted environment only.
    _stage_change_again(repo)
    done = safe(repo, "-c", f"core.hooksPath={hooks}", "commit", "-q", "-m", "y")
    assert done.returncode == 0, done.stderr
    seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert not set(secrets) & set(seen)
    assert seen["PATH"] and seen["GIT_TERMINAL_PROMPT"] == "0"


def _stage_change_again(repo: Path) -> None:
    (repo / "a.txt").write_text("three\n")
    plain(repo, "add", "a.txt")


SERVER_SECRETS = {"LOOPER_DATABASE_URL": "postgresql://svc:pw-fake@db.invalid/looper", "PGPASSWORD": "pw-fake",
                  "PGUSER": "svc", "LOOPER_AGENT_TOKEN": "tok-fake", "LOOPER_OPERATOR_TOKEN": "op-fake",
                  "ANTHROPIC_API_KEY": "sk-fake", "OPENAI_API_KEY": "sk-fake2", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-fake",
                  "XAI_API_KEY": "xai-fake", "HTTPS_PROXY": "http://proxy.invalid:1", "SSH_AUTH_SOCK": "/tmp/agent.sock"}


def _plant_after_check(monkeypatch, repo: Path, probe: Path, *, asynchronous: bool = False) -> None:
    """Simulate the check-then-run race deterministically: the repository gains a filter that runs, after the
    check found none and before the command starts."""
    def plant():
        plain(repo, "config", "filter.race.clean", str(probe))

    if asynchronous:
        real_async = git_safe.check_repository_async

        async def late(directory, **kwargs):
            found = await real_async(directory, **kwargs)
            plant()
            return found

        monkeypatch.setattr(git_safe, "check_repository_async", late)
    else:
        real = git_safe.check_repository

        def late_sync(directory, **kwargs):
            found = real(directory, **kwargs)
            plant()
            return found

        monkeypatch.setattr(git_safe, "check_repository", late_sync)


# What git itself adds to the environment of a filter it starts (measured with a filter that is ``env``).
GIT_ADDS = {"GIT_EXEC_PATH", "GIT_PREFIX", "GIT_CONFIG_PARAMETERS"}
# macOS adds this to every process it starts, whatever the parent passes.
PLATFORM_ADDS = {"__CF_USER_TEXT_ENCODING"}


def _prepare_race(repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.txt filter=race\n")
    (repo / "a.txt").write_text("changed\n")


def _env_seen_by_the_filter(repo: Path) -> dict[str, str]:
    """The filter is ``env``: its output, which git stores as the file's content, is the environment it started with."""
    text = plain(repo, "cat-file", "-p", ":a.txt").stdout
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def test_a_driver_that_runs_anyway_sees_exactly_the_allowlisted_environment(repo, monkeypatch):
    """The containment: if a definition slips in after the check, what git starts has only ``environment()``."""
    for name, value in SERVER_SECRETS.items():
        monkeypatch.setenv(name, value)
    _prepare_race(repo)
    plain(repo, "config", "filter.race.clean", "/usr/bin/env")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], env={**os.environ, **_PLAIN_ENV, **SERVER_SECRETS},
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    assert set(SERVER_SECRETS) <= set(_env_seen_by_the_filter(repo)), "positive control: plain git passes them on"
    plain(repo, "config", "--unset", "filter.race.clean")
    plain(repo, "reset", "-q")
    _plant_after_check(monkeypatch, repo, Path("/usr/bin/env"))
    assert safe(repo, "add", "-A").returncode == 0
    seen = _env_seen_by_the_filter(repo)
    assert seen, "the planted filter ran: the race is real and the environment is what contains it"
    assert set(seen) - PLATFORM_ADDS == set(git_safe.environment(_IDENTITY)) | GIT_ADDS
    assert seen["GIT_TERMINAL_PROMPT"] == "0" and seen["GIT_ASKPASS"] == "true"


def test_a_driver_that_runs_anyway_through_spawn_sees_exactly_the_allowlisted_environment(repo, monkeypatch):
    for name, value in SERVER_SECRETS.items():
        monkeypatch.setenv(name, value)
    _prepare_race(repo)
    _plant_after_check(monkeypatch, repo, Path("/usr/bin/env"), asynchronous=True)

    async def go():
        process = await git_safe.spawn(repo, "add", "-A", extra_env=_IDENTITY, stdout=asyncio.subprocess.PIPE,
                                       stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
        await process.communicate()
        assert process.returncode == 0

    asyncio.run(go())
    seen = _env_seen_by_the_filter(repo)
    assert set(seen) - PLATFORM_ADDS == set(git_safe.environment(_IDENTITY)) | GIT_ADDS


def test_a_driver_that_appears_while_git_runs_is_logged_with_the_task_and_step(repo, probe, monkeypatch, caplog):
    _plant_after_check(monkeypatch, repo, probe)
    with caplog.at_level("WARNING", logger="looper.agents.git_safe"), git_safe.running_for("task-7", "step-3"):
        safe(repo, "status", "--porcelain")
    lines = [r.getMessage() for r in caplog.records if r.name == "looper.agents.git_safe"]
    assert len(lines) == 1 and "task task-7" in lines[0] and "step step-3" in lines[0]
    assert "filter.race.clean" in lines[0] and str(probe) not in lines[0]  # names, never the commands


def test_the_warning_says_unknown_without_ids_and_is_silent_without_a_new_driver(repo, probe, monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="looper.agents.git_safe"):
        safe(repo, "status", "--porcelain")
    assert caplog.records == []
    _plant_after_check(monkeypatch, repo, probe)
    with caplog.at_level("WARNING", logger="looper.agents.git_safe"):
        safe(repo, "status", "--porcelain")
    assert "task unknown, step unknown" in caplog.records[-1].getMessage()


def test_the_warning_through_spawn_and_the_decorator_ids(repo, probe, monkeypatch, caplog):
    _plant_after_check(monkeypatch, repo, probe, asynchronous=True)

    @git_safe.attributed
    async def work(task_id, step_key):
        process = await git_safe.spawn(repo, "status", "--porcelain", stdout=asyncio.subprocess.PIPE,
                                       stdin=asyncio.subprocess.DEVNULL)
        await process.communicate()
        await git_safe.recheck_async(process)

    with caplog.at_level("WARNING", logger="looper.agents.git_safe"):
        asyncio.run(work("t-9", "s-1"))
    assert "task t-9, step s-1" in caplog.records[-1].getMessage()


def test_the_recheck_is_skipped_inside_a_cached_block(repo, probe, monkeypatch, caplog):
    _plant_after_check(monkeypatch, repo, probe)
    with caplog.at_level("WARNING", logger="looper.agents.git_safe"), git_safe.cached():
        safe(repo, "status", "--porcelain")
    assert caplog.records == []


def test_environment_allowlist():
    env = git_safe.environment({"GIT_INDEX_FILE": "/tmp/i"}, environ={
        "PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "LANG": "C", "PGPASSWORD": "x", "LOOPER_DATABASE_URL": "x",
        "GIT_DIR": "/elsewhere", "GIT_EXTERNAL_DIFF": "/bin/evil", "GIT_CONFIG_COUNT": "1", "HTTPS_PROXY": "p"})
    assert env["PATH"] == "/bin" and env["LC_ALL"] == "C" and env["GIT_INDEX_FILE"] == "/tmp/i"
    assert not {"PGPASSWORD", "LOOPER_DATABASE_URL", "GIT_DIR", "GIT_EXTERNAL_DIFF", "GIT_CONFIG_COUNT",
                "HTTPS_PROXY"} & set(env)
    with pytest.raises(ValueError):
        git_safe.environment({"GIT_DIR": "/elsewhere"})


def test_a_gitdir_link_to_another_repository_is_refused(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    plain(other, "init", "-q")
    task = tmp_path / "task"
    task.mkdir()
    (task / ".git").write_text(f"gitdir: {other / '.git'}\n")
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(task, "status")
    assert refused.value.reason == "git_dir_mismatch"


def test_a_gitdir_link_to_another_worktrees_entry_is_refused(repo, tmp_path):
    plain(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "wt"), "HEAD")
    forged = tmp_path / "forged"
    forged.mkdir()
    entry = next((repo / ".git" / "worktrees").iterdir())
    (forged / ".git").write_text(f"gitdir: {entry}\n")
    with pytest.raises(git_safe.RepositoryRefused):
        safe(forged, "status")


def test_a_core_worktree_elsewhere_is_refused(repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    plain(repo, "config", "core.worktree", str(elsewhere))
    (repo / "sub").mkdir()
    with pytest.raises(git_safe.RepositoryRefused):
        safe(repo / "sub", "status")


def test_ordinary_layouts_are_accepted(repo, tmp_path):
    (repo / "sub").mkdir()
    plain(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "wt"), "HEAD")
    assert git_safe.check_repository(repo) is not None
    assert git_safe.check_repository(repo / "sub") is not None
    assert git_safe.check_repository(tmp_path / "wt") is not None
    assert git_safe.check_repository(repo / ".git") is not None  # inside the git dir: no work tree, still checked
    assert safe(tmp_path / "wt", "status", "--porcelain").returncode == 0


def _submodule(tmp_path: Path, repo: Path) -> Path:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    plain(upstream, "init", "-q", "-b", "main")
    (upstream / "f").write_text("x\n")
    plain(upstream, "add", "-A")
    plain(upstream, "commit", "-q", "-m", "u")
    plain(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "module")
    return repo / "module"


def test_a_checked_out_submodule_is_accepted(repo, tmp_path):
    module = _submodule(tmp_path, repo)
    assert git_safe.check_repository(module) is not None
    assert safe(module, "status", "--porcelain").returncode == 0


def test_a_gitdir_link_to_a_submodule_of_another_checkout_is_refused(repo, tmp_path):
    module = _submodule(tmp_path, repo)
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / ".git").write_text(f"gitdir: {repo / '.git' / 'modules' / 'module'}\n")
    assert module.is_dir()
    with pytest.raises(git_safe.RepositoryRefused):
        safe(forged, "status")


def test_a_relocated_checkout_is_run_without_the_layout_check(repo, tmp_path):
    wt = tmp_path / "wt"
    plain(repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    moved = tmp_path / "moved"
    wt.rename(moved)  # the main repository still lists ``wt``: the links are stale
    with pytest.raises(git_safe.RepositoryRefused):
        git_safe.check_repository(moved)
    assert git_safe.check_repository(moved, layout=False) is not None
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.txt filter=evil\n")
    with pytest.raises(git_safe.RepositoryRefused) as refused:  # the attributes are checked either way
        git_safe.check_repository(moved, layout=False)
    assert refused.value.reason == "attributes_driver"


def test_a_submodule_info_attributes_is_refused_for_the_superproject(repo, tmp_path):
    module = _submodule(tmp_path, repo)
    git_dir = Path(plain(module, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip())
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "attributes").write_text("*.txt filter=evil\n")
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        safe(repo, "status")
    assert refused.value.reason == "attributes_driver"


def test_a_gitdir_link_elsewhere_runs_nothing_and_is_refused(repo, probe, marker, tmp_path):
    """A task directory whose ``.git`` file points at a repository armed with a probe."""
    other = tmp_path / "other"
    other.mkdir()
    plain(other, "init", "-q", "-b", "main")
    plain(other, "config", "core.fsmonitor", str(probe))
    task = tmp_path / "task"
    task.mkdir()
    (task / ".git").write_text(f"gitdir: {other / '.git'}\n")
    plain(task, "status", "--porcelain", check=False)
    assert fired(marker), "positive control: the linked repository's fsmonitor fires with plain git"
    marker.unlink()
    with pytest.raises(git_safe.RepositoryRefused):
        safe(task, "status", "--porcelain")
    assert fired(marker) == []


def test_core_worktree_in_the_config_runs_nothing_and_is_refused(repo, probe, marker, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    plain(repo, "config", "core.fsmonitor", str(probe))
    plain(repo, "config", "core.worktree", str(elsewhere))
    plain(repo, "status", "--porcelain", check=False)
    plain(repo, "--git-dir", str(repo / ".git"), "status", "--porcelain", check=False)
    assert fired(marker), "positive control: git runs the configured fsmonitor against the redirected work tree"
    marker.unlink()
    assert safe(elsewhere, "status", "--porcelain").returncode == 128  # no repository is found there
    with pytest.raises(git_safe.RepositoryRefused):
        safe(repo, "status", "--porcelain")
    assert fired(marker) == []


SUBMODULE_COMMANDS = {"status": ("status", "--porcelain"), "add": ("add", "-A"), "commit": ("commit", "-q", "-a", "-m", "x"),
                      "diff": ("diff",), "worktree_add": ("worktree", "add", "-q", "--detach", "../wt", "HEAD")}


def _arm_submodule(repo: Path, module: Path, probe: Path) -> None:
    """The submodule's own config, hook, attributes and a change inside it."""
    plain(module, "config", "core.fsmonitor", str(probe))
    plain(module, "config", "filter.evil.clean", str(probe))
    plain(module, "config", "diff.evil.textconv", str(probe))
    (module / ".gitattributes").write_text("*.txt filter=evil diff=evil\n")
    hook = Path(plain(module, "rev-parse", "--path-format=absolute", "--git-path", "hooks").stdout.strip()) / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f'#!/bin/sh\nexec "{probe}" submodule-hook\n')
    hook.chmod(0o755)
    (module / "f").write_text("changed in the submodule\n")
    (module / "g.txt").write_text("new\n")
    (repo / "a.txt").write_text("changed in the superproject\n")


@pytest.mark.usefixtures("no_jail")
@pytest.mark.parametrize("command", SUBMODULE_COMMANDS)
def test_a_submodule_runs_nothing_either(command, repo, probe, marker, tmp_path):
    module = _submodule(tmp_path, repo)
    plain(repo, "commit", "-q", "-m", "module")
    _arm_submodule(repo, module, probe)
    args = SUBMODULE_COMMANDS[command]
    plain(repo, *args, check=False)
    plain(module, "add", "-A", check=False)
    positive = fired(marker)
    assert positive, f"positive control: the submodule's own code fires with plain git (git {command})"
    marker.unlink()
    _arm_submodule(repo, module, probe)
    safe(repo, *args)
    safe(repo, "status", "--porcelain", "--ignore-submodules=none")
    assert fired(marker) == []


def test_a_submodule_info_attributes_is_refused_when_the_helper_runs_in_it(repo, probe, tmp_path):
    module = _submodule(tmp_path, repo)
    git_dir = Path(plain(module, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip())
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "attributes").write_text("*.txt filter=evil\n")
    with pytest.raises(git_safe.RepositoryRefused):
        safe(module, "status")


def test_outside_a_repository_the_command_runs_and_fails_as_before(tmp_path):
    plain_run = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"], env=_PLAIN_ENV,
                               capture_output=True, text=True)
    assert git_safe.check_repository(tmp_path) is None
    done = safe(tmp_path, "rev-parse", "--show-toplevel")
    assert (done.returncode, done.stdout) == (plain_run.returncode, plain_run.stdout)
    assert safe(tmp_path / "missing", "rev-parse", "HEAD").returncode == 128


def test_sha256_repositories_work(tmp_path):
    directory = tmp_path / "r256"
    directory.mkdir()
    if plain(directory, "init", "-q", "--object-format=sha256", check=False).returncode != 0:
        pytest.skip("this git cannot make a sha256 repository")
    (directory / "f").write_text("x\n")
    assert git_safe.check_repository(directory) is not None
    assert safe(directory, "status", "--porcelain").stdout.strip() == "?? f"


def _global_config(tmp_path: Path, monkeypatch, text: str) -> None:
    config = tmp_path / "user.gitconfig"
    config.write_text(text)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    _PLAIN_ENV_PATCH["GIT_CONFIG_GLOBAL"] = str(config)


_PLAIN_ENV_PATCH: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _reset_plain_env():
    yield
    _PLAIN_ENV_PATCH.clear()


def _blob(repo: Path, path: str) -> str:
    return plain(repo, "cat-file", "-p", f":{path}").stdout


def test_gitattributes_still_apply(repo):
    """eol and text attributes (what LFS and normalisation rely on) are not dropped."""
    (repo / ".gitattributes").write_text("*.txt text eol=crlf\n")
    (repo / "c.txt").write_bytes(b"one\r\ntwo\r\n")
    assert safe(repo, "add", "-A").returncode == 0
    assert _blob(repo, "c.txt") == "one\ntwo\n"  # normalised to LF in the object: the attribute applied


@pytest.mark.usefixtures("no_jail")
def test_a_user_level_filter_still_runs_and_converts(repo, probe, marker, tmp_path, monkeypatch):
    upper = tmp_path / "upper.sh"
    upper.write_text(f'#!/bin/sh\necho user-filter >> "{marker}"\ntr a-z A-Z\n')
    upper.chmod(0o755)
    _global_config(tmp_path, monkeypatch, f'[filter "lfsish"]\n\tclean = {upper}\n')
    (repo / ".gitattributes").write_text("*.txt filter=lfsish\n")
    (repo / "c.txt").write_text("hello\n")
    assert safe(repo, "add", "-A").returncode == 0
    assert _blob(repo, "c.txt") == "HELLO\n"
    assert set(fired(marker)) == {"user-filter"}


def test_a_repository_definition_of_a_user_level_filter_gets_the_users_back(repo, probe, marker, tmp_path, monkeypatch):
    upper = tmp_path / "upper.sh"
    upper.write_text("#!/bin/sh\ntr a-z A-Z\n")
    upper.chmod(0o755)
    _global_config(tmp_path, monkeypatch, f'[filter "lfsish"]\n\tclean = {upper}\n')
    plain(repo, "config", "filter.lfsish.clean", str(probe))  # the repository redefines it
    plain(repo, "config", "filter.lfsish.smudge", str(probe))
    plain(repo, "config", "filter.lfsish.required", "true")
    (repo / ".gitattributes").write_text("*.txt filter=lfsish\n")
    (repo / "c.txt").write_text("hello\n")
    assert safe(repo, "add", "-A").returncode == 0
    assert _blob(repo, "c.txt") == "HELLO\n"
    assert fired(marker) == []


def test_a_repository_attributes_file_setting_is_replaced(repo, probe, marker, tmp_path):
    attributes = tmp_path / "attrs"
    attributes.write_text("*.txt filter=evil\n")
    plain(repo, "config", "core.attributesFile", str(attributes))
    plain(repo, "config", "filter.evil.clean", str(probe))
    (repo / "a.txt").write_text("changed\n")
    plain(repo, "add", "-A")
    assert fired(marker), "positive control"
    plain(repo, "reset", "-q")
    marker.unlink()
    assert safe(repo, "add", "-A").returncode == 0
    assert fired(marker) == []


def test_a_required_repository_filter_without_a_command_does_not_fail_the_command(repo, probe):
    (repo / ".gitattributes").write_text("*.txt filter=evil\n")
    plain(repo, "config", "filter.evil.clean", str(probe))
    plain(repo, "config", "filter.evil.required", "true")
    (repo / "a.txt").write_text("changed\n")
    assert safe(repo, "add", "-A").returncode == 0


def test_overrides_are_found_by_scope():
    out = "\0".join(["global", "file:/h/.gitconfig", "filter.lfs.clean\nlfs clean", "local", "file:.git/config",
                     "filter.lfs.clean\nevil", "local", "file:.git/config", "filter.x.smudge\nevil",
                     "local", "file:.git/config", "filter.x.required\ntrue", "worktree", "file:w", "diff.d.textconv\nevil",
                     "local", "file:.git/config", "merge.m.driver\nevil", "local", "file:c", "core.attributesfile\n/x",
                     "global", "file:/h/.gitconfig", "filter.only.process\nok", ""])
    assert git_safe._overrides(out) == ("core.attributesfile=/dev/null", "diff.d.textconv=", "filter.lfs.clean=lfs clean",
                                        "filter.x.required=false", "filter.x.smudge=", "merge.m.driver=")


def _counting(monkeypatch):
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda command, *a, **k: calls.append(command) or real(command, *a, **k))
    return calls


def test_cached_checks_a_directory_once(repo, monkeypatch):
    calls = _counting(monkeypatch)
    with git_safe.cached():
        for _ in range(3):
            git_safe.check_repository(repo)
    assert len(calls) == 3  # layout, config and gitlink probes, once


def test_a_check_is_remembered_while_its_files_are_unchanged(repo, monkeypatch):
    calls = _counting(monkeypatch)
    first = git_safe.check_repository(repo)
    assert len(calls) == 3
    assert git_safe.check_repository(repo) == first and len(calls) == 3  # nothing started


def test_a_change_to_the_repository_gives_a_fresh_check(repo, probe, monkeypatch):
    assert git_safe.check_repository(repo).overrides == ()
    plain(repo, "config", "filter.new.clean", str(probe))
    assert git_safe.check_repository(repo).overrides == ("filter.new.clean=",)
    git_dir = repo / ".git"
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "attributes").write_text("*.txt filter=evil\n")
    with pytest.raises(git_safe.RepositoryRefused):
        git_safe.check_repository(repo)


def test_an_in_place_rewrite_with_the_old_mtime_is_seen(repo, probe):
    """An agent rewrites the config with the same length and sets the modification time back."""
    config = repo / ".git" / "config"
    plain(repo, "config", "filter.aaaa.clean", "/bin/cat")
    assert git_safe.check_repository(repo).overrides == ("filter.aaaa.clean=",)
    stamp = os.stat(config)
    text = config.read_text()
    same_length = text.replace('[filter "aaaa"]', '[filter "bbbb"]')
    assert len(same_length) == len(text) and same_length != text
    with open(config, "r+") as handle:  # in place: same inode and size
        handle.write(same_length)
    os.utime(config, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert os.stat(config).st_mtime_ns == stamp.st_mtime_ns and os.stat(config).st_size == stamp.st_size
    assert git_safe.check_repository(repo).overrides == ("filter.bbbb.clean=",)


def test_a_nested_repository_made_after_the_check_is_seen(repo, probe, tmp_path):
    (repo / "sub").mkdir()
    assert git_safe.check_repository(repo / "sub").overrides == ()
    plain(repo / "sub", "init", "-q")
    plain(repo / "sub", "config", "filter.inner.clean", str(probe))
    assert git_safe.check_repository(repo / "sub").overrides == ("filter.inner.clean=",)


def test_a_submodule_checked_out_after_the_check_is_seen(repo, probe, tmp_path):
    module = _submodule(tmp_path, repo)
    assert git_safe.check_repository(repo).overrides == ()
    plain(module, "config", "filter.sub.clean", str(probe))
    assert git_safe.check_repository(repo).overrides == ("filter.sub.clean=",)


def test_a_configuration_with_includes_is_never_remembered(repo, probe, monkeypatch, tmp_path):
    included = tmp_path / "included.cfg"
    included.write_text("")
    plain(repo, "config", "include.path", str(included))
    calls = _counting(monkeypatch)
    git_safe.check_repository(repo)
    first = len(calls)
    git_safe.check_repository(repo)
    assert len(calls) == 2 * first
    included.write_text(f"[filter \"inc\"]\n\tclean = {probe}\n")
    assert git_safe.check_repository(repo).overrides == ("filter.inc.clean=",)


# --- Object stores and replacement refs the repository supplies ---

def _foreign_store(tmp_path: Path) -> tuple[Path, str]:
    """Another repository under ``tmp_path`` and the id of a commit only it holds."""
    other = tmp_path / "foreign"
    other.mkdir()
    plain(other, "init", "-q", "-b", "main")
    (other / "foreign.txt").write_text("foreign\n")
    plain(other, "add", "-A")
    plain(other, "commit", "-q", "-m", "foreign commit")
    return other, plain(other, "rev-parse", "HEAD").stdout.strip()


def _borrow(common: Path, store: Path, name: str = "alternates") -> Path:
    path = common / "objects" / "info" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{store / '.git' / 'objects'}\n")
    return path


def _refused_for_alternates(directory: Path, **kwargs) -> None:
    with pytest.raises(git_safe.RepositoryRefused) as refused:
        git_safe.check_repository(directory, **kwargs)
    assert refused.value.reason == "object_alternates"


def test_an_alternate_object_store_is_refused(repo, tmp_path):
    other, commit = _foreign_store(tmp_path)
    assert plain(repo, "cat-file", "-e", commit, check=False).returncode != 0
    _borrow(repo / ".git", other)
    assert "foreign commit" in plain(repo, "cat-file", "-p", commit).stdout, "positive control: git reads the store"
    _refused_for_alternates(repo)
    _refused_for_alternates(repo, layout=False)
    with pytest.raises(git_safe.RepositoryRefused):
        safe(repo, "cat-file", "-p", commit)

    async def go():
        await git_safe.spawn(repo, "cat-file", "-p", commit, stdout=asyncio.subprocess.PIPE)

    with pytest.raises(git_safe.RepositoryRefused):
        asyncio.run(go())


def test_http_alternates_are_refused(repo, tmp_path):
    _borrow(repo / ".git", tmp_path, "http-alternates")
    _refused_for_alternates(repo)


@pytest.mark.parametrize("name", ["alternates", "http-alternates"])
def test_an_empty_alternates_file_is_allowed(repo, name):
    path = repo / ".git" / "objects" / "info" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    assert git_safe.check_repository(repo) is not None
    assert safe(repo, "status", "--porcelain").returncode == 0


@pytest.mark.parametrize("shape", ["link", "directory", "loop"])
def test_an_alternates_path_that_is_not_an_empty_file_is_refused(repo, tmp_path, shape):
    path = repo / ".git" / "objects" / "info" / "alternates"
    path.parent.mkdir(parents=True, exist_ok=True)
    if shape == "link":
        (tmp_path / "list").write_text("/elsewhere\n")
        path.symlink_to(tmp_path / "list")
    elif shape == "directory":
        path.mkdir()
    else:
        path.symlink_to(path)
    _refused_for_alternates(repo)


def test_the_worktrees_shared_alternates_are_refused(repo, tmp_path):
    wt = tmp_path / "wt"
    plain(repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    _borrow(repo / ".git", _foreign_store(tmp_path)[0])
    _refused_for_alternates(wt)


@pytest.mark.parametrize("name", ["alternates", "http-alternates"])
def test_alternates_written_after_a_check_are_seen(repo, tmp_path, name):
    assert git_safe.check_repository(repo) is not None
    path = repo / ".git" / "objects" / "info" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    assert git_safe.check_repository(repo) is not None
    stamp = os.stat(path)
    _borrow(repo / ".git", _foreign_store(tmp_path)[0], name)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    _refused_for_alternates(repo)


def test_a_submodules_alternates_are_refused(repo, tmp_path):
    module = _submodule(tmp_path, repo)
    git_dir = Path(plain(module, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip())
    assert git_safe.check_repository(repo) is not None  # remembered: the later file must still be seen
    _borrow(git_dir, _foreign_store(tmp_path)[0])
    _refused_for_alternates(repo)
    _refused_for_alternates(module)


def test_a_submodule_with_a_common_directory_is_checked_there(repo, tmp_path):
    """A submodule whose ``.git`` names a linked worktree's entry reads the objects of that entry's repository."""
    module = _submodule(tmp_path, repo)
    holder = tmp_path / "holder"
    holder.mkdir()
    plain(holder, "init", "-q", "-b", "main")
    plain(holder, "commit", "-q", "--allow-empty", "-m", "holder")
    plain(holder, "worktree", "add", "-q", "--detach", str(tmp_path / "holder-wt"), "HEAD")
    entry = next((holder / ".git" / "worktrees").iterdir())
    (module / ".git").write_text(f"gitdir: {entry}\n")
    assert git_safe.check_repository(repo) is not None
    _borrow(holder / ".git", _foreign_store(tmp_path)[0])
    _refused_for_alternates(repo)


def test_loopers_own_clones_and_worktrees_pass_the_check(repo, tmp_path):
    """The private verification clone (with a submodule), a step's worktree and a consult's checkout borrow no
    objects through alternates."""
    from looper.agents import consult, step_workspaces
    from looper.agents.tiers import worker

    _submodule(tmp_path, repo)
    plain(repo, "commit", "-q", "-m", "module")
    git_jail.add_workspace_root(tmp_path)  # as the server does: checkouts are made under its runtime root
    clone = Path(worker._verification_checkout(str(repo), tmp_path / ".local" / "verification" / "backup"))

    async def worktree() -> Path:
        found = await step_workspaces.inspect(str(repo))
        assert isinstance(found, step_workspaces.Repo), found
        workspace = step_workspaces.new_workspace(
            found, runtime_root=tmp_path / "runtime", workspace_id=1, task_id="task-1", step_id="step-1",
            step_key="s1", base=plain(repo, "rev-parse", "HEAD").stdout.strip(), checkpointed=False)
        await step_workspaces.ensure_worktree(workspace)
        return Path(workspace.path)

    step = asyncio.run(worktree())
    consulted = consult._snapshot(repo, tmp_path / ".local" / "consults" / "consult")
    assert consulted is not None
    for directory in (repo, repo / "module", clone, clone / "module", step, consulted[1]):
        git_safe._memo.clear()
        assert git_safe.check_repository(directory) is not None, directory
        assert safe(directory, "status", "--porcelain").returncode == 0, directory


def _replace_head(repo: Path) -> str:
    """Put a replacement ref on HEAD's commit; the original commit's id."""
    original = plain(repo, "rev-parse", "HEAD").stdout.strip()
    tree = plain(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    other = plain(repo, "commit-tree", "-m", "substituted", tree).stdout.strip()
    plain(repo, "replace", original, other)
    assert "substituted" in plain(repo, "cat-file", "-p", original).stdout, "positive control: git honours the ref"
    return original


def test_a_replace_ref_does_not_change_what_is_read(repo):
    original = _replace_head(repo)
    shown = safe(repo, "cat-file", "-p", original, check=True).stdout
    assert "first" in shown and "substituted" not in shown

    async def go() -> bytes:
        process = await git_safe.spawn(repo, "cat-file", "-p", original, stdout=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        return stdout

    assert b"substituted" not in asyncio.run(go())


@pytest.mark.parametrize("guard", ["argv", "environment"])
def test_each_replace_ref_guard_works_on_its_own(repo, guard):
    original = _replace_head(repo)
    command = ["git", "-C", str(repo), "cat-file", "-p", original]
    env = {**_PLAIN_ENV}
    if guard == "argv":
        command = git_safe.argv("cat-file", "-p", original, directory=repo)
    else:
        env = git_safe.environment()
    shown = subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=30,
                           stdin=subprocess.DEVNULL).stdout
    assert "first" in shown and "substituted" not in shown


# --- A missing object is not fetched from a promisor remote ---


def _promise_missing_blob(repo: Path, tmp_path: Path, marker: Path, config: str) -> str:
    """Name a promisor remote in the repository's ``config`` file, with a transport program that writes the marker,
    and delete the blob of ``a.txt``; its id."""
    transport = tmp_path / "transport.sh"
    transport.write_text(f'#!/bin/sh\necho "$0 $*" >> "{marker}"\nexit 1\n')
    transport.chmod(0o755)
    blob = plain(repo, "rev-parse", "HEAD:a.txt").stdout.strip()
    if config == "config.worktree":
        plain(repo, "config", "extensions.worktreeConfig", "true")
    for key, value in (("url", str(tmp_path / "elsewhere")), ("promisor", "true"), ("uploadpack", str(transport))):
        plain(repo, "config", "--file", str(repo / ".git" / config), f"remote.origin.{key}", value)
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    (repo / "a.txt").write_text("two\n")
    return blob


@pytest.mark.parametrize("config", ["config", "config.worktree"])
@pytest.mark.parametrize("command", [("cat-file", "-p", "BLOB"), ("show", "HEAD"), ("diff", "HEAD", "--", "a.txt"),
                                     ("log", "-p", "-1")], ids=lambda command: command[0])
def test_a_missing_object_is_not_fetched_from_a_promisor_remote(command, config, repo, marker, tmp_path):
    blob = _promise_missing_blob(repo, tmp_path, marker, config)
    command = tuple(blob if part == "BLOB" else part for part in command)
    plain(repo, *command, check=False)
    assert fired(marker), "positive control: ordinary git runs the transport to fetch the object"
    marker.unlink()
    done = safe(repo, *command)
    assert fired(marker) == []
    assert done.returncode != 0, done.stdout

    async def go() -> int:
        process = await git_safe.spawn(repo, *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()
        return process.returncode

    assert asyncio.run(go()) != 0
    assert fired(marker) == []


def test_every_git_call_refuses_lazy_fetches(repo, tmp_path, monkeypatch):
    _submodule(tmp_path, repo)
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")  # the server's own value does not reach git
    seen: list[tuple[list[str], dict[str, str] | None]] = []
    real_run, real_exec = subprocess.run, asyncio.create_subprocess_exec

    def run(command, *args, **kwargs):
        seen.append((list(command), kwargs.get("env")))
        return real_run(command, *args, **kwargs)

    async def execute(*command, **kwargs):
        seen.append((list(command), kwargs.get("env")))
        return await real_exec(*command, **kwargs)

    monkeypatch.setattr(git_safe.subprocess, "run", run)
    monkeypatch.setattr(git_safe.asyncio, "create_subprocess_exec", execute)

    async def go() -> None:
        process = await git_safe.spawn(repo, "rev-parse", "HEAD", stdout=asyncio.subprocess.PIPE)
        await process.communicate()
        await git_safe.recheck_async(process)

    safe(repo, "status", "--porcelain", check=True)
    git_safe._memo.clear()
    asyncio.run(go())
    git_safe.run(None, "--version", capture_output=True, check=True, timeout=30)
    subcommands = {word for command, _ in seen for word in command}
    assert {"rev-parse", "config", "ls-files", "status", "--version"} <= subcommands
    assert [command for command, env in seen if (env or {}).get("GIT_NO_LAZY_FETCH") != "1"] == []
    with pytest.raises(ValueError):
        git_safe.environment({"GIT_NO_LAZY_FETCH": "0"})


# --- No direct git call in looper/ outside the places listed here ---

# A file may run git directly only for a directory Looper owns the content of (its own checkout, a clone it just
# made from a remote, the operator's knowledge vault whose hooks are meant to run) or where there is no repository.
DIRECT_GIT_ALLOWED = {
    "looper/agents/git_safe.py": "the helper itself",
    "looper/agents/git_jail.py": "finds the git binary and runs `git --version` in the jail's self-check",
    "looper/components.py": "Looper's own checkout (component install and upgrade)",
    "looper/agents/service.py": "Looper's own checkout (self-upgrade)",
    "looper/agents/catalog_sources.py": "a shallow clone of a remote into a new temporary directory, already neutralised",
    "looper/agents/self_check.py": "`git --version`, no repository",
    "looper/agents/vault.py": "the operator's knowledge vault: its commit hooks run by design",
    "looper/agents/vault_commit.py": "the operator's knowledge vault: its commit hooks run by design",
    "looper/agents/memory_settings.py": "the operator's knowledge vault",
}
_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output", "create_subprocess_exec", "create_subprocess_shell"}


_GIT_PATH = re.compile(r"^(?:/[\w.+-]+)*/git$")
_GIT_SHELL = re.compile(r"^\s*(?:/\S*/)?git(?:\s|$)")
_GIT_NAMES = {"git", "git_bin", "git_path", "GIT", "GIT_BIN"}


def _is_git_word(node: ast.AST) -> bool:
    """"git", an absolute path to it, or a variable named like the binary."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "git" or bool(_GIT_PATH.match(node.value))
    return isinstance(node, ast.Name) and node.id in _GIT_NAMES


def _starts_with_git(node: ast.AST) -> bool:
    return isinstance(node, (ast.List, ast.Tuple)) and bool(node.elts) and _is_git_word(node.elts[0])


def _is_git_shell(node: ast.AST) -> bool:
    """A shell string that runs git (``"git status"``, ``f"git -C {d} status"``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(_GIT_SHELL.match(node.value))
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return isinstance(first, ast.Constant) and bool(_GIT_SHELL.match(str(first.value)))
    return False


def _direct_git_calls(source: str) -> list[int]:
    """Lines that start git directly: an argv (a list or tuple starting with git, its absolute path or a variable
    named like it) passed to a process starter, or one whose next item is an option or a ``*args``;
    ``create_subprocess_exec("git", ...)``; a shell string starting with git given to a starter or ``os.system``;
    ``shutil.which("git")``."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) > 1 and _is_git_word(node.elts[0]):
            second = node.elts[1]
            if isinstance(second, ast.Starred) or (isinstance(second, ast.Constant) and isinstance(second.value, str)
                                                    and second.value.startswith("-")):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            first = node.args[0] if node.args else None
            if name in _SPAWNERS | {"system", "popen"} and first is not None and (
                    _is_git_word(first) or _starts_with_git(first) or _is_git_shell(first)):
                lines.append(node.lineno)
            elif name == "which" and first is not None and _is_git_word(first):
                lines.append(node.lineno)
    return lines


def test_direct_git_calls_are_found_by_the_scan():
    assert _direct_git_calls('import subprocess\nsubprocess.run(["git", "status"])') == [2]
    assert not _direct_git_calls('x = (("git", "git"),)\ny = name not in ("git", "manager_read")')
    assert _direct_git_calls('async def f():\n    await asyncio.create_subprocess_exec("git", "-C", d)')
    assert _direct_git_calls('x = ("git", "-c", "a=b")')
    assert _direct_git_calls('git = shutil.which("git")')
    assert _direct_git_calls('subprocess.run(["/usr/bin/git", "status"])')
    assert _direct_git_calls('subprocess.run(f"git -C {d} status", shell=True)')
    assert _direct_git_calls('os.system("git status")')
    assert _direct_git_calls('subprocess.run([git, "status"])') and _direct_git_calls('x = [git, *args]')
    assert not _direct_git_calls('subprocess.run(["ls"])\ngit_safe.run(d, "status")')


def test_no_direct_git_calls_outside_the_allowlist():
    found = {}
    for path in sorted((REPO_ROOT / "looper").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        lines = _direct_git_calls(path.read_text())
        if lines and relative not in DIRECT_GIT_ALLOWED:
            found[relative] = sorted(set(lines))
    assert not found, ("run git through looper.agents.git_safe (or list the file in DIRECT_GIT_ALLOWED with the reason it "
                       f"may not): {found}")
    for relative in DIRECT_GIT_ALLOWED:
        assert (REPO_ROOT / relative).is_file(), f"{relative} is listed but does not exist"


# --- The eval-export committable check runs git through git_safe ---

def test_committable_runs_nothing_a_hostile_repository_configures(repo, probe, marker):
    from looper.agents import leads_commands
    target = repo / "set.jsonl"
    plain(repo, "config", "core.fsmonitor", str(probe))
    plain(repo, "check-ignore", "-q", "--", str(target), check=False)
    assert fired(marker), "positive control: plain git runs the configured fsmonitor for the same check"
    marker.unlink()
    assert "git does not ignore it" in leads_commands.committable(target)
    assert fired(marker) == []


def test_committable_reports_a_refused_repository_as_a_git_that_cannot_answer(repo):
    from looper.agents import leads_commands
    (repo / ".git" / "info" / "attributes").write_text("*.txt filter=evil\n")
    reason = leads_commands.committable(repo / "set.jsonl")
    assert reason is not None and reason.startswith("cannot ask git") and "RepositoryRefused" in reason
