"""The fixed launch configuration of a task Manager's direct session: Claude Code's Bash sandbox plus pinned keys.

A direct session runs the model's Bash inside Claude Code's sandbox; the task directory is untrusted, so the
boundary is these values, written by code into the ``--settings`` file and the launch flags. No configuration layer
can loosen them: the effective settings are read back and compared against ``pins`` (``Pin``, one row per key of
the settings reference that could widen the boundary). Verified against Claude Code 2.1.332 by
``tests/test_task_manager_sandbox_probe.py``; rerun it before changing ``VERIFIED_CLI_VERSION``.

- F1 ``sandbox.enabled``: Bash runs in the sandbox (Seatbelt on macOS).
- F2 ``sandbox.failIfUnavailable``: no silent fallback to an unsandboxed Bash.
- F3 ``filesystem.allowWrite`` stays empty: the writable set is the task directory and the sandbox's temp directory.
- F4 ``network.strictAllowlist``: with ``bypassPermissions`` a connection outside the allowlist would otherwise pass.
- F5 ``allowUnsandboxedCommands`` false: ``dangerouslyDisableSandbox`` is ignored, and project/local layers cannot
  switch the sandbox off.
- F6 (hooks) is not pinned: v1 injects no Looper hook into a direct session.
- F7 ``--setting-sources user``: project and local layers (hooks, ``env``, permissions, ``.mcp.json``) are not loaded.
- F8 ``--strict-mcp-config``: only the generated ``--mcp-config`` loads (``operator_mcp``).
- F9 ``--tools``: an allowlist of built-in tools (``BUILTIN_TOOLS``), because the sandbox covers only Bash children.
  Tool search is pinned off so the allowed MCP servers' tools are present without ``ToolSearch``.
- F10 ``filesystem.denyRead``: credentials and trust roots (``read_deny_paths``); a blacklist, incomplete by nature.
- F11 ``filesystem.denyWrite``: ``.git/hooks``, ``config``, ``info`` and ``modules`` of the task directory.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

# F9: the one place the built-in allowlist lives. Everything else Claude Code ships is out: Read, Grep, Glob, Write,
# Edit, NotebookEdit, WebFetch, WebSearch, Monitor, SendMessage, EnterWorktree, LSP, the Cron tools, TodoWrite,
# Skill and ToolSearch. File reads and writes go through the sandboxed Bash, so ``denyRead`` is the one read scope.
BUILTIN_TOOLS: tuple[str, ...] = ("Bash",)
# Present whatever ``--tools`` says: EndConversation cannot be removed (CLI reference); WaitForMcpServers appears when
# tool search is off. Neither reads, writes or reaches the network.
UNREMOVABLE_TOOLS: tuple[str, ...] = ("EndConversation", "WaitForMcpServers")
# Tool search defers MCP tools behind ToolSearch, which is not allowlisted: pin it off (``env`` of --settings).
TOOL_SEARCH_ENV = ("env", "ENABLE_TOOL_SEARCH")

# The environment a launch must not carry (card 2 asserts it on the effective ``env``).
FORBIDDEN_ENV = ("CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
# Removed from the direct session's process environment (``task_manager_safety.environment``): the forbidden names in
# both cases (a proxy Looper does not control would weaken the sandbox's allowlist), and the ssh agent socket (git over
# SSH is not available in v1, so nothing needs it; denyRead cannot cover a Unix socket). Card 2 asserts the same
# names on the process environment and on the effective settings ``env`` at readback.
DIRECT_DROPPED_ENV = (*FORBIDDEN_ENV, *(name.lower() for name in FORBIDDEN_ENV), "SSH_AUTH_SOCK")

# F10: subdirectories of ~/.config (or $XDG_CONFIG_HOME) that hold credentials. Not the whole directory: git reads
# ~/.config/git/config and other tools keep their settings there.
CONFIG_CREDENTIAL_DIRS = ("gh", "hub", "gcloud", "glab-cli", "op", "Bitwarden CLI")
# Credential files under ~/.config (git keeps its credential store beside its configuration).
CONFIG_CREDENTIAL_FILES = ("git/credentials",)
# Plain files in HOME that hold credentials.
HOME_CREDENTIAL_FILES = (".netrc", ".git-credentials", ".npmrc", ".pypirc", ".vault-token", ".sentryclirc",
                         ".cargo/credentials.toml", ".gem/credentials", ".pulumi/credentials.json")
# Credential directories in HOME (cloud, cluster, registry and infrastructure logins).
HOME_CREDENTIAL_DIRS = (".docker", ".kube", ".azure", ".terraform.d", ".fly")
# Shell history often holds tokens typed on a command line.
HOME_HISTORY_FILES = (".zsh_history", ".bash_history")
# Browser profiles (cookies, saved logins) and cookie stores, relative to HOME.
HOME_BROWSER_PATHS = ("Library/Application Support/Firefox", "Library/Application Support/Google/Chrome",
                      "Library/Application Support/Microsoft Edge", "Library/Application Support/BraveSoftware",
                      "Library/Application Support/Arc", "Library/Cookies")


# Every name of ``child_env.RUNTIMES`` and what a direct session does with it. A name that is not classified here fails
# ``tests/test_task_manager_sandbox.py``: adding one to ``child_env`` means deciding it. "keep": the Claude CLI itself
# needs it (the credentials among them stay visible to the sandboxed Bash: a residual, RFC 25.5). "drop": another
# runtime's home or credential; nothing in a direct session needs it.
RUNTIME_ENV_CLASS: dict[str, str] = {
    "CLAUDE_CONFIG_DIR": "keep", "ANTHROPIC_API_KEY": "keep", "ANTHROPIC_AUTH_TOKEN": "keep",
    "CLAUDE_CODE_OAUTH_TOKEN": "keep", "ANTHROPIC_BASE_URL": "keep", "ANTHROPIC_MODEL": "keep",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "keep", "ANTHROPIC_DEFAULT_SONNET_MODEL": "keep",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "keep", "ANTHROPIC_SMALL_FAST_MODEL": "keep", "CLAUDE_CODE_SUBAGENT_MODEL": "keep",
    "TRAE_HOME": "drop", "CODEX_HOME": "drop", "GROK_HOME": "drop", "COPILOT_HOME": "drop",
    "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "drop", "CODEX_API_KEY": "drop", "CODEX_ACCESS_TOKEN": "drop",
    "XAI_API_KEY": "drop", "GROK_CODE_XAI_API_KEY": "drop", "GROK_AUTH": "drop",
}
# Variables an Agent Profile's own ``env`` may add that a direct session keeps: the Claude CLI's and Anthropic's. Any
# other name a profile sets (``GH_TOKEN``, a cloud key, ...) is not passed: the sandboxed Bash would print it.
PROFILE_ENV_KEEP_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "ENABLE_TOOL_SEARCH", "DISABLE_", "MAX_THINKING_TOKENS", "API_TIMEOUT_MS")


DIRECT_DROPPED_ENV = (*DIRECT_DROPPED_ENV, *(name for name, kind in RUNTIME_ENV_CLASS.items() if kind == "drop"))


def profile_env_dropped(profile_names: Iterable[str], binding_names: Iterable[str] = ()) -> tuple[str, ...]:
    """The names of a profile's ``env`` a direct session does not carry: not the CLI's own, not set by the provider
    binding, not a name the server inherits by allowlist (``child_env.NAMES``)."""
    from .. import child_env

    kept = set(binding_names) | set(child_env.NAMES)
    return tuple(sorted({name for name in profile_names if name not in kept
                         and not name.upper().startswith(PROFILE_ENV_KEEP_PREFIXES)}))


def dropped_env(launch: Any) -> tuple[str, ...]:
    """What a direct session's process environment must not carry for ``launch``: ``DIRECT_DROPPED_ENV`` and the
    names its Agent Profile adds that the CLI does not need (``profile_env_dropped``)."""
    binding = launch.runtime.bind_provider(launch.profile.provider, launch.model, launch.effort)
    return (*DIRECT_DROPPED_ENV, *profile_env_dropped((name for name, _ in launch.env), (name for name, _ in binding.env)))


def tool_allowed(name: str, servers: Iterable[str]) -> bool:
    """Whether ``name`` may be in the launched session's tool list: the built-ins, the expected extras, or a tool of
    one of ``servers`` (Looper's own and the allowed MCP servers)."""
    return (name in BUILTIN_TOOLS or name in UNREMOVABLE_TOOLS
            or any(name.startswith(f"mcp__{server}__") for server in servers))


class Check(StrEnum):
    EQUAL = "equal"  # the effective value equals the pinned value
    ARRAY_EQUAL = "array_equal"  # the effective array equals the pinned one (sources merge, so a pin cannot override)
    CONTAINS = "contains"  # every pinned element is in the effective array; it may hold more (only narrower)
    ABSENT = "absent"  # the key must not be set by anyone
    NOT_ASSERTED = "not_asserted"  # a known key that is not compared


@dataclass(frozen=True)
class Pin:
    item: str  # the RFC name used in a refusal's details: "F1".."F11", else the dotted key
    key: tuple[str, ...]
    value: Any
    check: Check


def git_deny_write(work_dir: Path) -> tuple[str, ...]:
    """F11: the parts of the task directory's ``.git`` a Bash child must not write (absolute paths under the real
    path of ``work_dir``). ``hooks`` and ``config`` are protected by default; ``info`` (``attributes`` is not
    neutralized by ``--attr-source``) and ``modules`` (a submodule's own config and hooks) are not."""
    root = os.path.realpath(work_dir)
    return tuple(f"{root}/.git/{part}" for part in ("hooks", "config", "info", "modules"))


def pins(work_dir: Path, *, allowed_domains: Sequence[str], read_deny: Sequence[str]) -> tuple[Pin, ...]:
    """Every key the readback compares (RFC 25.2.1), in order."""
    eq, arr, has, absent, free = Check.EQUAL, Check.ARRAY_EQUAL, Check.CONTAINS, Check.ABSENT, Check.NOT_ASSERTED
    return (
        Pin("F1", ("sandbox", "enabled"), True, eq),
        Pin("F2", ("sandbox", "failIfUnavailable"), True, eq),
        Pin("F5", ("sandbox", "allowUnsandboxedCommands"), False, eq),
        Pin("sandbox.enableWeakerNetworkIsolation", ("sandbox", "enableWeakerNetworkIsolation"), False, eq),
        Pin("sandbox.enableWeakerNestedSandbox", ("sandbox", "enableWeakerNestedSandbox"), False, eq),
        Pin("sandbox.allowAppleEvents", ("sandbox", "allowAppleEvents"), False, eq),
        Pin("sandbox.allowNestedUserNamespaces", ("sandbox", "allowNestedUserNamespaces"), False, eq),
        Pin("F4", ("sandbox", "network", "strictAllowlist"), True, eq),
        Pin("sandbox.network.allowAllUnixSockets", ("sandbox", "network", "allowAllUnixSockets"), False, eq),
        Pin("sandbox.network.allowLocalBinding", ("sandbox", "network", "allowLocalBinding"), False, eq),
        Pin("sandbox.filesystem.disabled", ("sandbox", "filesystem", "disabled"), False, eq),
        Pin("disableSkillShellExecution", ("disableSkillShellExecution",), True, eq),
        Pin("crossSessionInbound", ("crossSessionInbound",), "refuse", eq),
        Pin("F9", TOOL_SEARCH_ENV, "false", eq),
        Pin("sandbox.excludedCommands", ("sandbox", "excludedCommands"), [], arr),
        Pin("sandbox.network.allowedDomains", ("sandbox", "network", "allowedDomains"), list(allowed_domains), arr),
        Pin("sandbox.network.allowUnixSockets", ("sandbox", "network", "allowUnixSockets"), [], arr),
        Pin("sandbox.network.allowMachLookup", ("sandbox", "network", "allowMachLookup"), [], arr),
        Pin("F3", ("sandbox", "filesystem", "allowWrite"), [], arr),
        Pin("permissions.additionalDirectories", ("permissions", "additionalDirectories"), [], arr),
        Pin("F11", ("sandbox", "filesystem", "denyWrite"), list(git_deny_write(work_dir)), has),
        Pin("F10", ("sandbox", "filesystem", "denyRead"), list(read_deny), has),
        Pin("sandbox.network.httpProxyPort", ("sandbox", "network", "httpProxyPort"), None, absent),
        Pin("sandbox.network.socksProxyPort", ("sandbox", "network", "socksProxyPort"), None, absent),
        Pin("sandbox.network.tlsTerminate", ("sandbox", "network", "tlsTerminate"), None, absent),
        Pin("sandbox.filesystem.allowRead", ("sandbox", "filesystem", "allowRead"), None, absent),
        Pin("sandbox.credentials", ("sandbox", "credentials"), None, absent),
        Pin("sandbox.ignoreViolations", ("sandbox", "ignoreViolations"), None, absent),
        *(Pin(".".join(key), key, None, free) for key in (
            ("sandbox", "autoAllowBashIfSandboxed"), ("sandbox", "bwrapPath"), ("sandbox", "socatPath"),
            ("sandbox", "ripgrep"), ("sandbox", "network", "allowManagedDomainsOnly"),
            ("sandbox", "filesystem", "allowManagedReadPathsOnly"), ("sandbox", "network", "deniedDomains"))),
    )


# Every ``sandbox.*`` key the pins name; the readback refuses any other as ``unknown_key``.
KNOWN_SANDBOX_KEYS: frozenset[tuple[str, ...]] = frozenset(
    pin.key for pin in pins(Path("/"), allowed_domains=(), read_deny=()) if pin.key[0] == "sandbox")


def read_deny_paths(environment: Mapping[str, str], runtime_root: Path,
                    config_file: Path | None = None) -> tuple[str, ...]:
    """F10: the operator's credentials and trust roots, as real paths without duplicates; ``environment`` is the CLI's
    (``launch.runtime.environment(launch)``); ``config_file`` is Looper's ops file when it lives outside
    ``runtime_root``. A blacklist: it cannot say "read only the task directory"."""
    from .runtimes import coco_login, codex_login, grok_login

    home = Path(environment.get("HOME") or Path.home())
    config_home = Path(environment["XDG_CONFIG_HOME"]) if environment.get("XDG_CONFIG_HOME") else home / ".config"
    custom_claude = environment.get("CLAUDE_CONFIG_DIR")
    claude_dirs = [Path(custom_claude).expanduser()] if custom_claude else []
    claude_dirs += [home / ".claude"]  # also the default one when another is in use: an old login may remain there
    # Agent runtimes' own logins: the default home directory, and the one a variable names (the runtimes' helpers say
    # which file is the login, and so which directory is the home).
    logins = []
    for variable, default, helper in (("CODEX_HOME", ".codex", codex_login), ("GROK_HOME", ".grok", grok_login),
                                      ("TRAE_HOME", ".trae", coco_login)):
        logins.append(home / default)
        if (environment.get(variable) or "").strip():
            logins.append(helper.login_file(environment).parents[len(Path(helper.LOGIN_FILE).parts) - 1])
    # The Copilot CLI (omp's provider) keeps its login in its home; it has no login helper here, so the paths are listed.
    logins.append(home / ".copilot")
    if (environment.get("COPILOT_HOME") or "").strip():
        logins.append(Path(environment["COPILOT_HOME"]).expanduser())
    entries = (
        home / ".ssh",  # SSH keys
        home / ".aws",  # cloud credentials
        home / ".gnupg",  # GPG keys
        home / "Library" / "Keychains",  # the login keychain: the CLI sandbox lets `security` reach it (Claude Code's
        # own login, git hosting tokens), and a sandboxed Bash can read an item whose ACL trusts /usr/bin/security
        *(home / name for name in (*HOME_CREDENTIAL_FILES, *HOME_CREDENTIAL_DIRS, *HOME_HISTORY_FILES,
                                   *HOME_BROWSER_PATHS)),
        *(config_home / name for name in (*CONFIG_CREDENTIAL_DIRS, *CONFIG_CREDENTIAL_FILES)),  # CLI credential stores
        *claude_dirs,  # Claude Code's whole configuration directory: login, settings with `env`, session transcripts
        home / ".claude.json",  # MCP server definitions with their secrets (inside the directory above when it is set)
        *logins,
        home / ".looper",  # the default runtime root, when the active one is elsewhere
        runtime_root,  # Looper's ops file, tokens, generated MCP configs and launch settings
        *([config_file] if config_file is not None else []),  # the ops file, when it lives elsewhere
    )
    return tuple(dict.fromkeys(os.path.realpath(entry) for entry in entries))


def _set(tree: dict[str, Any], key: tuple[str, ...], value: Any) -> None:
    for part in key[:-1]:
        tree = tree.setdefault(part, {})
    tree[key[-1]] = value


def settings_content(work_dir: Path, *, allowed_domains: Sequence[str] = (),
                     read_deny: Sequence[str] = ()) -> dict[str, Any]:
    """The nested ``--settings`` object: the value of every EQUAL, ARRAY_EQUAL and CONTAINS pin. No ``hooks``, no
    ``disableAllHooks`` (F6). ``allowed_domains`` arrive validated; here only their type is checked."""
    if isinstance(allowed_domains, str) or not all(isinstance(domain, str) for domain in allowed_domains):
        raise TypeError("allowed_domains must be a sequence of str")
    content: dict[str, Any] = {}
    for pin in pins(work_dir, allowed_domains=allowed_domains, read_deny=read_deny):
        if pin.check in (Check.EQUAL, Check.ARRAY_EQUAL, Check.CONTAINS):
            _set(content, pin.key, pin.value)
    return content


def launch_args() -> list[str]:
    """F7 and F9. ``--tools`` takes several values, so the comma-joined value is followed by a flag (the caller's
    next argument), never by a free value."""
    return ["--setting-sources", "user", "--tools", ",".join(BUILTIN_TOOLS)]


# The only platform the boundary is verified on (a test of another platform's launch path overrides it).
SUPPORTED_PLATFORM = "darwin"


def platform_problem() -> str | None:
    """Why this machine cannot run a direct session, or ``None``: v1 supports macOS only."""
    if sys.platform == SUPPORTED_PLATFORM:
        return None
    return ("The task Manager's direct session runs only on macOS for now: its boundary is Claude Code's Bash sandbox, "
            f"which is verified there only (this machine: {sys.platform}).")
