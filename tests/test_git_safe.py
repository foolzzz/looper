"""``looper.git_safe``: git run in a directory an agent writes does not run the repository's own code.

Each mechanism is armed in a temporary repository with a probe that appends to a marker file. The marker fires with
plain git (the positive control: the mechanism is real here) and stays empty through the helper.
"""

from __future__ import annotations

import ast
import asyncio
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from looper import git_safe

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
    return subprocess.run(["git", "-C", str(repo), *args], env=_PLAIN_ENV, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, check=check, timeout=30)


def safe(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return git_safe.run(repo, *args, extra_env=_IDENTITY, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                        check=check, timeout=30)


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
    assert git_safe.check_repository(repo) == "sha1"
    assert git_safe.check_repository(repo / "sub") == "sha1"
    assert git_safe.check_repository(tmp_path / "wt") == "sha1"
    assert git_safe.check_repository(repo / ".git") == "sha1"  # inside the git dir: no work tree, still checked
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
    assert git_safe.check_repository(module) == "sha1"
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
    assert git_safe.check_repository(moved, layout=False) == "sha1"
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.txt filter=evil\n")
    with pytest.raises(git_safe.RepositoryRefused) as refused:  # the attributes are checked either way
        git_safe.check_repository(moved, layout=False)
    assert refused.value.reason == "attributes_driver"


def test_outside_a_repository_the_command_runs_and_fails_as_before(tmp_path):
    plain_run = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"], env=_PLAIN_ENV,
                               capture_output=True, text=True)
    assert git_safe.check_repository(tmp_path) is None
    done = safe(tmp_path, "rev-parse", "--show-toplevel")
    assert (done.returncode, done.stdout) == (plain_run.returncode, plain_run.stdout)
    assert safe(tmp_path / "missing", "rev-parse", "HEAD").returncode == 128


def test_sha256_repositories_get_their_own_empty_tree(tmp_path):
    directory = tmp_path / "r256"
    directory.mkdir()
    if plain(directory, "init", "-q", "--object-format=sha256", check=False).returncode != 0:
        pytest.skip("this git cannot make a sha256 repository")
    (directory / "f").write_text("x\n")
    assert git_safe.check_repository(directory) == "sha256"
    assert safe(directory, "status", "--porcelain").stdout.strip() == "?? f"


def test_spawn_runs_through_the_same_checks(repo, probe, marker):
    async def go():
        _arm_fsmonitor(repo, probe)
        process = await git_safe.spawn(repo, "status", "--porcelain", stdout=asyncio.subprocess.PIPE,
                                       stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
        out, _ = await process.communicate()
        assert out.decode().strip() == "M a.txt"
        _arm_info_attributes(repo, probe)
        with pytest.raises(git_safe.RepositoryRefused):
            await git_safe.spawn(repo, "status")

    asyncio.run(go())
    assert fired(marker) == []


def test_call_sites_treat_a_refused_repository_as_a_failed_git(repo, probe, tmp_path):
    from looper.agents import consult, step_workspaces

    _arm_info_attributes(repo, probe)

    async def go():
        with pytest.raises(step_workspaces.GitError, match="attributes_driver"):
            await step_workspaces._git(repo, "status")

    asyncio.run(go())
    done = consult._git(repo, "status")
    assert done.returncode == 128 and "info/attributes" in done.stderr
    assert consult._snapshot(repo, tmp_path / "snap") is None


# --- No direct git call in looper/ outside the places listed here ---

# A file may run git directly only for a directory Looper owns the content of (its own checkout, a clone it just
# made from a remote, the operator's knowledge vault whose hooks are meant to run) or where there is no repository.
DIRECT_GIT_ALLOWED = {
    "looper/git_safe.py": "the helper itself",
    "looper/components.py": "Looper's own checkout (component install and upgrade)",
    "looper/agents/service.py": "Looper's own checkout (self-upgrade)",
    "looper/agents/catalog_sources.py": "a shallow clone of a remote into a new temporary directory, already neutralised",
    "looper/agents/self_check.py": "`git --version`, no repository",
    "looper/agents/leads_commands.py": "the operator's own CLI command, run in the directory the operator names",
    "looper/agents/vault.py": "the operator's knowledge vault: its commit hooks run by design",
    "looper/agents/vault_commit.py": "the operator's knowledge vault: its commit hooks run by design",
    "looper/agents/memory_settings.py": "the operator's knowledge vault",
}
_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output", "create_subprocess_exec", "create_subprocess_shell"}


def _is_git_word(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "git"


def _starts_with_git(node: ast.AST) -> bool:
    return isinstance(node, (ast.List, ast.Tuple)) and bool(node.elts) and _is_git_word(node.elts[0])


def _direct_git_calls(source: str) -> list[int]:
    """Lines that start git directly: an argv (a list or tuple starting with "git") passed to a process starter,
    or one whose next item is an option or a ``*args`` (``("git", "-c", ...)``, ``["git", *args]``);
    ``create_subprocess_exec("git", ...)``; ``shutil.which("git")``."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) > 1 and _is_git_word(node.elts[0]):
            second = node.elts[1]
            if isinstance(second, ast.Starred) or (isinstance(second, ast.Constant) and isinstance(second.value, str)
                                                    and second.value.startswith("-")):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in _SPAWNERS and node.args and (_is_git_word(node.args[0]) or _starts_with_git(node.args[0])):
                lines.append(node.lineno)
            elif name == "which" and node.args and _is_git_word(node.args[0]):
                lines.append(node.lineno)
    return lines


def test_direct_git_calls_are_found_by_the_scan():
    assert _direct_git_calls('import subprocess\nsubprocess.run(["git", "status"])') == [2]
    assert not _direct_git_calls('x = (("git", "git"),)\ny = name not in ("git", "manager_read")')
    assert _direct_git_calls('async def f():\n    await asyncio.create_subprocess_exec("git", "-C", d)')
    assert _direct_git_calls('x = ("git", "-c", "a=b")')
    assert _direct_git_calls('git = shutil.which("git")')
    assert not _direct_git_calls('subprocess.run(["ls"])\ngit_safe.run(d, "status")')


def test_no_direct_git_calls_outside_the_allowlist():
    found = {}
    for path in sorted((REPO_ROOT / "looper").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        lines = _direct_git_calls(path.read_text())
        if lines and relative not in DIRECT_GIT_ALLOWED:
            found[relative] = sorted(set(lines))
    assert not found, ("run git through looper.git_safe (or list the file in DIRECT_GIT_ALLOWED with the reason it "
                       f"may not): {found}")
    for relative in DIRECT_GIT_ALLOWED:
        assert (REPO_ROOT / relative).is_file(), f"{relative} is listed but does not exist"
