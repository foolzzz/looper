"""Fail-closed checks for the review hook in an operator-environment task Manager."""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .. import child_env
from .runtimes.claude_code import MANAGED_SETTINGS

if TYPE_CHECKING:
    from .agent_settings import Launch

# Required upgrade check: run tests/test_task_manager_launch_probe.py against the new CLI before updating this
# constant. Both lower versions and unverified higher versions refuse direct launch and resume.
VERIFIED_CLI_VERSION = "2.1.332"
HOOK_ENV = ("CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE")


class Source(str, Enum):
    USER = "user"
    USER_LOCAL = "user_local"
    PROJECT = "project"
    PROJECT_LOCAL = "project_local"
    MANAGED = "managed"
    LAUNCH = "launch"
    PROCESS = "process"


class Reason(str, Enum):
    REVIEW_HOOK_DISABLED = "review_hook_disabled"
    CLI_UNVERIFIED = "cli_unverified"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    READBACK_UNAVAILABLE = "readback_unavailable"
    LAUNCH_SETTING_MISMATCH = "launch_setting_mismatch"
    MCP_SET_MISMATCH = "mcp_set_mismatch"
    MCP_LAUNCHER_UNSAFE = "mcp_launcher_unsafe"
    NETWORK_POLICY_INVALID = "network_policy_invalid"
    WORKSPACE_NOT_REPO_ROOT = "workspace_not_repo_root"
    WORKSPACE_IS_HOME = "workspace_is_home"
    WORKSPACE_CONTAINS_TRUSTED_CONFIG = "workspace_contains_trusted_config"
    WORKSPACE_IN_SYSTEM_DIR = "workspace_in_system_dir"
    WORKSPACE_OVERLAPS_SESSION = "workspace_overlaps_session"
    WORKTREE_UNSUPPORTED = "worktree_unsupported"
    HEAD_MOVED = "head_moved"
    HEAD_DETACHED = "head_detached"


class Predicate(str, Enum):
    IS_TRUE = "is_true"
    EQ_STRING_1 = "eq_string_1"
    TRUE_OR_CONTAINS_HOOKS = "true_or_contains_hooks"

    def matches(self, value: Any) -> bool:
        if self is Predicate.IS_TRUE:
            return value is True
        if self is Predicate.EQ_STRING_1:
            return value == "1"
        return value is True or isinstance(value, list) and "hooks" in value


@dataclass(frozen=True)
class Rule:
    source: Source
    key: tuple[str, ...]
    predicate: Predicate
    statically_confirmed: bool = False


# The operator-launch probe proves the dynamic rows on VERIFIED_CLI_VERSION. Managed policy rows are statically
# confirmed only: never edit policy files, and refuse rather than assume that an unprobed policy permits a hook.
RULES = (
    *(Rule(source, ("disableAllHooks",), Predicate.IS_TRUE,
           statically_confirmed=source is Source.MANAGED) for source in Source if source is not Source.PROCESS),
    *(Rule(source, ("env", name), Predicate.EQ_STRING_1) for source in (Source.USER, Source.LAUNCH) for name in HOOK_ENV),
    *(Rule(Source.PROCESS, (name,), Predicate.EQ_STRING_1) for name in HOOK_ENV),
    Rule(Source.MANAGED, ("allowManagedHooksOnly",), Predicate.IS_TRUE, True),
    Rule(Source.MANAGED, ("strictPluginOnlyCustomization",),
         Predicate.TRUE_OR_CONTAINS_HOOKS, True),
)


@dataclass(frozen=True)
class Refusal:
    reason: Reason
    notice: str
    version: str = "unknown"


# One increment per refused launch/resume, not per file or key hit. These dimensions contain no settings values.
refusal_counts: Counter[tuple[str, str, str]] = Counter()


def record_refusal(reason: Reason, phase: Literal["launch", "resume", "turn"], version: str = "unknown") -> None:
    """Count one refused launch, resume or turn (``task_manager_launch_refusals_total``)."""
    refusal_counts[(reason.value, phase, version)] += 1


def environment(launch: Launch, task_id: str) -> dict[str, str]:
    env = launch.runtime.environment(launch)
    for key in (*HOOK_ENV, child_env.AGENT_RUN_ENV):
        env.pop(key, None)
    env[child_env.TASK_MANAGER_ENV] = task_id
    return env


def _hits(source: Source, values: Mapping[str, Any]) -> list[str]:
    found = []
    for rule in RULES:
        if rule.source is not source:
            continue
        value: Any = values
        for key in rule.key:
            value = value.get(key) if isinstance(value, Mapping) else None
        if rule.predicate.matches(value):
            found.append(".".join(rule.key))
    return found


async def gate(executable: str, *, cwd: str | Path, environment: Mapping[str, str],
               phase: Literal["launch", "resume"], settings_file: Path | None = None,
               review_hook: bool = True) -> Refusal | None:
    """Read every source before every direct launch or resume; no model process is started here.

    ``review_hook=False`` is a direct session that carries no Looper hook (the sandbox is its boundary): the
    ``RULES`` checks, which guard that hook against being switched off, are skipped. The CLI version check runs on
    every launch and resume either way."""
    home = Path(environment.get("HOME", str(Path.home())))
    config = Path(environment.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))).expanduser()
    sources = [(Source.USER, config / "settings.json"), (Source.USER_LOCAL, config / "settings.local.json"),
               (Source.PROJECT, Path(cwd) / ".claude/settings.json"),
               (Source.PROJECT_LOCAL, Path(cwd) / ".claude/settings.local.json")]
    sources += [(Source.MANAGED, path) for path in MANAGED_SETTINGS]
    if settings_file is not None:
        sources.append((Source.LAUNCH, settings_file))
    notices = []
    for source, path in (sources if review_hook else []):
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError("settings must be an object")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            notices.append(f"{path}: settings cannot be checked ({type(exc).__name__}); repair the file before retrying.")
            continue
        for key in _hits(source, values):
            notices.append(f"{path}: {key} disables the review hook; remove this setting before retrying.")
    # Parent variables are checked even though the child allowlist removes them. Profile/provider variables are
    # checked by the caller's unsanitized environment too; environment() clears them from the final child.
    for key in dict.fromkeys((*_hits(Source.PROCESS, os.environ), *_hits(Source.PROCESS, environment))
                             if review_hook else ()):
        notices.append(f"process environment: {key} disables the review hook; remove this variable before retrying.")
    if notices:
        refusal = Refusal(Reason.REVIEW_HOOK_DISABLED, "\n".join(notices))
    else:
        version = "unknown"
        try:
            process = await asyncio.create_subprocess_exec(executable, "--version", env=dict(environment),
                                                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            text = stdout.decode("utf-8", errors="replace").strip()
            match = re.match(r"^(\d+\.\d+\.\d+)(?:\s|$)", text)
            version = match[1] if match else text[:100] or "unknown"
            if process.returncode == 0 and version == VERIFIED_CLI_VERSION:
                return None
        except (OSError, asyncio.TimeoutError):
            pass
        refusal = Refusal(Reason.CLI_UNVERIFIED,
                          f"Claude Code {version} is unverified for the task Manager review hook; "
                          f"use {VERIFIED_CLI_VERSION} or run the required CLI upgrade probe.", version)
    record_refusal(refusal.reason, phase, refusal.version)
    return refusal
