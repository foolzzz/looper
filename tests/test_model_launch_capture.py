"""The portable launch contract must retain differences that affect a model run."""

from __future__ import annotations

import asyncio
import ast
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import agent_launch_parity as claude_parity
from model_launch_parity import FIELDS, TIME_PATHS, assert_parity, capture, normalize


def _record(root: Path) -> dict[str, Any]:
    return {
        "argv": [str(root / "bin/claude"), "--model", "model[1m]"],
        "env": {"ANTHROPIC_BASE_URL": "https://provider.example.test/api/anthropic/",
                "ANTHROPIC_API_KEY": "sk-test-capture", "HOME": str(root / "home")},
        "unset": ["ANTHROPIC_AUTH_TOKEN"],
        "config": {"settings.json": '{"protocol":"anthropic-messages","name":"provider"}\n'},
        "prompt": f"Read {root}/work. Preserve 2026-10-20T10:00:00+00:00 verbatim.\n",
        "first_input": "What can you do?\n",
        "permissions": {"read_only": True},
        "isolation": {"home": str(root / "home"), "mode": "0700"},
        "mcp": {"docs": {"type": "http", "url": "https://docs.example.test/mcp"}},
        "login_target": str(root / "operator/auth.json"),
        "session": {"id": "sess-parity", "created_at": "2026-10-20T10:00:00+00:00"},
        "totals": {"input_tokens": 100, "cost_usd": 0.25},
        "price": {"model[1m]": {"input_usd_per_mtok": 0.6, "output_usd_per_mtok": 2.2}},
        "cap": 0.5,
    }


@pytest.mark.parametrize(("field", "value"), [
    ("env", {"ANTHROPIC_BASE_URL": "https://other.example.test/api/anthropic/"}),
    ("config", {"settings.json": '{"protocol":"openai-responses","name":"provider"}\n'}),
    ("config", {"settings.json": '{"protocol":"anthropic-messages","name":"renamed"}\n'}),
    ("env", {"ANTHROPIC_API_KEY": "sk-test-different"}),
    ("unset", []),
    ("argv", ["claude", "--model", "model"]),
    ("prompt", "Changed prompt."),
    ("first_input", "Changed input.\n"),
    ("permissions", {"read_only": False}),
    ("isolation", {"mode": "0755"}),
    ("mcp", {"docs": {"type": "sse", "url": "https://docs.example.test/mcp"}}),
    ("login_target", "/somewhere/else/auth.json"),
    ("session", {"id": "other-session", "created_at": "2026-10-20T10:00:00+00:00"}),
    ("totals", {"input_tokens": 101, "cost_usd": 0.25}),
    ("price", {"model[1m]": {"input_usd_per_mtok": 0.7, "output_usd_per_mtok": 2.2}}),
    ("cap", 0.6),
])
def test_behavior_changes_are_never_normalized_away(tmp_path: Path, field: str, value: Any) -> None:
    expected = _record(tmp_path)
    actual = copy.deepcopy(expected)
    if field == "env":
        actual[field].update(value)
    else:
        actual[field] = value
    with pytest.raises(AssertionError, match=field):
        assert_parity([expected], [actual], expected_root=tmp_path, actual_root=tmp_path)


def test_only_temporary_roots_and_explicit_session_times_can_differ(tmp_path: Path) -> None:
    before, after = tmp_path / "old", tmp_path / "new"
    expected, actual = _record(before), _record(after)
    actual["session"]["created_at"] = "2026-10-21T10:01:02+00:00"
    with pytest.raises(AssertionError, match="session"):
        assert_parity([expected], [actual], expected_root=before, actual_root=after)
    assert_parity([expected], [actual], expected_root=before, actual_root=after,
                  time_paths=(("session", "created_at"),))
    normalized = normalize(expected, root=before)
    assert normalized["env"]["HOME"] == "<root>/home"
    assert normalized["env"]["ANTHROPIC_BASE_URL"].endswith("/anthropic/")
    assert normalized["config"] == expected["config"]  # exact bytes, including trailing newline
    assert expected == _record(before)  # callers and old fixtures are never mutated


@pytest.mark.parametrize("path", [
    ("env", "ANTHROPIC_BASE_URL"), ("argv",), ("config", "protocol"), ("prompt",),
    ("session", "id"), ("totals", "cost_usd"), ("price",), ("cap",),
    ("session", "anything_at"),
])
def test_normalization_has_a_closed_allowlist(tmp_path: Path, path: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="time_paths"):
        normalize(_record(tmp_path), root=tmp_path, time_paths=(path,))


def test_allowed_times_must_exist_and_be_timestamps(tmp_path: Path) -> None:
    assert TIME_PATHS == frozenset({("session", "created_at"), ("session", "updated_at")})
    for value in (None, 123, "not a timestamp"):
        record = _record(tmp_path)
        record["session"]["created_at"] = value
        with pytest.raises(ValueError, match="timestamp"):
            normalize(record, root=tmp_path, time_paths=(("session", "created_at"),))
    with pytest.raises(ValueError, match="missing"):
        normalize(_record(tmp_path), root=tmp_path, time_paths=(("session", "updated_at"),))


@pytest.mark.parametrize("field", sorted(_record(Path("/capture"))))
def test_every_contract_field_is_required(tmp_path: Path, field: str) -> None:
    record = _record(tmp_path)
    del record[field]
    with pytest.raises(ValueError, match="fields"):
        normalize(record, root=tmp_path)


def test_extra_fields_empty_captures_and_type_changes_fail(tmp_path: Path) -> None:
    record = _record(tmp_path)
    with pytest.raises(ValueError, match="fields"):
        normalize({**record, "ignored": "not allowed"}, root=tmp_path)
    with pytest.raises(ValueError, match="non-empty"):
        assert_parity([], [], expected_root=tmp_path, actual_root=tmp_path)
    with pytest.raises(AssertionError, match="launch"):
        assert_parity([record], [record, record], expected_root=tmp_path, actual_root=tmp_path)
    changed = copy.deepcopy(record)
    changed["totals"]["input_tokens"] = True
    with pytest.raises(AssertionError, match="totals"):
        assert_parity([record], [changed], expected_root=tmp_path, actual_root=tmp_path)


def test_root_replacement_does_not_hide_similarly_named_paths(tmp_path: Path) -> None:
    record = _record(tmp_path)
    record["login_target"] = str(tmp_path) + "-other/auth.json"
    assert normalize(record, root=tmp_path)["login_target"] == record["login_target"]


def test_existing_claude_capture_is_a_nonempty_positive_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    # The old capture drives all four real tiers. Only persistence is replaced;
    # every CLI is the existing stand-in, with a disposable HOME and runtime root.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("LOOPER_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("PGPASSWORD", "synthetic-inherited-token")
    from unittest.mock import AsyncMock
    from looper.db import profile_registry as db_registry

    monkeypatch.setattr(db_registry, "get_profile", AsyncMock(return_value=None))
    old: dict[str, Any] = {}

    async def exercise() -> None:
        old.update(await claude_parity.capture(tmp_path, "unconfigured"))

    def inspect(argv: list[str], env: dict[str, str], cwd: str) -> dict[str, Any]:
        prompt_flag = next(flag for flag in ("--append-system-prompt-file", "--system-prompt-file") if flag in argv)
        prompt = Path(argv[argv.index(prompt_flag) + 1])
        settings = argv[argv.index("--settings") + 1]
        mcp = argv[argv.index("--mcp-config") + 1] if "--mcp-config" in argv else None
        return {"unset": [], "config": {"--settings": settings}, "prompt": prompt.read_text(),
                "permissions": [arg for arg in argv if "permission" in arg.lower()],
                "isolation": json.loads(settings), "mcp": mcp, "login_target": None}

    def readback(index: int) -> dict[str, Any]:
        # Saved resident sessions are read after the exercise, not guessed at launch.
        owner = "manager" if index == 0 else f"planner-{claude_parity.TASKS[claude_parity.PROJECT_A]}"
        saved = agent_db.store.sessions.get(owner) if index < 2 else None
        return {"session": {"id": saved["session_id"]} if saved else None,
                "totals": saved["session_totals"] if saved else None,
                "price": None, "cap": None}

    records = asyncio.run(capture(tmp_path, exercise, inspect_launch=inspect, readback=readback))
    assert len(records) == len(old) == 4
    assert set(old) == set(claude_parity.SCENARIOS["unconfigured"]["cases"])
    for record, previous in zip(records, old.values()):
        assert set(record) == FIELDS
        assert record["prompt"] and record["prompt"].replace(str(tmp_path), "<root>") == previous["system_prompt"]
        assert json.loads(record["first_input"])["message"]["content"].replace(str(tmp_path), "<root>") == previous["stdin"]
        assert record["argv"][0].startswith(str(tmp_path / "bin"))
        assert "PGPASSWORD" in record["unset"]
        assert "synthetic-inherited-token" not in json.dumps(record)
    assert_parity(records, json.loads(json.dumps(records)), expected_root=tmp_path, actual_root=tmp_path)


def test_contract_imports_without_any_checkout_on_python_path(tmp_path: Path) -> None:
    # Copy only this helper to stand in for using it on an older checkout.
    import model_launch_parity

    helper = tmp_path / "model_launch_parity.py"
    helper.write_text(Path(model_launch_parity.__file__).read_text())
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import runpy, sys; api = runpy.run_path(sys.argv[1]); "
         "assert 'looper' not in sys.modules; print(len(api['FIELDS']))", str(helper)],
        cwd=tmp_path, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == len(FIELDS)


def test_timeout_kills_the_fake_process_and_restores_the_interception(tmp_path: Path) -> None:
    from fake_claude import fake_claude

    executable = fake_claude(tmp_path / "waiting-cli", "import sys\nsys.stdin.read()\n")
    original = asyncio.create_subprocess_exec
    processes = []
    details = {field: _record(tmp_path)[field] for field in
               ("unset", "config", "prompt", "permissions", "isolation", "mcp", "login_target")}

    async def exercise() -> None:
        process = await asyncio.create_subprocess_exec(executable, stdin=asyncio.subprocess.PIPE)
        processes.append(process)
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        asyncio.run(capture(tmp_path, exercise, inspect_launch=lambda *_: details,
                            readback=lambda _: {}, timeout=0.2))
    assert len(processes) == 1 and processes[0].returncode is not None
    assert asyncio.create_subprocess_exec is original


@pytest.mark.parametrize("field", ["config", "prompt", "first_input", "session", "totals", "price", "cap"])
def test_non_json_capture_values_are_rejected(tmp_path: Path, field: str) -> None:
    record = _record(tmp_path)
    record[field] = Path("not-json")
    with pytest.raises(ValueError, match="JSON"):
        normalize(record, root=tmp_path)


def test_capture_rejects_an_exercise_that_never_starts_a_cli(tmp_path: Path) -> None:
    async def exercise() -> None:
        pass

    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(capture(tmp_path, exercise, inspect_launch=lambda *_: {}, readback=lambda _: {}))


@pytest.mark.parametrize("runtime", ["codex", "omp", "grok"])
def test_existing_runtime_fakes_work_with_the_same_capture_interface(
    runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fake_claude import fake_claude
    from looper.agents import profile_registry as registry
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.runtimes import ApiProtocol, AuthKind, adapter
    from looper.agents.run_usage import run_usage
    from looper.agents.system_prompt import SystemPrompt

    # Read the existing literal without importing that test module's model
    # constructors: those constructors differ between the old and new schema.
    source = ast.parse(Path(__file__).with_name(f"test_{runtime}_runtime.py").read_text())
    body = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
                and any(isinstance(name, ast.Name) and name.id == f"_FAKE_{runtime.upper()}"
                        for name in node.targets))
    executable = fake_claude(tmp_path / runtime, body)
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    login = tmp_path / "operator-grok" / "auth.json"
    login.parent.mkdir()
    login.write_text("synthetic login")
    monkeypatch.setenv("GROK_HOME", str(login.parent))
    adapter_ = adapter(runtime)
    provider_cls = getattr(registry, "LegacyProviderSpec", registry.ProviderSpec)
    from runtime_model_helpers import resolved_profile as profile_cls
    provider = provider_cls("prov-parity", "parity", AuthKind.OAUTH if runtime == "grok" else AuthKind.API_KEY,
                            frozenset({ApiProtocol.OPENAI_RESPONSES}),
                            **({"oauth_runtime_type": "grok"} if runtime == "grok" else
                               {"base_url": "https://provider.example.test/v1", "api_key": "sk-test-capture"}))
    profile = profile_cls("profile-parity", "parity", registry.RuntimeSpec(
        f"rt-{runtime}", runtime, runtime, executable, adapter_.capabilities), provider,
        "grok-4.7" if runtime == "grok" else "gpt-5.5", "low")
    launch = Launch(AgentSettings(profile=profile, max_budget_usd=0.5), role=AgentType.WORKER)
    prompt = SystemPrompt("The portable capture prompt.").write_file(tmp_path / "runtime")
    state: dict[str, Any] = {}

    def inspect(argv: list[str], env: dict[str, str], cwd: str) -> dict[str, Any]:
        if runtime == "codex":
            home = Path(env["CODEX_HOME"])
            files = [home / "config.toml", home / "AGENTS.md"]
        elif runtime == "grok":
            home = Path(env["GROK_HOME"])
            files = [home / "config.toml", home / "rules/looper.md"]
        else:
            home = Path(env["HOME"])
            files = sorted(home.rglob("models.yml")) + [Path(argv[argv.index("--append-system-prompt") + 1])]
        assert files and all(file.is_file() for file in files)
        binding = adapter_.bind_provider(launch.profile.provider, launch.model, launch.effort)
        return {"unset": list(binding.unset), "config": {str(file): file.read_text() for file in files},
                "prompt": prompt.read_text(), "permissions": {"read_only": launch.read_only},
                "isolation": {"home": str(home), "mode": oct(home.stat().st_mode & 0o777)},
                "mcp": launch.library.mcp_config,
                "login_target": str((home / "auth.json").readlink()) if (home / "auth.json").is_symlink() else None}

    async def exercise() -> None:
        command = adapter_.run_command(executable, cwd=tmp_path, prompt_file=prompt, launch=launch)
        process = await asyncio.create_subprocess_exec(*command, env=launch.environment(), cwd=tmp_path,
                                                       stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
        output, error = await process.communicate(b"First input.\n")
        assert process.returncode == 0, error.decode()
        reader = asyncio.StreamReader()
        reader.feed_data(output)
        reader.feed_eof()
        events = [event async for event in adapter_.parse(reader, model=launch.model)]
        result = next(event for event in reversed(events) if event["type"] == "result")
        assert not result.get("is_error")
        state.update(session={"id": result["session_id"]}, totals=run_usage(result).to_dict(),
                     price={"source": launch.cost_source.value,
                            "probe_cost": launch.price(launch.model, run_usage(result)) if launch.price else None},
                     cap=launch.max_budget_usd)

    records = asyncio.run(capture(tmp_path, exercise, inspect_launch=inspect, readback=lambda _: state))
    assert len(records) == 1
    seen = records[0]
    assert seen["first_input"] == "First input.\n"
    assert seen["totals"]["input_tokens"] > 0 and seen["session"]["id"]
    assert seen["cap"] == 0.5 and seen["price"]["source"]
    if runtime == "grok":
        assert seen["login_target"] == str(login)
    assert_parity(records, json.loads(json.dumps(records)), expected_root=tmp_path, actual_root=tmp_path)
