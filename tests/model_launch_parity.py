"""Portable launch capture for legacy and current checkouts; no production imports.

``capture(root, exercise, inspect_launch=..., readback=...)`` runs an async
driver through its real tiers/adapters. Reuse ``agent_launch_parity.capture``
or the runtime tests' existing fake CLIs; only executables below ``root`` are
observed (also behind an ``env NAME=value ...`` prefix). Other subprocesses
are left alone. The driver owns disposable persistence and synthetic auth.

``inspect_launch(argv, env, cwd)`` reads config bytes, prompt, permissions,
isolation, MCP and login-link targets *at launch*. ``readback(index)`` reads
session, totals, price and cap *after the exercise*. Both must return all
their fields, using explicit None for inapplicable facts. No runtime-specific
defaults or expected values are invented here. The returned list is JSON
data, suitable for freezing on an old checkout and comparing on a new one.
Drivers must seed deterministic IDs and control their inherited environment.

Only the supplied temporary root and explicitly requested session timestamps
can be normalized. Config text, URL spelling, model/provider names, ordering,
explicit nulls, environment removals, prices and caps remain significant.
Existing main fixtures are never read or refreshed by this module.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

LAUNCH_FIELDS = frozenset({"unset", "config", "prompt", "permissions", "isolation", "mcp", "login_target"})
STATE_FIELDS = frozenset({"session", "totals", "price", "cap"})
FIELDS = frozenset({"argv", "env", "unset", "first_input"}) | LAUNCH_FIELDS | STATE_FIELDS
TIME_PATHS = frozenset({("session", "created_at"), ("session", "updated_at")})

# The last old-schema main after both preset patches, before model splitting.
# A new resolver must never manufacture the expected side of this contract.
CLAUDE_LEGACY_REVISION = "252e736725c427d4b19e3a427e9f044077781ef8"

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


class LegacyBaselineUnavailable(RuntimeError):
    """A baseline cannot be generated from the required, patched v2 source."""


def claude_legacy_source(checkout: Path) -> dict[str, str]:
    """Verify a clean old checkout; return public provenance, never local paths.

    Call this in the generator subprocess before importing Looper. Supplying
    the current checkout, a dirty adapter or an unpatched revision is an error,
    not permission to update expected from the implementation under test.
    """
    if not checkout.is_dir() or not (checkout / ".git").exists():
        raise LegacyBaselineUnavailable("patched Claude baseline requires an old checkout")

    def git(*args: str) -> str:
        try:
            result = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True,
                                    text=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LegacyBaselineUnavailable("cannot verify old checkout revision") from exc
        return result.stdout.strip()

    revision = git("rev-parse", "HEAD")
    if revision != CLAUDE_LEGACY_REVISION:
        raise LegacyBaselineUnavailable("Claude baseline requires the pinned patched v2 revision")
    git("diff", "--exit-code", "HEAD", "--", "looper", "tests/agent_launch_parity.py", "tests/fake_claude.py")
    return {"revision": revision, "tree": git("rev-parse", "HEAD^{tree}"),
            "adapter_blob": git("rev-parse", "HEAD:looper/agents/runtimes/claude_code.py"),
            "registry_blob": git("rev-parse", "HEAD:looper/agents/profile_registry.py")}


def generate_claude_baseline(checkout: Path, output: Path) -> None:
    """Run only the pinned source in a fresh interpreter; write after all cases succeed.

    Usage: ``python tests/model_launch_parity.py --checkout OLD --output FIXTURE``
    with LOOPER_TEST_DATABASE_URL set to a disposable PostgreSQL database.
    A missing prerequisite is an error, never a reason to synthesize expected.
    """
    checkout = checkout.resolve()
    source = claude_legacy_source(checkout)
    if not os.environ.get("LOOPER_TEST_DATABASE_URL"):
        raise LegacyBaselineUnavailable("Claude baseline requires a disposable test database")
    interpreter = checkout / ".venv/bin/python"
    if not interpreter.is_file():
        raise LegacyBaselineUnavailable("Claude baseline requires the old checkout's test venv")
    result = subprocess.run(
        [str(interpreter), "-I", str(Path(__file__).resolve()), "--checkout", str(checkout), "--capture"],
        cwd=checkout, capture_output=True, text=True, timeout=180, check=True,
    )
    baseline = json.loads(result.stdout)
    if baseline["source"] != source or baseline["captures"].keys() != CLAUDE_CASES.keys():
        raise LegacyBaselineUnavailable("legacy capture returned inconsistent source or cases")
    for records in baseline["captures"].values():
        if len(records) != 4:
            raise LegacyBaselineUnavailable("legacy capture did not run all four tiers")
        for record in records:
            _fields(record, FIELDS)
    # Do not truncate a previous baseline when capture or validation fails.
    with tempfile.NamedTemporaryFile(mode="w", dir=output.parent, encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(baseline, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            stream.close()
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)


async def _capture_legacy_claude(checkout: Path) -> dict[str, Any]:
    """Child-only driver: real old settings, SQL persistence, tiers and adapter."""
    source = claude_legacy_source(checkout)
    # -I ignores PYTHONPATH and the caller's test imports. The old checkout wins
    # over any editable installation in the interpreter's site-packages.
    sys.path[:0] = [str(checkout), str(checkout / "tests")]
    import asyncpg
    import looper
    import agent_launch_parity as tiers
    from looper.agents import claude_cli
    from looper.db import agent_sessions, connection

    if (Path(looper.__file__).resolve() != checkout / "looper/__init__.py"
            or Path(tiers.__file__).resolve() != checkout / "tests/agent_launch_parity.py"):
        raise LegacyBaselineUnavailable("legacy capture imported a different checkout")
    dsn = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not dsn:
        raise LegacyBaselineUnavailable("Claude baseline requires a disposable test database")
    captures = {}
    for name, (account, url, background, teams, mode, scope) in CLAUDE_CASES.items():
        schema = f"looper_claude_baseline_{uuid.uuid4().hex}"
        admin = await asyncpg.connect(dsn)
        pool = None
        try:
            await admin.execute(f'CREATE SCHEMA "{schema}"')
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4,
                                            server_settings={"search_path": schema})
            await connection.ensure_schema(pool)
            connection._pool = pool
            await tiers.seed_projects(pool)
            catalog = [{"name": "main-model[1m]", "context_window": 1_000_000,
                        "input_usd_per_mtok": 0.6, "output_usd_per_mtok": 2.2,
                        "cache_read_usd_per_mtok": 0, "cache_write_usd_per_mtok": 1,
                        "efforts": ["high", "low"], "background_model": background}]
            await pool.execute(
                "INSERT INTO llm_providers (id, name, auth, protocols, oauth_runtime_type, base_url, models, env) "
                "VALUES ('prov-capture', 'capture', $1, ARRAY['anthropic-messages'], $2, $3, $4::jsonb, $5::jsonb)",
                account, "claude-code" if account == "oauth" else None, url, json.dumps(catalog),
                json.dumps([{"key": "API_TIMEOUT_MS", "value": "3000000"}]),
            )
            if account == "api_key":
                await pool.execute("INSERT INTO llm_provider_secrets VALUES ('prov-capture', 'sk-test-capture')")
            await pool.execute(
                "INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model, effort, agent_teams) "
                "VALUES ('profile-capture', 'capture', 'rt-claude-code', 'prov-capture', 'main-model[1m]', 'high', $1)",
                teams,
            )
            await pool.execute(
                "INSERT INTO agent_setting_layers (scope, profile_id, run_budget_usd) "
                "VALUES ('system', 'profile-capture', 0.5)")
            with tempfile.TemporaryDirectory(prefix="looper-claude-baseline-", dir="/tmp") as directory:
                root = Path(directory).resolve()
                environment = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
                               "CLAUDE_CONFIG_DIR": str(root / "login"),
                               "ANTHROPIC_API_KEY": "sk-test-inherited-key",
                               "ANTHROPIC_AUTH_TOKEN": "sk-test-inherited-token",
                               "ANTHROPIC_BASE_URL": "https://inherited.example.test/",
                               "ANTHROPIC_DEFAULT_HAIKU_MODEL": "inherited-side",
                               "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}
                login = root / "login/.credentials.json"
                login.parent.mkdir()
                login.write_text('{"synthetic":true}\n', encoding="utf-8")
                states: list[dict[str, Any]] = []
                original_capture = tiers.capture

                async def exercise(*args: Any) -> Any:
                    result = await original_capture(*args)
                    records = await pool.fetch(
                        "SELECT session_id, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
                        "cost_usd, status FROM llm_records ORDER BY id")
                    if len(records) != 4 or any(row["status"] != "completed" for row in records):
                        raise LegacyBaselineUnavailable("legacy tiers did not persist four completed runs")
                    for index, row in enumerate(records):
                        owner = "manager" if index == 0 else f"planner-{tiers.TASKS[tiers.PROJECT_A]}"
                        session = await agent_sessions.get_session(owner) if index < 2 else {
                            "session_id": row["session_id"]}
                        states.append({"session": session, "totals": {key: row[key] for key in
                                       ("input_tokens", "output_tokens", "cache_read_tokens",
                                        "cache_creation_tokens", "cost_usd")}})
                    return result

                cli = tiers._CLAUDE.replace(
                    "'total_cost_usd': 0,", "'total_cost_usd': 0.25, 'modelUsage': {{'main-model[1m]': "
                    "{{'inputTokens': 100, 'outputTokens': 40, 'cacheReadInputTokens': 5, "
                    "'cacheCreationInputTokens': 2}}}},")
                from itertools import count
                ids = count(1)
                with patch.dict(os.environ, environment, clear=True), patch.object(tiers, "_CLAUDE", cli), \
                        patch.object(tiers, "capture", exercise), \
                        patch.object(claude_cli, "uuid4", lambda: uuid.UUID(int=next(ids))):
                    await tiers.seed_rest("unconfigured", root)
                    if mode is not None:
                        for role in ("manager", "planner", "worker", "reviewer"):
                            if scope == "task" and role != "manager":
                                await pool.execute(
                                    "INSERT INTO agent_setting_layers (scope, role, system_prompt_mode) "
                                    "VALUES ('role', $1, 'append')", role)
                                await pool.execute("INSERT INTO task_system_prompt_modes VALUES ($1, $2, $3)",
                                                   tiers.TASKS[tiers.PROJECT_A], role, mode)
                            else:
                                await pool.execute(
                                    "INSERT INTO agent_setting_layers (scope, role, system_prompt_mode) "
                                    "VALUES ('role', $1, $2)", role, mode)
                    records = await capture_claude_tiers(root, "unconfigured", read_state=states.__getitem__)
                    captures[name] = [normalize(record, root=root) for record in records]
        finally:
            connection._pool = None
            if pool is not None:
                await pool.close()
            try:
                await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await admin.close()
    return {"source": source, "captures": captures}


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


def normalize(record: dict[str, Any], *, root: Path,
              time_paths: Iterable[tuple[str, ...]] = ()) -> dict[str, Any]:
    """Return a detached contract, normalizing only root paths and opted-in times."""
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

    return replace(result)


def assert_parity(expected: list[dict[str, Any]], actual: list[dict[str, Any]], *,
                  expected_root: Path, actual_root: Path,
                  time_paths: Iterable[tuple[str, ...]] = ()) -> None:
    """Compare every launch, with a field-level diff. Empty baselines are errors."""
    if not expected or not actual:
        raise ValueError("launch captures must be non-empty")
    if len(expected) != len(actual):
        raise AssertionError(f"launch count: expected {len(expected)}, actual {len(actual)}")
    paths = tuple(time_paths)
    for index, (before, after) in enumerate(zip(expected, actual)):
        left = json.dumps(normalize(before, root=expected_root, time_paths=paths),
                          ensure_ascii=False, sort_keys=True, indent=2)
        right = json.dumps(normalize(after, root=actual_root, time_paths=paths),
                           ensure_ascii=False, sort_keys=True, indent=2)
        if left != right:
            diff = "\n".join(difflib.unified_diff(left.splitlines(), right.splitlines(),
                                                fromfile="legacy", tofile="current", lineterm=""))
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

    ``read_state`` returns ``session`` and ``totals`` from the driver's
    persistence *after* the exercise. The other fields are read from the
    actual Launch at subprocess creation, including explicit binding removals.
    Imports are lazy so this same helper can be copied into the old checkout.
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
        _fields(state, frozenset({"session", "totals"}))
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


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Capture the patched old Claude baseline without model calls")
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--capture", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.capture:
        print(json.dumps(asyncio.run(_capture_legacy_claude(options.checkout.resolve()))))
    elif options.output is None:
        parser.error("--output is required")
    else:
        generate_claude_baseline(options.checkout, options.output)
