"""A run's access level (``agent_settings.Access``): what each level makes of a launch, and the command lines of every
role and runtime, which stay as they were before the level replaced ``Launch.consult``.

The digest below was computed on ``main`` before the access level existed; a change to it is a change to some agent's
command line, which this refactor must not make.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import looper
from looper.agents.agent_settings import Access, AgentSettings, AgentType, Launch
from looper.agents.profile_registry import LegacyProviderSpec as ProviderSpec, RuntimeSpec, builtin_resolved_profile
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported, adapter
from tests.runtime_model_helpers import resolved_profile

# sha256 of ``_command_lines`` on main 961070ce4, before the access level (the same on macOS and Linux).
COMMAND_LINES_SHA256 = "73e7b9e04fd07ff27d649609434cfe7754dc0e85d680f6fd6f124838a74f2183"
# Keep the historical snapshot's runtime set fixed; later adapters have their own launch tests.
BASELINE_RUNTIMES = ("api", "claude-code", "coco", "codex", "cursor-agent", "dsh", "grok", "omp", "pi")

ROLES = (AgentType.MANAGER, AgentType.PLANNER, AgentType.WORKER, AgentType.REVIEWER, AgentType.PROCESSOR)


# The account each runtime runs on here: Claude Code's own login (the built-in profile); the runtimes that run only on
# their own login, on it; DSH on an Anthropic Messages endpoint; the others on an OpenAI Responses endpoint.
_LOGIN_RUNTIMES = ("coco", "cursor-agent", "grok")


def _profile(runtime: str) -> Any:
    if runtime == "claude-code":
        return builtin_resolved_profile()
    if runtime in _LOGIN_RUNTIMES:
        provider = ProviderSpec(f"prov-{runtime}-login", f"{runtime}-login", AuthKind.OAUTH,
                                frozenset({ApiProtocol.OPENAI_RESPONSES}), oauth_runtime_type=runtime)
    else:
        protocol = ApiProtocol.ANTHROPIC_MESSAGES if runtime == "dsh" else ApiProtocol.OPENAI_RESPONSES
        provider = ProviderSpec("prov-cpa", "cpa", AuthKind.API_KEY, frozenset({protocol}),
                                base_url="http://127.0.0.1:8317/v1", api_key="sk-test-0123456789")
    levels = adapter(runtime).effort_levels
    effort = None if levels is not None and "low" not in levels else "low"
    model = "deepseek-flash" if runtime == "dsh" else "gpt-5.5"
    return resolved_profile(f"agent-{runtime}", f"{runtime}/{model}",
                            RuntimeSpec(f"rt-{runtime}", runtime, runtime, runtime, adapter(runtime).capabilities),
                            provider, model, effort)


def _launches(runtime: str) -> dict[str, Launch]:
    base = Launch(AgentSettings(profile=_profile(runtime)))
    launches = {role.value: replace(base, role=role) for role in ROLES}
    launches["none"] = base
    launches["consult"] = _consult(replace(base, role=AgentType.WORKER))
    return launches


def _consult(launch: Launch) -> Launch:
    """``launch`` as a room consult's."""
    return replace(launch, access=Access.CONSULT)


# A fresh directory a runtime makes for each run (pi's ``run-<random>``, Grok's ``runs/<hash>``, Cursor's temporary
# directory).
_FRESH_DIR = re.compile(r"/run-[A-Za-z0-9_]{8}|/runs/[0-9a-f]{64}|/looper-cur-[0-9a-f]{12}")
_REPO = str(Path(looper.__file__).resolve().parents[1])


def _normalized(value: Any, root: Path) -> Any:
    """``value`` with what differs between machines and runs replaced: the root (both spellings), the checkout, fresh
    directories, and the memory files above the root that Claude Code's settings exclude (its parents and the home
    directory, which the access level does not touch)."""
    roots = sorted({str(root.resolve()), str(root)}, key=len, reverse=True)

    def local(text: str) -> str:
        for name in roots:
            text = text.replace(name, "<root>")
        return _FRESH_DIR.sub("/<fresh>", text.replace(_REPO, "<repo>"))

    def walk(item: Any) -> Any:
        if isinstance(item, list):
            return [walk(part) for part in item]
        if isinstance(item, dict):
            return {key: walk(part) for key, part in item.items()}
        if isinstance(item, str) and item.startswith("{") and "claudeMdExcludes" in item:
            settings = json.loads(item)
            kept = [local(path) for path in settings["claudeMdExcludes"]]
            settings["claudeMdExcludes"] = list(dict.fromkeys(path for path in kept if path.startswith("<root>")))
            return json.dumps(settings, sort_keys=True)
        return local(item) if isinstance(item, str) else item

    return walk(value)


def _attempt(build: Any) -> Any:
    try:
        return build()
    except RuntimeUnsupported as exc:
        return f"unsupported: {exc}"
    except Exception as exc:  # what a runtime raises for a launch it cannot lay out is part of what it does
        return f"{type(exc).__name__}: {exc}"


def _command_lines(root: Path) -> dict[str, Any]:
    """Every runtime's view of every role's launch: its flags, its commands and the properties the runtimes read."""
    prompt = root / "prompts" / "system.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("SYSTEM", encoding="utf-8")
    cwd = root / "work"
    cwd.mkdir(exist_ok=True)
    seen: dict[str, Any] = {}
    for runtime in BASELINE_RUNTIMES:
        for name, launch in _launches(runtime).items():
            run = launch.runtime
            seen[f"{runtime}/{name}"] = _normalized({
                "properties": {"read_only": launch.read_only, "no_tools": launch.no_tools,
                               "agent_teams": launch.agent_teams, "consult": launch.consult,
                               "system_prompt_mode": launch.system_prompt_mode.value},
                "args": _attempt(lambda: run.launch_args(launch, cwd=cwd) if runtime == "claude-code"
                                  else run.launch_args(launch)),
                "run": _attempt(lambda: run.run_command("bin", cwd=cwd, prompt_file=prompt, launch=launch,
                                                        max_turns=3)),
                "resident": _attempt(lambda: run.resident_command("bin", cwd=cwd, prompt_file=prompt, launch=launch)),
                "consult": _attempt(lambda: run.consult_command("bin", cwd=cwd, prompt_file=prompt, launch=launch,
                                                                session_id="s-1", session_dir=None,
                                                                fork_dir=root / "fork", max_turns=20)),
            }, root)
    return seen


@pytest.fixture
def fixed_root() -> Any:
    """A directory with no symbolic link in its path (macOS's ``/tmp`` is one, which would name each path twice),
    whose parents a command names only in the parts the digest drops."""
    root = Path(tempfile.mkdtemp(prefix="looper-access-", dir="/tmp")).resolve()
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_every_role_and_runtime_keeps_its_command_line(fixed_root: Path) -> None:
    lines = _command_lines(fixed_root)
    assert len(lines) == len(BASELINE_RUNTIMES) * (len(ROLES) + 2)
    digest = hashlib.sha256(json.dumps(lines, sort_keys=True).encode()).hexdigest()
    assert digest == COMMAND_LINES_SHA256, json.dumps(lines, sort_keys=True, indent=1)[:4000]


@pytest.mark.parametrize(("role", "access", "level"), [
    (None, None, Access.FULL),
    (AgentType.MANAGER, None, Access.FULL),
    (AgentType.WORKER, None, Access.FULL),
    (AgentType.REVIEWER, None, Access.FULL),
    (AgentType.PROCESSOR, None, Access.FULL),
    (AgentType.PLANNER, None, Access.READ_ONLY),
    (AgentType.WORKER, Access.CONSULT, Access.CONSULT),
    (AgentType.REVIEWER, Access.CONSULT, Access.CONSULT),
    (None, Access.ADVISOR, Access.ADVISOR),
    (AgentType.WORKER, Access.READ_ONLY, Access.READ_ONLY),
    (AgentType.MANAGER, Access.PLATFORM_TOOLS, Access.PLATFORM_TOOLS),
    (AgentType.EXPLORER, None, Access.EXPLORE),
    (AgentType.WORKER, Access.EXPLORE, Access.EXPLORE),
])
def test_a_launch_has_its_roles_access_level_unless_it_names_one(role: AgentType | None, access: Access | None,
                                                                 level: Access) -> None:
    launch = Launch(AgentSettings(profile=builtin_resolved_profile()), role=role, access=access)
    assert launch.access_level is level
    assert launch.read_only is (level not in (Access.FULL, Access.PLATFORM_TOOLS))
    assert launch.platform_tools_only is (level is Access.PLATFORM_TOOLS)
    assert launch.consult is (level is Access.CONSULT)
    assert launch.no_tools is (level is Access.ADVISOR or role is AgentType.PROCESSOR)


def test_an_advisor_launch_has_no_tool_and_replaces_its_system_prompt(fixed_root: Path) -> None:
    from looper.agents.claude_cli import NO_TOOLS_ARGS
    from looper.agents.system_prompt_mode import SystemPromptMode

    launch = Launch(AgentSettings(profile=replace(builtin_resolved_profile(), agent_teams=True)),
                    access=Access.ADVISOR)
    assert launch.system_prompt_mode is SystemPromptMode.REPLACE and not launch.agent_teams
    prompt = fixed_root / "system.md"
    prompt.write_text("ADVISOR CONTRACT", encoding="utf-8")
    argv = launch.runtime.run_command("claude", cwd=fixed_root, prompt_file=prompt, launch=launch, max_turns=2)
    start = argv.index(NO_TOOLS_ARGS[0])
    assert tuple(argv[start:start + len(NO_TOOLS_ARGS)]) == NO_TOOLS_ARGS
    assert argv[argv.index("--system-prompt-file") + 1] == str(prompt)
    assert argv[argv.index("--setting-sources") + 1] == "" and argv[argv.index("--max-turns") + 1] == "2"
    for flag in ("--dangerously-skip-permissions", "--replay-user-messages", "--append-system-prompt-file",
                 "--teammate-mode", "--plugin-dir", "--add-dir"):
        assert flag not in argv


def test_a_runtime_without_verified_platform_only_tools_refuses_the_launch() -> None:
    launch = Launch(AgentSettings(profile=_profile("codex")), role=AgentType.MANAGER)
    assert launch.environment()  # the regular operator path remains available
    with pytest.raises(RuntimeUnsupported, match="platform tools"):
        replace(launch, access=Access.PLATFORM_TOOLS).environment()


@pytest.mark.parametrize("access", [Access.PLATFORM_TOOLS, Access.EXPLORE])
def test_restricted_launches_stay_sealed_and_keep_the_tool_hook(access: Access, fixed_root: Path) -> None:
    from looper.agents.outbound_hook import ToolHook

    hook = ToolHook("synthetic-hook-command")
    launch = Launch(AgentSettings(profile=builtin_resolved_profile()), access=access, tool_hook=hook)
    argv = launch.runtime.isolation_args(fixed_root, launch=launch)
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert settings["hooks"] == hook.settings()
    assert launch.read_only is (access is Access.EXPLORE)
