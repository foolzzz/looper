"""The fixed launch configuration of a task Manager's direct session (``task_manager_sandbox``)."""
from __future__ import annotations

import json
import os

import pytest

from looper.agents import task_manager_sandbox as sandbox
from looper.agents.task_manager_sandbox import BUILTIN_TOOLS, Check


@pytest.mark.parametrize("tool", BUILTIN_TOOLS)
def test_the_builtin_allowlist_is_allowed(tool):
    assert sandbox.tool_allowed(tool, [])


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch",
                                  "Monitor", "SendMessage", "EnterWorktree", "LSP", "CronCreate", "TodoWrite",
                                  "Skill", "ToolSearch"])
def test_other_builtins_are_not_allowed_unless_the_allowlist_names_them(tool):
    assert sandbox.tool_allowed(tool, []) is (tool in BUILTIN_TOOLS)


def test_extras_the_cli_adds_and_server_tools():
    for name in sandbox.UNREMOVABLE_TOOLS:
        assert sandbox.tool_allowed(name, [])
    assert sandbox.tool_allowed("mcp__looper__task_get", ["looper"])
    assert not sandbox.tool_allowed("mcp__other__x", ["looper"])
    assert not sandbox.tool_allowed("mcp__looper", ["looper"])  # a server's name alone is not a tool


def test_launch_args_pass_one_comma_joined_value_followed_by_a_flag():
    assert sandbox.launch_args() == ["--setting-sources", "user", "--tools", ",".join(BUILTIN_TOOLS)]


def test_git_deny_write_is_absolute_under_the_real_path(tmp_path):
    link = tmp_path / "link"
    real = tmp_path / "real"
    real.mkdir()
    link.symlink_to(real)
    root = os.path.realpath(real)
    assert sandbox.git_deny_write(link) == tuple(
        f"{root}/.git/{p}" for p in ("hooks", "config", "info", "modules", "objects/info"))


def test_settings_content_holds_every_pinned_value_and_nothing_else(tmp_path):
    deny = ("/x/.ssh", "/x/.aws")
    content = sandbox.settings_content(tmp_path, allowed_domains=("example.com",), read_deny=deny)
    sb = content["sandbox"]
    assert content["crossSessionInbound"] == "refuse" and content["disableSkillShellExecution"] is True
    assert content["env"] == {"ENABLE_TOOL_SEARCH": "false"}
    assert content["permissions"] == {"additionalDirectories": []}
    assert (sb["enabled"], sb["failIfUnavailable"], sb["allowUnsandboxedCommands"]) == (True, True, False)
    assert sb["excludedCommands"] == [] and sb["filesystem"]["allowWrite"] == []
    assert sb["filesystem"]["denyRead"] == list(deny) and sb["filesystem"]["disabled"] is False
    assert sb["network"]["strictAllowlist"] is True and sb["network"]["allowedDomains"] == ["example.com"]
    assert sb["network"]["allowUnixSockets"] == [] and sb["network"]["allowMachLookup"] == []
    for key in ("allowAppleEvents", "allowNestedUserNamespaces", "enableWeakerNetworkIsolation",
                "enableWeakerNestedSandbox"):
        assert sb[key] is False
    # Hooks are not ours to set (F6), and a key that must be absent is never written.
    assert "hooks" not in content and "disableAllHooks" not in content
    assert not ({"httpProxyPort", "socksProxyPort", "tlsTerminate"} & set(sb["network"]))
    assert not ({"allowRead"} & set(sb["filesystem"])) and not ({"credentials", "ignoreViolations"} & set(sb))


def test_every_pin_is_consistent_with_its_check(tmp_path):
    rows = sandbox.pins(tmp_path, allowed_domains=(), read_deny=())
    assert len({row.key for row in rows}) == len(rows)
    for row in rows:
        if row.check in (Check.ABSENT, Check.NOT_ASSERTED):
            assert row.value is None
        if row.check is Check.ARRAY_EQUAL or row.check is Check.CONTAINS:
            assert isinstance(row.value, list)
    assert {row.item for row in rows} >= {"F1", "F2", "F3", "F4", "F5", "F9", "F10", "F11"}


def test_known_sandbox_keys_cover_every_sandbox_pin_and_not_other_keys(tmp_path):
    named = {row.key for row in sandbox.pins(tmp_path, allowed_domains=(), read_deny=()) if row.key[0] == "sandbox"}
    assert sandbox.KNOWN_SANDBOX_KEYS == named
    assert ("sandbox", "network", "allowedDomains") in named and ("sandbox", "autoAllowBashIfSandboxed") in named
    assert ("crossSessionInbound",) not in named


@pytest.mark.parametrize("domains", ["example.com", [1], [None]])
def test_allowed_domains_must_be_a_sequence_of_strings(tmp_path, domains):
    with pytest.raises(TypeError):
        sandbox.settings_content(tmp_path, allowed_domains=domains)


def test_two_allowlists_or_deny_lists_give_two_contents(tmp_path):
    contents = {json.dumps(sandbox.settings_content(tmp_path, allowed_domains=d, read_deny=r), sort_keys=True)
                for d, r in [((), ()), (("a.example",), ()), (("b.example",), ()), ((), ("/a",)), ((), ("/b",))]}
    assert len(contents) == 5


def test_read_deny_paths_cover_credentials_and_trust_roots_as_real_paths(tmp_path):
    home, config, runtime = tmp_path / "home", tmp_path / "claude-config", tmp_path / "runtime"
    for path in (home, config, runtime):
        path.mkdir()
    link = tmp_path / "home-link"
    link.symlink_to(home)
    env = {"HOME": str(link), "CLAUDE_CONFIG_DIR": str(config)}
    found = sandbox.read_deny_paths(env, runtime)
    real = os.path.realpath
    for expected in (home / ".ssh", home / ".aws", home / ".gnupg", home / ".netrc", home / ".git-credentials",
                     home / ".config/gh", home / ".config/hub", home / ".config/gcloud",
                     home / ".config/git/credentials", home / "Library/Keychains", config, runtime):
        assert real(expected) in found, expected
    assert len(found) == len(set(found)) and all(os.path.isabs(path) for path in found)
    # Not the whole configuration directory: git keeps its configuration in ~/.config/git.
    assert real(home / ".config") not in found and real(home / ".config/git") not in found


def test_xdg_config_home_replaces_the_default_and_home_is_the_claude_default(tmp_path):
    home, xdg, runtime = tmp_path / "home", tmp_path / "xdg", tmp_path / "runtime"
    found = sandbox.read_deny_paths({"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}, runtime)
    real = os.path.realpath
    assert real(xdg / "gh") in found and real(home / ".config/gh") not in found
    assert real(home / ".claude") in found and real(home / ".claude.json") in found


def test_platform_problem(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert sandbox.platform_problem() is None
    monkeypatch.setattr("sys.platform", "linux")
    assert "macOS" in sandbox.platform_problem()


def test_an_ops_file_outside_the_runtime_root_is_denied_too(tmp_path):
    ops = tmp_path / "elsewhere" / "ops.json"
    found = sandbox.read_deny_paths({"HOME": str(tmp_path / "home")}, tmp_path / "runtime", ops)
    assert os.path.realpath(ops) in found
    assert os.path.realpath(ops) not in sandbox.read_deny_paths({"HOME": str(tmp_path / "home")}, tmp_path / "runtime")


def test_dropped_environment_covers_proxies_in_both_cases_and_the_ssh_agent():
    for name in sandbox.FORBIDDEN_ENV:
        assert name in sandbox.DIRECT_DROPPED_ENV and name.lower() in sandbox.DIRECT_DROPPED_ENV
    assert "SSH_AUTH_SOCK" in sandbox.DIRECT_DROPPED_ENV


def test_whole_claude_config_dir_and_the_default_one_are_denied(tmp_path):
    home, config = tmp_path / "home", tmp_path / "custom-claude"
    found = sandbox.read_deny_paths({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config)}, tmp_path / "runtime")
    real = os.path.realpath
    # The custom directory whole (settings with `env`, transcripts), and the default one an old login may remain in.
    assert {real(config), real(home / ".claude"), real(home / ".claude.json")} <= set(found)
    assert real(config / "settings.json") not in found  # covered by the directory entry, not listed alone


@pytest.mark.parametrize("relative", [
    ".docker", ".kube", ".azure", ".terraform.d", ".npmrc", ".pypirc", ".vault-token", ".cargo/credentials.toml",
    ".config/Bitwarden CLI", "Library/Application Support/Firefox"])
def test_p0_credential_locations_are_denied(tmp_path, relative):
    home = tmp_path / "home"
    assert os.path.realpath(home / relative) in sandbox.read_deny_paths({"HOME": str(home)}, tmp_path / "runtime")


@pytest.mark.parametrize("relative", [
    ".zsh_history", ".bash_history", "Library/Application Support/Google/Chrome",
    "Library/Application Support/Microsoft Edge", "Library/Application Support/BraveSoftware",
    "Library/Application Support/Arc", "Library/Cookies", ".looper"])
def test_p1_history_browser_and_default_looper_locations_are_denied(tmp_path, relative):
    home = tmp_path / "home"
    assert os.path.realpath(home / relative) in sandbox.read_deny_paths({"HOME": str(home)}, tmp_path / "runtime")


def test_not_the_shell_startup_file(tmp_path):
    home = tmp_path / "home"  # sourced before every command: denying it would break each one (a follow-up)
    assert os.path.realpath(home / ".zshenv") not in sandbox.read_deny_paths({"HOME": str(home)}, tmp_path / "runtime")


def test_agent_runtime_logins_are_denied_by_default_and_where_a_variable_points(tmp_path):
    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    real = os.path.realpath
    found = sandbox.read_deny_paths({"HOME": str(home)}, tmp_path / "runtime")
    assert {real(home / ".codex"), real(home / ".grok"), real(home / ".trae")} <= set(found)
    env = {"HOME": str(home), "CODEX_HOME": str(elsewhere / "c"), "GROK_HOME": str(elsewhere / "g"),
           "TRAE_HOME": str(elsewhere / "t")}
    found = sandbox.read_deny_paths(env, tmp_path / "runtime")
    assert {real(elsewhere / "c"), real(elsewhere / "g"), real(elsewhere / "t")} <= set(found)  # the homes, not auth files


def test_every_non_anthropic_provider_credential_is_dropped_from_a_direct_session():
    from looper import child_env

    expected = {"CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "XAI_API_KEY", "GROK_CODE_XAI_API_KEY", "GROK_AUTH"}
    assert expected <= set(sandbox.DIRECT_DROPPED_ENV) and set(sandbox.PROVIDER_CREDENTIAL_ENV) <= set(child_env.RUNTIMES)
    assert not any(name.startswith(("ANTHROPIC", "CLAUDE")) for name in sandbox.PROVIDER_CREDENTIAL_ENV)
