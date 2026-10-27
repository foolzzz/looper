"""Portable current launch capture; no production imports.

``capture(root, exercise, inspect_launch=..., readback=...)`` runs an async
driver through its real tiers/adapters. Reuse ``agent_launch_parity.capture``
or the runtime tests' existing fake CLIs; only executables below ``root`` are
observed (also behind an ``env NAME=value ...`` prefix). Other subprocesses
are left alone. The driver owns disposable persistence and synthetic auth.

``inspect_launch(argv, env, cwd)`` reads config bytes, prompt, permissions,
isolation, MCP and login-link targets *at launch*. ``readback(index)`` reads
session, totals, price, cap and persisted prompt-layer sources *after the
exercise*. Both must return all
their fields, using explicit None for inapplicable facts. No runtime-specific
defaults or expected values are invented here. The returned list is JSON
data, suitable for strict comparison of current launch behavior.
Drivers must seed deterministic IDs and control their inherited environment.

Only the supplied temporary root, explicitly requested session timestamps,
internal catalog price number spelling can be normalized; and the system prompt's
text, which the capture does not compare (``without_prompt_text``): the flag
and the directory that carry it, and whether there is one, stay. Layer sources
remain significant; only their content hashes and project row timestamps vary.
Config text,
URL spelling, model/provider names, ordering, environment removals, model
settings, price values and caps remain significant.
Existing main fixtures are never read or refreshed by this module.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import math
import os
import re
import tempfile
import tomllib
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

LAUNCH_FIELDS = frozenset({"unset", "config", "prompt", "permissions", "isolation", "mcp", "login_target"})
STATE_FIELDS = frozenset({"session", "totals", "price", "cap", "prompt_layers"})
FIELDS = frozenset({"argv", "env", "unset", "first_input"}) | LAUNCH_FIELDS | STATE_FIELDS
TIME_PATHS = frozenset({("session", "created_at"), ("session", "updated_at")})
CATALOG_PRICE_FIELDS = ("input_usd_per_mtok", "output_usd_per_mtok",
                        "cache_read_usd_per_mtok", "cache_write_usd_per_mtok")

CODEX_CASES = {
    "key-default": ("api_key", "", "", "append", True),
    "key-proxy": ("api_key", "https://proxy.example.test/responses/v1/", "high", "replace", True),
    "login-home": ("oauth", "", "low", "append", False),
    "login-codex-home": ("oauth", "", "high", "replace", True),
}
CODEX_CATALOG = {"name": "capture-codex", "context_window": 128000,
                 "input_usd_per_mtok": 0.6, "output_usd_per_mtok": 2.2,
                 "cache_read_usd_per_mtok": 0.1, "cache_write_usd_per_mtok": 1,
                 "efforts": ["high", "low"]}
CODEX_TOTALS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd")
OMP_CATALOG = {**CODEX_CATALOG, "name": "capture-omp[1m]", "context_window": 1_000_000}
OMP_CASES = {
    "responses-default": ("openai-responses", "", OMP_CATALOG, "high", "replace"),
    "chat-proxy": ("openai-chat", "https://proxy.example.test/chat/v1/",
                   {**OMP_CATALOG, "efforts": []}, "", "append"),
    "messages-open": ("anthropic-messages", "", {}, "", "replace"),
    "responses-explicit": ("openai-responses",
                     "https://proxy.example.test/responses/", {**OMP_CATALOG, "efforts": None},
                     "low", "append"),
    "chat-explicit": ("openai-chat", "", OMP_CATALOG, "high", "replace"),
    "responses-open": ("openai-responses", "https://proxy.example.test/exact/v1/", {}, "low", "append"),
}


@contextmanager
def omp_capture_root() -> Any:
    """Use a private root; prompt-derived content addresses are normalized separately."""
    with tempfile.TemporaryDirectory(prefix="looper-omp-capture-", dir="/tmp") as directory:
        yield Path(directory).resolve()


def omp_environment(root: Path) -> dict[str, str]:
    """Synthetic inherited settings, including directories the adapter must unset."""
    return {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
            "PI_CONFIG_DIR": str(root / "inherited-config"),
            "XDG_CONFIG_HOME": str(root / "xdg"),
            "CLAUDE_CONFIG_DIR": str(root / "synthetic-login"),
            "LOOPER_PROVIDER_API_KEY": "sk-test-inherited"}


def omp_executable(root: Path) -> str:
    from fake_claude import fake_claude

    (root / "bin").mkdir(exist_ok=True)
    return fake_claude(root / "bin/omp", '''import json, os, sys
args = sys.argv[1:]
assert args[:3] == ['-p', '--mode', 'json'], args
message = sys.stdin.read()
assert message
session = 'sess-capture-omp'
if '--session-dir' in args:
    directory = args[args.index('--session-dir') + 1]
    with open(os.path.join(directory, '2026-10-14T00-00-00-000Z_' + session + '.jsonl'), 'w') as stream:
        stream.write('{}')
reply = json.dumps({'status':'completed', 'summary':'ok', 'passed':True, 'findings':[]})
for event in [
    {'type':'session', 'version':3, 'id':session, 'cwd':os.getcwd()},
    {'type':'agent_start'},
    {'type':'message_end', 'message':{'role':'assistant', 'stopReason':'stop', 'model':'capture-omp[1m]',
      'usage':{'input':60, 'output':10, 'cacheRead':40, 'cacheWrite':0},
      'content':[{'type':'text', 'text':reply}]}},
    {'type':'agent_end', 'messages':[], 'isTerminal':True},
]:
    print(json.dumps(event), flush=True)
''')


@contextmanager
def codex_capture_root() -> Any:
    """Use a private root; prompt-derived content addresses are normalized separately."""
    with tempfile.TemporaryDirectory(prefix="looper-codex-capture-", dir="/tmp") as directory:
        yield Path(directory).resolve()


def codex_environment(root: Path, *, explicit_home: bool) -> dict[str, str]:
    """An entirely synthetic login, never the operator's home or credentials."""
    login_home = root / ("login" if explicit_home else "home/.codex")
    login_home.mkdir(parents=True)
    (login_home / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":"synthetic"}\n')
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
                   "CODEX_API_KEY": "sk-test-inherited", "CODEX_ACCESS_TOKEN": "synthetic-token"}
    if explicit_home:
        environment["CODEX_HOME"] = str(login_home)
    return environment


def codex_executable(root: Path) -> str:
    from fake_claude import fake_claude

    (root / "bin").mkdir(exist_ok=True)
    # The runtime's existing stand-in protocol: one process per turn, cumulative
    # usage kept in the isolated home. Nothing invokes a paid model or network.
    return fake_claude(root / "bin/codex", '''import json, os, sys
home = os.environ['CODEX_HOME']
args = sys.argv[1:]
assert args[0] == 'exec' and args[-1] == '-', args
message = sys.stdin.read()
state = os.path.join(home, 'capture-thread.json')
turns = json.load(open(state)) if 'resume' in args else 0
if 'resume' in args:
    assert args[args.index('resume') + 1] == 'thread-capture'
turns += 1
json.dump(turns, open(state, 'w'))
reply = json.dumps({'status':'completed', 'summary':'ok', 'passed':True, 'findings':[]})
for event in [
    {'type':'thread.started', 'thread_id':'thread-capture'},
    {'type':'turn.started'},
    {'type':'item.completed', 'item':{'id':'item_0', 'type':'agent_message', 'text':reply}},
    {'type':'turn.completed', 'usage':{'input_tokens':100*turns, 'cached_input_tokens':40*turns,
      'cache_write_input_tokens':0, 'output_tokens':10*turns, 'reasoning_output_tokens':0}},
]:
    print(json.dumps(event), flush=True)
''')


GROK_CATALOG = {**CODEX_CATALOG, "name": "grok-4.7", "efforts": ["low", "xhigh"]}
# ACP refusal is supplied by the stand-in executable, never by changing the
# runtime's transport, configuration, permissions or budget policy.
GROK_CASES = {
    "acp-known": (False, 0.01, "low", "replace", True),
    "acp-unknown": (False, 0, "", "append", False),
    "fallback-known": (True, 0.02, "xhigh", "replace", False),
    "fallback-unknown": (True, 0, "low", "append", True),
}


@contextmanager
def grok_capture_root() -> Any:
    """Use a private root; prompt-derived content addresses are normalized separately."""
    with tempfile.TemporaryDirectory(prefix="looper-grok-capture-", dir="/tmp") as directory:
        yield Path(directory).resolve()


def grok_environment(root: Path, *, explicit_home: bool) -> dict[str, str]:
    login_home = root / ("login" if explicit_home else "home/.grok")
    login_home.mkdir(parents=True)
    (login_home / "auth.json").write_text('{"token":"synthetic-login-secret"}\n')
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
                   "XAI_API_KEY": "sk-test-inherited", "GROK_CODE_XAI_API_KEY": "sk-test-inherited",
                   "GROK_AUTH": "synthetic-token", "GROK_CONFIG": str(root / "inherited.toml"),
                   "XDG_CONFIG_HOME": str(root / "xdg"),
                   "CLAUDE_CONFIG_DIR": str(root / "synthetic-claude")}
    if explicit_home:
        environment["GROK_HOME"] = str(login_home)
    return environment


def grok_executable(root: Path, *, cost: float, fallback: bool) -> str:
    """Use the checkout's existing ACP/headless protocol stand-ins without models."""
    import ast
    import agent_launch_parity as tiers
    from fake_claude import fake_claude
    from fake_grok_acp import FAKE_ACP

    # Read the standalone protocol body without importing a test module (whose
    # module-level profiles belong to the checkout's own schema).
    module = ast.parse(Path(tiers.__file__).with_name("test_grok_runtime.py").read_text())
    headless = next(ast.literal_eval(node.value) for node in module.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(name, ast.Name) and name.id == "_FAKE_GROK" for name in node.targets))
    reply = json.dumps({"status": "completed", "summary": "ok", "passed": True, "findings": []})
    # Replace mode leaves rules/looper.md empty (the prompt is the agent file).
    # The stand-in still has to finish; it must not require the harness separator.
    prompt_line = ("prompt = open(os.path.join(home, 'rules', 'looper.md')).read()"
                   ".partition('\\n\\n---\\n\\n')[2].splitlines()[0]")
    safe_prompt = ("prompt = (open(os.path.join(home, 'rules', 'looper.md')).read()"
                   ".partition('\\n\\n---\\n\\n')[2].splitlines() or [''])[0]")
    text_line = "text = f'{prompt} | {message} | turn {turns}'"
    cost_line = "'total_cost_usd': 0,"
    answer_line = 'answer = f\'{rules_line()} | {text} | turn {state["turns"]}\''
    usage_line = "usage(1, 100, 40, 5, 100000000)"
    for source, needle in ((headless, prompt_line), (headless, text_line), (headless, cost_line),
                           (FAKE_ACP, answer_line), (FAKE_ACP, usage_line)):
        if needle not in source:
            raise RuntimeError("Grok stand-in protocol no longer matches the capture contract")
    headless = headless.replace(prompt_line, safe_prompt).replace(text_line, f"text = {reply!r}")
    headless = headless.replace(cost_line, f"'total_cost_usd': {cost!r},")
    acp = FAKE_ACP.replace(answer_line, f"answer = {reply!r}")
    acp = acp.replace(usage_line, f"usage(1, 100, 40, 5, {int(round(cost * 10**10))})")
    prefix = "import os, sys\n" + ("os.environ['FAKE_ACP_DEAD'] = '1'\n" if fallback else "")
    body = prefix + f"src = {acp!r} if 'agent' in sys.argv[1:] else {headless!r}\nexec(compile(src, 'fake', 'exec'))\n"
    (root / "bin").mkdir()
    return fake_claude(root / "bin/grok", body)


# Public, synthetic inputs shared by the source capture and its coverage tests.
CLAUDE_CASES = {
    "oauth-default": ("oauth", "", None, True, None, "role"),
    "official-key-append": ("api_key", "https://api.anthropic.com/", None, False, "append", "role"),
    "empty-url-key-replace": ("api_key", "", None, True, "replace", "role"),
    "proxy-key-background-append": ("api_key", "https://proxy.example.test/messages/", "side-model",
                                     True, "append", "role"),
    "proxy-key-task-replace": ("api_key", "https://proxy.example.test/messages/", None,
                               False, "replace", "task"),
}


def with_planner_file_reads(argv: list[str]) -> list[str]:
    """Apply the declared Planner read-tool and worktree restrictions; keep historical captures unchanged."""
    argv = list(argv)
    if "--permission-mode" in argv and "--disallowedTools" in argv and "--allowedTools" in argv:
        denied, allowed = argv.index("--disallowedTools") + 1, argv.index("--allowedTools") + 1
        if argv[denied] == "Edit,Write,NotebookEdit":
            assert argv[allowed].startswith("Skill,WebFetch,WebSearch"), argv
            argv[denied] = "Bash," + argv[denied] + ",EnterWorktree"
            argv[allowed] = "Read,Glob,Grep," + argv[allowed]
    return argv


async def seed_current(pool: Any, runtime: Any, provider: Any, endpoint: Any, model: Any, profile: Any) -> None:
    """Persist explicit current declarations in an initialized disposable schema."""
    from looper.agents.profile_registry import MODEL_FIELDS

    await pool.execute(
        "INSERT INTO agent_runtimes (id, name, type, executable, capabilities) VALUES ($1,$2,$3,$4,$5) "
        "ON CONFLICT (id) DO UPDATE SET executable = EXCLUDED.executable, capabilities = EXCLUDED.capabilities",
        runtime.id, runtime.name, runtime.type, runtime.executable,
        sorted(capability.value for capability in runtime.capabilities))
    await pool.execute(
        "INSERT INTO llm_providers (id, name, vendor_type, auth, oauth_runtime_type, env, max_concurrency) "
        "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7)", provider.id, provider.name, provider.vendor_type.value,
        provider.auth.value, provider.oauth_runtime_type,
        json.dumps([{"key": key, "value": value} for key, value in provider.env]), provider.max_concurrency)
    if provider.api_key is not None:
        await pool.execute("INSERT INTO llm_provider_secrets (provider_id, api_key) VALUES ($1,$2)",
                           provider.id, provider.api_key)
    await pool.execute(
        "INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url, use_runtime_default) "
        "VALUES ($1,$2,$3,$4)", endpoint.provider_id, endpoint.protocol.value, endpoint.base_url,
        endpoint.use_runtime_default)
    columns = ("id", "provider_id", "protocol", *MODEL_FIELDS, "catalog_order", "builtin")
    values = [getattr(model, name) for name in columns]
    await pool.execute(f"INSERT INTO llm_models ({', '.join(columns)}) "
                       f"VALUES ({', '.join(f'${index}' for index in range(1, len(columns) + 1))})", *values)
    await pool.execute(
        "INSERT INTO agent_profiles (id, name, runtime_id, model_id, effort, agent_teams, custom_prompt) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7)", profile.id, profile.name, profile.runtime.id, profile.model_id,
        profile.effort, profile.agent_teams, profile.custom_prompt)


async def capture_tiers(
    pool: Any, runtime: str, case: str, temporary_root: Path, *, rename_profile_after: int | None = None,
) -> list[dict[str, Any]]:
    """Exercise current registry rows through real tier classes and SQL readback."""
    import agent_launch_parity as tiers
    from looper.agents import claude_cli
    from looper.agents.profile_registry import AgentProfile, Endpoint, ModelSpec, ProviderSpec, RuntimeSpec
    from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType
    from looper.agents.runtimes import adapter
    from looper.db import agent_sessions
    from itertools import count

    mode, scope, teams = None, "role", False
    if runtime == "claude":
        auth, url, background, teams, mode, scope = CLAUDE_CASES[case]
        runtime_type, name, effort = "claude-code", "main-model[1m]", "high"
        protocol = "anthropic-messages"
        metadata = {"name": name, "context_window": 1_000_000, "input_usd_per_mtok": 0.6,
                    "output_usd_per_mtok": 2.2, "cache_read_usd_per_mtok": 0,
                    "cache_write_usd_per_mtok": 1, "efforts": ["high", "low"], "background_model": background}
        account_env = [{"key": "API_TIMEOUT_MS", "value": "3000000"}]
        budget = 0.5
        root_context = tempfile.TemporaryDirectory(prefix="looper-target-claude-", dir="/tmp")
    else:
        runtime_type = runtime
        account_env = [{"key": "CAPTURE_SETTING", "value": "provider"}]
        protocol = "openai-responses"
        if runtime == "codex":
            auth, url, effort, mode, explicit_home = CODEX_CASES[case]
            name, metadata, budget = "capture-codex", CODEX_CATALOG, 0.5
            root_context = codex_capture_root()
        elif runtime == "omp":
            protocol, url, metadata, effort, mode = OMP_CASES[case]
            auth, name, budget = "api_key", "capture-omp[1m]", 0.5
            root_context = omp_capture_root()
        elif runtime == "grok":
            fallback, cost, effort, mode, explicit_home = GROK_CASES[case]
            auth, url, name, metadata, budget = "oauth", "", "grok-4.7", GROK_CATALOG, 0.005
            root_context = grok_capture_root()
        else:
            raise ValueError(f"unsupported capture runtime: {runtime}")

    with root_context as directory:
        root = Path(directory).resolve() if runtime == "claude" else Path(directory)
        if runtime == "claude":
            executable = ""
            environment = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
                           "CLAUDE_CONFIG_DIR": str(root / "login"),
                           "ANTHROPIC_API_KEY": "sk-test-inherited-key",
                           "ANTHROPIC_AUTH_TOKEN": "sk-test-inherited-token",
                           "ANTHROPIC_BASE_URL": "https://inherited.example.test/",
                           "ANTHROPIC_DEFAULT_HAIKU_MODEL": "inherited-side",
                           "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}
            login = root / "login/.credentials.json"
            login.parent.mkdir()
            login.write_text('{"synthetic":true}\n')
        elif runtime == "codex":
            executable = codex_executable(root)
            environment = codex_environment(root, explicit_home=explicit_home)
        elif runtime == "omp":
            executable, environment = omp_executable(root), omp_environment(root)
        else:
            executable = grok_executable(root, cost=cost, fallback=fallback)
            environment = grok_environment(root, explicit_home=explicit_home)
        spec = RuntimeSpec(f"rt-{runtime_type}", runtime_type, runtime_type, executable,
                           adapter(runtime_type).capabilities)
        vendor = ({"claude": VendorType.CLAUDE_SUBSCRIPTION, "codex": VendorType.CODEX_LOGIN,
                   "grok": VendorType.GROK_LOGIN}[runtime] if auth == "oauth" else VendorType.CUSTOM)
        provider = ProviderSpec("prov-capture", "capture", vendor, AuthKind(auth), vendor.oauth_runtime_type,
                                env=tuple((entry["key"], entry["value"]) for entry in account_env),
                                api_key="sk-test-capture" if auth == "api_key" else None)
        protocol = ApiProtocol(protocol)
        endpoint = Endpoint(provider.id, protocol, url, use_runtime_default=not url and auth == "api_key")
        model = ModelSpec("model-capture", provider.id, protocol=protocol,
                          **(metadata or {"name": name}))
        profile = AgentProfile("profile-capture", "capture", spec, model.id, effort or None, teams)
        await seed_current(pool, spec, provider, endpoint, model, profile)
        await tiers.seed_projects(pool)
        await pool.execute(
            "INSERT INTO agent_setting_layers (scope, profile_id, run_budget_usd, env) "
            "VALUES ('system', 'profile-capture', $1, $2::jsonb)", budget,
            json.dumps([] if runtime == "claude" else [{"key": "CAPTURE_SETTING", "value": "settings"}]))
        states: list[dict[str, Any]] = []

        async def state(index: int) -> dict[str, Any]:
            rows = await pool.fetch("SELECT session_id, input_tokens, output_tokens, cache_read_tokens, "
                                    "cache_creation_tokens, cost_usd, status, metadata FROM llm_records ORDER BY id")
            if len(rows) != index + 1 or rows[-1]["status"] != "completed":
                raise AssertionError(f"{runtime}/{case}: tier {index} did not persist a completed run")
            resident = index < (4 if runtime in {"codex", "grok"} else 2)
            owner = "manager" if index < (2 if runtime in {"codex", "grok"} else 1) else (
                f"planner-{tiers.TASKS[tiers.PROJECT_A]}")
            session = await agent_sessions.get_session(owner) if resident else {"session_id": rows[-1]["session_id"]}
            result = {"session": session, "totals": {key: rows[-1][key] for key in CODEX_TOTALS},
                            "prompt_layers": _prompt_layer_sources(json.loads(rows[-1]["metadata"])["prompt_layers"])}
            if index == rename_profile_after:
                await pool.execute("UPDATE agent_profiles SET name = 'renamed' WHERE id = 'profile-capture'")
            return result

        original_capture = tiers.capture

        async def claude_exercise(*args: Any) -> Any:
            result = await original_capture(*args)
            # Claude's existing driver completes all roles before readback.
            rows = await pool.fetch("SELECT session_id, input_tokens, output_tokens, cache_read_tokens, "
                                    "cache_creation_tokens, cost_usd, status, metadata FROM llm_records ORDER BY id")
            if len(rows) != 4 or any(row["status"] != "completed" for row in rows):
                raise AssertionError("Claude tiers did not persist four completed runs")
            for index in range(4):
                owner = "manager" if index == 0 else f"planner-{tiers.TASKS[tiers.PROJECT_A]}"
                saved = await agent_sessions.get_session(owner) if index < 2 else {"session_id": rows[index]["session_id"]}
                states.append({"session": saved, "totals": {key: rows[index][key] for key in CODEX_TOTALS},
                               "prompt_layers": _prompt_layer_sources(json.loads(rows[index]["metadata"])["prompt_layers"])})
            return result

        ids = count(1)
        cli = tiers._CLAUDE.replace(
            "'total_cost_usd': 0,", "'total_cost_usd': 0.25, 'modelUsage': {{'main-model[1m]': "
            "{{'inputTokens': 100, 'outputTokens': 40, 'cacheReadInputTokens': 5, 'cacheCreationInputTokens': 2}}}},")
        with patch.dict(os.environ, environment, clear=True):
            await tiers.seed_rest("unconfigured", root)
            if mode is not None:
                for role in ("manager", "planner", "worker", "reviewer"):
                    if scope == "task" and role != "manager":
                        await pool.execute("INSERT INTO agent_setting_layers (scope, role, system_prompt_mode) "
                                           "VALUES ('role', $1, 'append')", role)
                        await pool.execute("INSERT INTO task_system_prompt_modes VALUES ($1,$2,$3)",
                                           tiers.TASKS[tiers.PROJECT_A], role, mode)
                    else:
                        await pool.execute("INSERT INTO agent_setting_layers (scope, role, system_prompt_mode) "
                                           "VALUES ('role',$1,$2)", role, mode)
            if runtime == "claude":
                with patch.object(tiers, "_CLAUDE", cli), patch.object(tiers, "capture", claude_exercise), \
                        patch.object(claude_cli, "uuid4", lambda: uuid.UUID(int=next(ids))):
                    records = await capture_claude_tiers(root, "unconfigured", read_state=states.__getitem__)
            else:
                driver = {"codex": capture_codex_tiers, "omp": capture_omp_tiers, "grok": capture_grok_tiers}[runtime]
                records = await driver(root, read_state=state)
        return [normalize(record, root=root) for record in records]


def above_root_normalized(settings: str, root: str) -> str:
    """The Claude ``--settings`` text with ``root`` as ``<root>`` and every exclusion for a directory above it as
    ``<above>/<name>`` (each once; entries under the root keep their count). The directories above a temporary root depend on the host (``/private/tmp`` on
    macOS, ``/tmp`` on Linux CI), so captures cannot name them."""
    parsed = json.loads(settings)
    excludes: list[str] = []
    for pattern in parsed.get("claudeMdExcludes", []):
        if not pattern.startswith((root + "/", "<root>/")):
            name = next(n for n in (".claude/rules/**", ".claude/CLAUDE.md", ".claude/AGENTS.md", "CLAUDE.md", "AGENTS.md")
                        if pattern.endswith("/" + n))
            pattern = f"<above>/{name}"
            if pattern in excludes:
                continue
        excludes.append(pattern)
    parsed["claudeMdExcludes"] = excludes
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).replace(root, "<root>")


def _fields(value: Mapping[str, Any], expected: frozenset[str]) -> None:
    if not isinstance(value, dict) or value.keys() != expected:
        actual = set(value) if isinstance(value, dict) else set()
        raise ValueError(f"capture fields: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")


def _public_data(value: Any) -> Any:
    """Turn adapter tuples into the lists their stored JSON uses.

    Catalog efforts stay a tuple on the provider object. The capture contract
    accepts only JSON types, so convert them explicitly at the adapter boundary.
    """
    if isinstance(value, tuple):
        return [_public_data(item) for item in value]
    if isinstance(value, list):
        return [_public_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _public_data(item) for key, item in value.items()}
    return value


def _above_root_everywhere(value: Any) -> Any:
    """Apply ``above_root_normalized`` to every Claude ``--settings`` text (argv, config) and the parsed isolation
    dict in a root-normalized record. Only the exclusions for directories above the root change."""
    if isinstance(value, str):
        if '"claudeMdExcludes"' in value:
            try:
                parsed = json.loads(value)
                # Only text that is exactly its own compact serialization: any other byte (a stray newline) must
                # still reach the comparison unchanged.
                if isinstance(parsed, dict) and \
                        json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) == value:
                    return above_root_normalized(value, "<root>")
            except ValueError:
                pass
        return value
    if isinstance(value, list):
        return [_above_root_everywhere(item) for item in value]
    if isinstance(value, dict):
        result = {key: _above_root_everywhere(item) for key, item in value.items()}
        if isinstance(result.get("claudeMdExcludes"), list):
            result = json.loads(above_root_normalized(json.dumps(result), "<root>"))
        return result
    return value


def _json_copy(value: Any) -> Any:
    """Reject implicit coercion (tuples, Paths, callables, non-string keys, NaN)."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_json_copy(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _json_copy(item) for key, item in value.items()}
    raise ValueError(f"capture requires JSON data, got {type(value).__name__}")


# Only paths derived from prompt-bearing files may vary with the wording.
# Keys, endpoints, models and arbitrary hashes in other configuration stay exact.
_PROMPT_ADDRESS = re.compile(
    r"(<root>/runtime/\.local/(?:prompts/|codex/(?:instructions/|runs/)|grok/runs/|omp/homes/))"
    r"[0-9a-f]{64}(?=[/.\"\\\s]|$)"
)
_OMP_PROMPT_ADDRESS = re.compile(
    r"(<root>/runtime/\.local/omp/homes/<sha256>/prompts/)[0-9a-f]{64}(?=\.md)"
)
_PROMPT = "<prompt>"


def _prompt_addresses(text: str) -> str:
    text = _PROMPT_ADDRESS.sub(r"\1<sha256>", text)
    return _OMP_PROMPT_ADDRESS.sub(r"\1<sha256>", text)


def without_prompt_text(record: dict[str, Any]) -> dict[str, Any]:
    """Discard wording and its content addresses, preserving flags, locations and other inputs."""
    record["prompt"] = _PROMPT if record["prompt"] else ""
    argv = record["argv"]
    prompt_flags = {"--system-prompt-file", "--append-system-prompt-file", "--system-prompt",
                    "--append-system-prompt", "--agent", "--agent-profile", "--config"}
    record["argv"] = [
        _prompt_addresses(arg) if (index and argv[index - 1] in prompt_flags)
        or arg.startswith(("HOME=", "CODEX_HOME=", "GROK_HOME=")) else arg
        for index, arg in enumerate(argv)
    ]
    for field in ("env", "isolation"):
        for key in ("HOME", "CODEX_HOME", "GROK_HOME"):
            if isinstance(record.get(field), dict) and isinstance(record[field].get(key), str):
                record[field][key] = _prompt_addresses(record[field][key])
    config = record.get("config")
    if isinstance(config, dict):
        if isinstance(config.get("toml"), str):
            config["toml"] = re.sub(
                r'(?m)^(model_instructions_file = ")([^"\n]+)(")$',
                lambda match: match[1] + _prompt_addresses(match[2]) + match[3], config["toml"])
        for key in ("agents_md", "agent_md", "acp_agent_md"):
            content = config.get(key)
            if isinstance(content, str) and content:
                # Grok's front matter carries tool permissions, so it remains significant.
                end = content.find("\n---\n\n") if content.startswith("---\n") else -1
                config[key] = (content[:end + 6] if end >= 0 else "") + _PROMPT
    return record


def _prompt_layer_sources(value: Any) -> Any:
    """Keep layer identity, scope and presence without pinning content hashes or row times."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or not value:
        raise ValueError("completed tier must persist prompt_layers")
    return {
        layer: ({key: ("<sha256>" if item else item) if key in {"sha256", "background_sha256", "knowledge_sha256"} else
                 ("<time>" if item else item) if key == "updated_at" else item
                 for key, item in details.items()} if isinstance(details, dict) else details)
        for layer, details in value.items()
    }


def normalize(record: dict[str, Any], *, root: Path,
              time_paths: Iterable[tuple[str, ...]] = ()) -> dict[str, Any]:
    """Normalize root paths, their derived prompt addresses and opted-in times."""
    _fields(record, FIELDS)
    result = _json_copy(record)
    for name in ("argv", "unset"):
        if not isinstance(result[name], list) or any(type(item) is not str for item in result[name]):
            raise ValueError(f"{name} must be a list of strings")
    if not result["argv"]:
        raise ValueError("argv must be non-empty")
    if not isinstance(result["env"], dict) or any(type(v) is not str for v in result["env"].values()):
        raise ValueError("env must map names to strings")
    if len(set(result["unset"])) != len(result["unset"]):
        raise ValueError("unset names must be unique")
    for name in ("prompt", "first_input"):
        if type(result[name]) is not str:
            raise ValueError(f"{name} must be a string")
    paths = tuple(time_paths)
    if any(type(path) is not tuple or path not in TIME_PATHS for path in paths) or len(set(paths)) != len(paths):
        raise ValueError(f"time_paths must be unique entries from {sorted(TIME_PATHS)}")
    for path in paths:
        parent = result
        for part in path[:-1]:
            if not isinstance(parent, dict) or part not in parent:
                raise ValueError(f"missing timestamp at {path}")
            parent = parent[part]
        if not isinstance(parent, dict) or path[-1] not in parent:
            raise ValueError(f"missing timestamp at {path}")
        value = parent[path[-1]]
        try:
            if not isinstance(value, str) or "T" not in value or datetime.fromisoformat(value).tzinfo is None:
                raise ValueError
        except ValueError:
            raise ValueError(f"invalid timestamp at {path}") from None
        parent[path[-1]] = "<time>"
    root_text = str(root)
    if not root.is_absolute() or root_text == "/" or ".." in root.parts:
        raise ValueError("root must be an absolute temporary directory, not /")
    # A root prefix in a longer filename is not a path under this root.
    pattern = re.compile(re.escape(root_text) + r"(?=/|$|[\s\"'\\,;:)\]}])")

    def replace(value: Any) -> Any:
        if isinstance(value, str):
            return pattern.sub("<root>", value)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            replaced = {pattern.sub("<root>", key): replace(item) for key, item in value.items()}
            if len(replaced) != len(value):
                raise ValueError("root normalization would collide dictionary keys")
            return replaced
        return value

    return without_prompt_text(replace(result))


def assert_parity(expected: list[dict[str, Any]], actual: list[dict[str, Any]], *,
                  expected_root: Path, actual_root: Path,
                  time_paths: Iterable[tuple[str, ...]] = ()) -> None:
    """Compare every launch, with a field-level diff. Empty baselines are errors."""
    if not expected or not actual:
        raise ValueError("launch captures must be non-empty")
    if len(expected) != len(actual):
        raise AssertionError(f"launch count: expected {len(expected)}, actual {len(actual)}")
    paths = tuple(time_paths)
    def comparable(record: dict[str, Any], root: Path) -> dict[str, Any]:
        result = _above_root_everywhere(normalize(record, root=root, time_paths=paths))
        # These two catalogs are internal asdict(provider.catalog) snapshots,
        # not runtime files/argv/env. SQL DOUBLE PRECISION reads JSON
        # prices 0/1 as 0.0/1.0. Normalize only their four price fields by value;
        # generated config strings and every other field retain exact bytes.
        for field in ("config", "price"):
            metadata = result[field]
            if isinstance(metadata, dict) and isinstance(metadata.get("catalog"), list):
                for entry in metadata["catalog"]:
                    if isinstance(entry, dict):
                        for name in CATALOG_PRICE_FIELDS:
                            value = entry.get(name)
                            if type(value) is float and value.is_integer():
                                entry[name] = int(value)
        return result

    for index, (before, after) in enumerate(zip(expected, actual)):
        left = json.dumps(comparable(before, expected_root), ensure_ascii=False, sort_keys=True, indent=2)
        right = json.dumps(comparable(after, actual_root), ensure_ascii=False, sort_keys=True, indent=2)
        if left != right:
            diff = "\n".join(difflib.unified_diff(left.splitlines(), right.splitlines(),
                                                fromfile="expected", tofile="current", lineterm=""))
            raise AssertionError(f"launch {index} differs:\n{diff}")


class _Input:
    def __init__(self, stream: Any, record: dict[str, Any]) -> None:
        self._stream, self._record = stream, record
        self._seen = False

    def observe(self, data: bytes) -> None:
        if data and not self._seen:
            self._record["first_input"] = data.decode("utf-8")
            self._seen = True

    def write(self, data: bytes) -> None:
        self.observe(data)
        self._stream.write(data)

    def writelines(self, lines: Iterable[bytes]) -> None:
        self.write(b"".join(lines))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _Process:
    def __init__(self, process: Any, record: dict[str, Any]) -> None:
        self._process = process
        self.stdin = _Input(process.stdin, record) if process.stdin is not None else None

    async def communicate(self, input: bytes | None = None) -> Any:
        if input is not None and self.stdin is not None:
            self.stdin.observe(input)
        return await self._process.communicate(input)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._process, name)


async def capture(
    root: Path, exercise: Callable[[], Awaitable[Any]], *,
    inspect_launch: Callable[[list[str], dict[str, str], str], dict[str, Any]],
    readback: Callable[[int], dict[str, Any]], timeout: float = 15,
) -> list[dict[str, Any]]:
    """Observe actual fake-CLI launches; callbacks never replace the launch itself.

    Run serially: interception is process-wide. ``env`` contains values changed
    or added relative to the parent; ``unset`` contains every removed name
    plus the binding's explicit removals supplied by ``inspect_launch``. A
    runtime can clear an inherited directory and then set its own isolated
    directory under the same name, so these two fields can overlap.
    ``first_input`` is the exact first nonempty stdin write (or communicate
    payload), including the runtime's envelope. No IDs or hashes are scrubbed.
    """
    root = root.absolute()
    records: list[dict[str, Any]] = []
    processes: list[Any] = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        argv = [os.fsdecode(arg) for arg in args]
        at = 1 if argv and argv[0] == "env" else 0
        while at and at < len(argv) and "=" in argv[at]:
            at += 1
        if at >= len(argv) or not Path(argv[at]).is_relative_to(root):
            return await original(*args, **kwargs)
        parent = dict(os.environ)
        env = dict(kwargs.get("env") if kwargs.get("env") is not None else parent)
        for assignment in argv[1:at]:
            key, _, value = assignment.partition("=")
            env[key] = value
        details = inspect_launch(argv, env, os.fsdecode(kwargs.get("cwd") or os.getcwd()))
        _fields(details, LAUNCH_FIELDS)
        details = _json_copy(details)
        unset = details.pop("unset")
        if not isinstance(unset, list) or any(type(key) is not str for key in unset):
            raise ValueError("unset must be a list of strings")
        record = {"argv": argv, "env": {k: v for k, v in env.items() if parent.get(k) != v},
                  "unset": sorted((parent.keys() - env.keys()) | set(unset)), "first_input": "", **details}
        process = await original(*args, **kwargs)
        processes.append(process)
        records.append(record)
        return _Process(process, record)

    try:
        with patch.object(asyncio, "create_subprocess_exec", spawn):
            await asyncio.wait_for(exercise(), timeout=timeout)
    finally:
        for process in processes:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # It exited between the returncode check and kill.
                await process.wait()
    if not records:
        raise ValueError("launch captures must be non-empty")
    for index, record in enumerate(records):
        state = readback(index)
        _fields(state, STATE_FIELDS)
        record.update(_json_copy(state))
        normalize(record, root=root)  # validate without discarding the raw paths
        if not record["first_input"]:
            raise ValueError(f"launch {index} has no first input")
    return records


async def capture_claude_tiers(
    root: Path, scenario: str, *, read_state: Callable[[int], dict[str, Any]], timeout: float = 30,
) -> list[dict[str, Any]]:
    """Capture the existing four-tier driver without replacing adapter behavior.

    ``read_state`` returns ``session``, ``totals`` and ``prompt_layers`` from the driver's
    persistence *after* the exercise. The other fields are read from the
    actual Launch at subprocess creation, including explicit binding removals.
    Imports are lazy so the capture remains independently importable.
    Login bytes are never read: only the path Claude will consult is recorded.
    """
    import agent_launch_parity as tiers
    from looper.agents.runtimes import CLAUDE_CODE

    launches: list[Any] = []
    environment = CLAUDE_CODE.environment

    def observe(launch: Any) -> dict[str, str]:
        result = environment(launch)
        launches.append(launch)
        return result

    def inspect(argv: list[str], env: dict[str, str], cwd: str) -> dict[str, Any]:
        launch = launches[-1]
        provider = launch.profile.provider
        protocols = provider.protocols & CLAUDE_CODE.protocols
        if len(protocols) != 1:
            raise ValueError("Claude capture requires one actual protocol")
        prompt_flag = next(flag for flag in ("--append-system-prompt-file", "--system-prompt-file") if flag in argv)
        prompt = Path(argv[argv.index(prompt_flag) + 1])
        settings = argv[argv.index("--settings") + 1]
        mcp = Path(argv[argv.index("--mcp-config") + 1]) if "--mcp-config" in argv else None
        login = Path(env.get("CLAUDE_CONFIG_DIR", str(Path(env["HOME"]) / ".claude"))) / ".credentials.json"
        config = {"settings": settings, "protocol": next(iter(protocols)).value,
                  "model": launch.model, "effort": launch.effort,
                  "catalog": [asdict(entry) for entry in provider.catalog]}
        # Catalog efforts are tuples in Python but lists in their stored JSON.
        config = _json_copy(_public_data(config))
        permissions = {}
        for flag in ("--permission-mode", "--permission-prompts", "--disallowedTools", "--allowedTools"):
            permissions[flag] = argv[argv.index(flag) + 1] if flag in argv else None
        permissions["--dangerously-skip-permissions"] = "--dangerously-skip-permissions" in argv
        return {"unset": list(CLAUDE_CODE.bind_provider(provider, launch.model, launch.effort).unset),
                "config": config, "prompt": prompt.read_text("utf-8"), "permissions": permissions,
                "isolation": json.loads(settings), "mcp": mcp.read_text("utf-8") if mcp else None,
                "login_target": str(login.resolve()) if login.exists() else None}

    def readback(index: int) -> dict[str, Any]:
        state = read_state(index)
        _fields(state, frozenset({"session", "totals", "prompt_layers"}))
        launch = launches[index]
        return {**state, "price": {"source": launch.cost_source.value,
                                   "catalog": _json_copy(_public_data([asdict(entry) for entry in
                                                                      launch.profile.provider.catalog]))},
                "cap": launch.max_budget_usd}

    with patch.object(CLAUDE_CODE, "environment", observe):
        records = await capture(root, lambda: tiers.capture(root, scenario), inspect_launch=inspect,
                                readback=readback, timeout=timeout)
    roles = [case.partition("@")[0] for case in tiers.SCENARIOS[scenario]["cases"]]
    if len(launches) != len(records) or [launch.role.value for launch in launches] != roles:
        raise ValueError("Claude capture did not observe each scenario tier's real adapter launch")
    return records


async def capture_codex_tiers(
    root: Path, *, read_state: Callable[[int], Awaitable[dict[str, Any]]], timeout: float = 45,
) -> list[dict[str, Any]]:
    """Observe real Manager/Planner restarts and Worker/Reviewer launches.

    Read the SQL (or positive-control memory store) after every completed tier
    call, before a resumed turn overwrites its saved session totals. Configuration
    and login symlinks are read at subprocess creation, never reconstructed.
    """
    import agent_launch_parity as tiers
    from looper.agents.runtimes import adapter

    runtime = adapter("codex")
    environment = runtime.environment
    launches: list[Any] = []
    states: list[dict[str, Any]] = []

    def observe(launch: Any) -> dict[str, str]:
        result = environment(launch)
        launches.append(launch)
        return result

    def inspect(argv: list[str], env: dict[str, str], cwd: str) -> dict[str, Any]:
        launch = launches[-1]
        provider = launch.profile.provider
        protocols = provider.protocols & runtime.protocols
        if len(protocols) != 1:
            raise ValueError("Codex capture requires one actual protocol")
        home = Path(env["CODEX_HOME"])
        text = (home / "config.toml").read_text("utf-8")
        config = tomllib.loads(text)
        agents = (home / "AGENTS.md").read_text("utf-8")
        instructions = config.get("model_instructions_file")
        prompt = Path(instructions).read_text("utf-8") if instructions else agents
        login = home / "auth.json"
        if login.exists() and not login.is_symlink():
            raise ValueError("Codex login must be a symlink, never a copied credential")
        return {"unset": list(runtime.bind_provider(provider, launch.model, launch.effort).unset),
                "config": {"toml": text, "agents_md": agents, "protocol": next(iter(protocols)).value,
                           "model": launch.model, "effort": launch.effort,
                           "catalog": _public_data([asdict(entry) for entry in provider.catalog])},
                "prompt": prompt,
                "permissions": {"read_only": launch.read_only,
                                "bypass": "--dangerously-bypass-approvals-and-sandbox" in argv},
                "isolation": {"CODEX_HOME": str(home), "shell_environment_policy": config["shell_environment_policy"],
                              "features": config["features"], "skills": config["skills"],
                              "project_doc_fallback_filenames": config["project_doc_fallback_filenames"]},
                "mcp": config.get("mcp_servers"),
                "login_target": os.readlink(login) if login.is_symlink() else None}

    async def exercise() -> None:
        from looper.agents.tiers.planner import PlannerAgent, TaskPlan

        executable = str(root / "bin/codex")
        tiers._work_dir(root, tiers.PROJECT_A).mkdir(parents=True, exist_ok=True)
        planner = PlannerAgent(task_id=tiers.TASKS[tiers.PROJECT_A], runtime_root=root / "runtime",
                               claude_bin=executable)
        plan = TaskPlan(plan_id="plan-parity", goal_id="", requirement=tiers.REQUIREMENT,
                        work_dir=str(tiers._work_dir(root, tiers.PROJECT_A)), project_id=tiers.PROJECT_A)
        planner._plan = plan
        await planner._refresh_project(plan)
        for index, role in enumerate(("manager", "manager", "planner", "planner", "worker", "reviewer")):
            if role == "manager":
                await tiers._manager(executable, root)
            elif role == "planner":
                try:
                    await planner._run_turn(planner._plan_message(plan))
                finally:
                    await planner._claude.stop()
            else:
                await tiers._worker(executable, root, tiers.PROJECT_A, verify=role == "reviewer")
            states.append(_json_copy(await read_state(index)))

    def readback(index: int) -> dict[str, Any]:
        _fields(states[index], frozenset({"session", "totals", "prompt_layers"}))
        launch = launches[index]
        return {**states[index], "price": {"source": launch.cost_source.value,
                                          "catalog": _public_data([asdict(entry) for entry in
                                                                   launch.profile.provider.catalog])},
                "cap": launch.max_budget_usd}

    with patch.object(runtime, "environment", observe):
        records = await capture(root, exercise, inspect_launch=inspect, readback=readback, timeout=timeout)
    if ([launch.role.value for launch in launches] !=
            ["manager", "manager", "planner", "planner", "worker", "reviewer"] or len(records) != 6):
        raise ValueError("Codex capture did not observe all four real tiers and resumed launches")
    return records


async def capture_omp_tiers(
    root: Path, *, read_state: Callable[[int], Awaitable[dict[str, Any]]], timeout: float = 45,
) -> list[dict[str, Any]]:
    """Observe generated OMP files and SQL readback through the four real tiers.

    The explicitly selected API is read from models.yml. Neither the capture
    nor its comparator resolves a protocol.
    """
    import agent_launch_parity as tiers
    from looper.agents import claude_cli
    from looper.agents.runtimes import adapter
    from itertools import count

    runtime = adapter("omp")
    environment = runtime.environment
    launches: list[Any] = []
    states: list[dict[str, Any]] = []

    def observe(launch: Any) -> dict[str, str]:
        result = environment(launch)
        launches.append(launch)
        return result

    def inspect(argv: list[str], env: dict[str, str], cwd: str) -> dict[str, Any]:
        launch = launches[-1]
        home = Path(env["HOME"])
        models_text = (home / ".omp/agent/models.yml").read_text("utf-8")
        overlay_text = Path(argv[argv.index("--config") + 1]).read_text("utf-8")
        models = json.loads(models_text)["providers"]["looper"]
        prompt_flag = "--system-prompt" if "--system-prompt" in argv else "--append-system-prompt"
        mcp = home / "plugins/looper/.mcp.json"
        return {"unset": list(runtime.bind_provider(launch.profile.provider, launch.model, launch.effort).unset),
                "config": {"models_yml": models_text, "overlay": overlay_text, "api": models["api"],
                           "model": launch.model, "effort": launch.effort,
                           "catalog": _public_data([asdict(entry) for entry in launch.profile.provider.catalog])},
                "prompt": Path(argv[argv.index(prompt_flag) + 1]).read_text("utf-8"),
                "permissions": {"read_only": launch.read_only,
                                "tools": argv[argv.index("--tools") + 1] if "--tools" in argv else None,
                                "approvalMode": json.loads(overlay_text)["tools"]["approvalMode"]},
                "isolation": {"HOME": str(home), "overlay": json.loads(overlay_text),
                              "natives_target": os.readlink(home / ".omp/natives")},
                "mcp": mcp.read_text("utf-8") if mcp.exists() else None,
                "login_target": None}

    async def exercise() -> None:
        executable = str(root / "bin/omp")
        tiers._work_dir(root, tiers.PROJECT_A).mkdir(parents=True, exist_ok=True)
        for index, role in enumerate(("manager", "planner", "worker", "reviewer")):
            if role == "manager":
                await tiers._manager(executable, root)
            elif role == "planner":
                await tiers._planner(executable, root, tiers.PROJECT_A)
            else:
                await tiers._worker(executable, root, tiers.PROJECT_A, verify=role == "reviewer")
            states.append(_json_copy(await read_state(index)))

    def readback(index: int) -> dict[str, Any]:
        _fields(states[index], frozenset({"session", "totals", "prompt_layers"}))
        launch = launches[index]
        return {**states[index], "price": {"source": launch.cost_source.value,
                                          "catalog": _public_data([asdict(entry) for entry in
                                                                   launch.profile.provider.catalog])},
                "cap": launch.max_budget_usd}

    ids = count(1)
    with patch.object(runtime, "environment", observe), \
            patch.object(claude_cli, "uuid4", lambda: uuid.UUID(int=next(ids))):
        records = await capture(root, exercise, inspect_launch=inspect, readback=readback, timeout=timeout)
    if [launch.role.value for launch in launches] != ["manager", "planner", "worker", "reviewer"] or len(records) != 4:
        raise ValueError("OMP capture did not observe all four real tiers' adapter launches")
    return records


async def capture_grok_tiers(
    root: Path, *, read_state: Callable[[int], Awaitable[dict[str, Any]]], timeout: float = 45,
) -> list[dict[str, Any]]:
    """Capture ACP and actual ACP refusal through the four real tier entry points.

    Keep every subprocess, including failed ACP initialization before a per-turn
    fallback. Both attempts read back the completed tier call's persisted state;
    no state is invented for an ACP process that never started a model turn.
    Capture unchanged config bytes and login targets at process creation, then
    read each turn's state before its owner saves the next turn's totals.
    """
    import agent_launch_parity as tiers
    from looper.agents.runtimes import adapter
    from looper.agents.runtimes.grok_acp import AcpGrok

    runtime = adapter("grok")
    environment = runtime.environment
    launches: list[Any] = []
    states: list[dict[str, Any]] = []
    launch_indices: list[int] = []
    requests: list[list[dict[str, Any]]] = []
    send = AcpGrok._send

    def observe(launch: Any) -> dict[str, str]:
        result = environment(launch)
        launches.append(launch)
        return result

    async def observe_send(session: Any, message: dict[str, Any]) -> None:
        # Record the actual request before the real transport sends it. The
        # generic first_input remains the exact initialize bytes, as contracted.
        requests[-1].append(_json_copy(message))
        await send(session, message)

    def inspect(argv: list[str], env: dict[str, str], cwd: str) -> dict[str, Any]:
        launch = launches[-1]
        provider = launch.profile.provider
        protocols = provider.protocols & runtime.protocols
        if len(protocols) != 1:
            raise ValueError("Grok capture requires one actual protocol")
        launch_indices.append(len(launches) - 1)
        requests.append([])
        home = Path(env["GROK_HOME"])
        text = (home / "config.toml").read_text("utf-8")
        config = tomllib.loads(text)
        login = home / "auth.json"
        if not login.is_symlink():
            raise ValueError("Grok login must be a symlink, never a copied credential")
        rules = (home / "rules/looper.md").read_text("utf-8")
        # Replace mode leaves the rules file empty and puts Looper's prompt in
        # the agent file the process is actually started with.
        if rules.strip():
            prompt = rules
        elif "--agent-profile" in argv:
            prompt = Path(argv[argv.index("--agent-profile") + 1]).read_text("utf-8")
        elif "--agent" in argv:
            prompt = Path(argv[argv.index("--agent") + 1]).read_text("utf-8")
        else:
            prompt = rules
        return {"unset": list(runtime.bind_provider(provider, launch.model, launch.effort).unset),
                "config": {"toml": text, "acp_requests": None, "protocol": next(iter(protocols)).value,
                           "model": launch.model, "effort": launch.effort,
                           "catalog": _public_data([asdict(entry) for entry in provider.catalog]),
                           "agent_md": (home / "agent.md").read_text("utf-8") if (home / "agent.md").exists() else None,
                           "acp_agent_md": (home / "acp-agent.md").read_text("utf-8")
                           if "agent" in argv and (home / "acp-agent.md").exists() else None},
                "prompt": prompt,
                "permissions": {"read_only": launch.read_only,
                                "always_approve": "--always-approve" in argv,
                                "tools": argv[argv.index("--tools") + 1] if "--tools" in argv else None},
                "isolation": {"GROK_HOME": str(home), "HOME": env["HOME"],
                              "config": config,
                              "shared_targets": {name: os.readlink(home / name) for name in ("bundled", "docs")}},
                "mcp": config.get("mcp_servers"), "login_target": os.readlink(login)}

    async def exercise() -> None:
        from looper.agents.tiers.planner import PlannerAgent, TaskPlan

        executable = str(root / "bin/grok")
        tiers._work_dir(root, tiers.PROJECT_A).mkdir(parents=True, exist_ok=True)
        planner = PlannerAgent(task_id=tiers.TASKS[tiers.PROJECT_A], runtime_root=root / "runtime",
                               claude_bin=executable)
        plan = TaskPlan(plan_id="plan-parity", goal_id="", requirement=tiers.REQUIREMENT,
                        work_dir=str(tiers._work_dir(root, tiers.PROJECT_A)), project_id=tiers.PROJECT_A)
        planner._plan = plan
        await planner._refresh_project(plan)
        for index, role in enumerate(("manager", "manager", "planner", "planner", "worker", "reviewer")):
            if role == "manager":
                await tiers._manager(executable, root)
            elif role == "planner":
                try:
                    await planner._run_turn(planner._plan_message(plan))
                finally:
                    await planner._claude.stop()
            else:
                await tiers._worker(executable, root, tiers.PROJECT_A, verify=role == "reviewer")
            states.append(_json_copy(await read_state(index)))

    def readback(index: int) -> dict[str, Any]:
        index = launch_indices[index]
        _fields(states[index], frozenset({"session", "totals", "prompt_layers"}))
        launch = launches[index]
        return {**states[index], "price": {"source": launch.cost_source.value,
                                          "catalog": _public_data([asdict(entry) for entry in
                                                                   launch.profile.provider.catalog])},
                "cap": launch.max_budget_usd}

    from itertools import count
    from looper.agents import claude_cli

    ids = count(1)
    with patch.object(runtime, "environment", observe), patch.object(AcpGrok, "_send", observe_send), \
            patch.object(claude_cli, "uuid4", lambda: uuid.UUID(int=next(ids))):
        records = await capture(root, exercise, inspect_launch=inspect, readback=readback, timeout=timeout)
    if ([launch.role.value for launch in launches] !=
            ["manager", "manager", "planner", "planner", "worker", "reviewer"] or len(launch_indices) != len(records) or
            set(launch_indices) != set(range(6))):
        raise ValueError("Grok capture did not observe all four real tiers and resumed launches")
    for record, sent in zip(records, requests):
        if "agent" in record["argv"]:
            record["config"]["acp_requests"] = sent
    return records
