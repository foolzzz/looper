"""Typed TTADK launchers preserve Codex's commands, stream and isolated homes."""

from __future__ import annotations

import asyncio
import json
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings, AgentType, Launch, profile_launch_problem
from looper.agents.profile_registry import (
    Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
)
from looper.agents.runtimes import ApiProtocol, AuthKind, Launcher, Probe, RuntimeUnsupported, VendorType, adapter
from looper.agents.runtimes import codex_login, ttadk
from looper.agents.runtimes.codex import CodexRuntime, ResidentCodex, config_toml
from looper.agents.system_prompt import SystemPrompt
from looper.agents.tiers import worker
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from test_agent_planner_worker import _step_envelope, _worker_row

CODEX: CodexRuntime = adapter("codex")  # type: ignore[assignment]


def _launch(launcher: Launcher = Launcher.TTADK, executable: str = "ttadk") -> Launch:
    provider = ProviderSpec("prov-codex-login", "codex-login", VendorType.CODEX_LOGIN, AuthKind.OAUTH, "codex")
    model = ModelSpec("model-login", provider.id, "gpt-6.1-sol", ApiProtocol.OPENAI_RESPONSES)
    runtime = RuntimeSpec("rt-ttadk-codex" if launcher is Launcher.TTADK else "rt-codex",
                          "ttadk-codex" if launcher is Launcher.TTADK else "codex", "codex", executable,
                          CODEX.capabilities, launcher=launcher, version="ttadk 1.2 / codex-cli 0.161.0")
    profile = ResolvedAgentProfile("agent-login", "codex/login", runtime, model.id,
        resolved_model=ResolvedModel(model, provider, Endpoint(provider.id, model.protocol, "")))
    return Launch(AgentSettings(profile=profile))


@pytest.fixture(autouse=True)
def _isolated_ttadk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Every tool setting is synthetic; no read reaches the operator's TTADK directory.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "operator"))
    ttadk.forget_probe()
    yield
    ttadk.forget_probe()


@pytest.mark.parametrize("builder", ["run", "kept-run", "resident-resume", "steer", "consult"])
def test_ttadk_prefix_preserves_every_codex_command(builder: str, tmp_path: Path) -> None:
    prompt = SystemPrompt("The same prompt.").write_file(tmp_path / "runtime")
    kept = tmp_path / "kept"
    (kept / "sessions").mkdir(parents=True)
    (kept / "sessions" / "rollout.jsonl").write_text("{}\n")

    def command(launcher: Launcher) -> list[str]:
        launch = _launch(launcher, "codex" if launcher is Launcher.NATIVE else "ttadk")
        args = {"cwd": tmp_path, "prompt_file": prompt, "launch": launch}
        executable = launch.profile.runtime.executable
        if builder == "run":
            return CODEX.run_command(executable, **args)
        if builder == "kept-run":
            return CODEX.run_command(executable, **args, session_dir=kept)
        if builder == "resident-resume":
            return CODEX.resident_command(executable, **args) + CODEX.resume_args("thread-1")
        if builder == "steer":
            return CODEX.steer_command(executable, **args)
        return CODEX.consult_command(executable, **{**args, "launch": replace(launch, consult=True)},
            session_id="thread-1", session_dir=kept, fork_dir=tmp_path / f"fork-{launcher.value}")

    native, wrapped = command(Launcher.NATIVE), command(Launcher.TTADK)
    assert native[:1] == wrapped[:1] == ["env"]
    assert native[1].startswith("CODEX_HOME=") and wrapped[1].startswith("CODEX_HOME=")
    assert native[2] == "codex"
    assert wrapped[2:9] == ["ttadk", "codex", "--skip-check", "--mode", "mixed", "--", native[3]]
    assert wrapped[8:] == native[3:]
    assert "--skip-check" not in native and "mixed" not in native
    if builder == "resident-resume":
        base, session_id, executable = ResidentCodex(label="planner")._split(wrapped)
        assert base == wrapped[:-2] and session_id == "thread-1" and executable == "ttadk"


@pytest.mark.parametrize("args", [["-v"], ["--version"], ["handoff"], ["--debug"], ["--verbose"],
    ["--log-level"], ["-c", "model_provider=x"], ["exec", "--", "-c", "model_provider=x"]])
def test_ttadk_rejects_flags_that_break_its_protocol_or_gateway(args: list[str]) -> None:
    with pytest.raises(RuntimeUnsupported, match="model_provider|-v|handoff|--debug|--verbose|--log-level"):
        ttadk.check_args(args)


def test_ttadk_accepts_codex_configuration_and_exact_argument_matches() -> None:
    ttadk.check_args(["exec", "-c", 'model_reasoning_effort="high"', "--model", "model_provider-name", "--debugger"])
    assert ttadk.prefix("ttadk", "cursor-cli") == ["ttadk", "code", "-t", "cursor-cli", "--skip-check", "--"]
    with pytest.raises(ValueError):
        ttadk.prefix("ttadk", "unsupported")


_FAKE_CODEX = """\
import json, os, sys
args = sys.argv[1:]
assert args[0] == 'exec' and args[-1] == '-', args
home = os.environ['CODEX_HOME']
open(os.path.join(home, 'argv.jsonl'), 'a').write(json.dumps(args) + '\\n')
sys.stdin.read()
path = os.path.join(home, 'turns')
turns = int(open(path).read()) + 1 if os.path.exists(path) else 1
open(path, 'w').write(str(turns))
if turns > 1:
    assert args[args.index('resume') + 1] == 'thread-ttadk', args
def emit(event):
    print(json.dumps(event), flush=True)
emit({'type': 'thread.started', 'thread_id': 'thread-ttadk'})
emit({'type': 'turn.started'})
emit({'type': 'item.completed', 'item': {'id': 'reply', 'type': 'agent_message',
    'text': json.dumps({'status': 'completed', 'summary': 'done', 'changed_files': {}})}})
emit({'type': 'turn.completed', 'usage': {'input_tokens': 100 * turns, 'cached_input_tokens': 40 * turns,
    'output_tokens': 10 * turns}})
"""


def _wrapper(tmp_path: Path, *, mode: str = "success") -> tuple[str, Path]:
    calls = tmp_path / "ttadk-calls.jsonl"
    codex = fake_claude(tmp_path / "codex", _FAKE_CODEX)
    executable = fake_claude(tmp_path / "ttadk", (
        "import json, os, sys, time\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'models':\n"
        "    assert sys.argv[1:] == ['models', '-t', 'codex', '-f', 'json']\n"
        f"    mode = {mode!r}\n"
        "    if mode == 'not-signed-in': sys.exit(1)\n"
        "    if mode == 'timeout': time.sleep(3)\n"
        "    print(json.dumps({'models': []})); sys.exit(0)\n"
        "assert sys.argv[1:7] == ['codex', '--skip-check', '--mode', 'mixed', '--', 'exec'], sys.argv\n"
        "for i in range(19): print(f'TTADK banner {i}', flush=True)\n"
        + ("print('boom', file=sys.stderr); sys.exit(0)\n" if mode == "empty-result" else
           f"os.execv({codex!r}, [{codex!r}, *sys.argv[6:]])\n")
    ))
    return executable, calls


async def _worker_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch: Launch,
                      *, gate: bool = False) -> dict[str, Any]:
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 5, "max_turns": 0})

    async def settings(*args: Any, **kwargs: Any) -> AgentSettings:
        problem = await profile_launch_problem(launch.settings.profile, AgentType.WORKER) if gate else None
        return replace(launch.settings, profile_error=problem)

    monkeypatch.setattr(worker, "load_agent_settings", settings)
    pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path / "runtime")
    return await pool._execute(_step_envelope(tmp_path / "work", verify=False))


@pytest.mark.parametrize(("mode", "problem"), [
    ("not-signed-in", ttadk.TtadkProblem.NOT_SIGNED_IN), ("success", None),
    ("timeout", ttadk.TtadkProblem.CHECK_FAILED), ("missing", ttadk.TtadkProblem.NOT_FOUND),
])
def test_ttadk_launch_gate_stops_the_worker_before_codex(
    mode: str, problem: ttadk.TtadkProblem | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    executable, calls = _wrapper(tmp_path, mode=mode)
    if mode == "missing":
        executable = str(tmp_path / "missing-ttadk")
    monkeypatch.setattr(ttadk, "_SIGN_IN_TIMEOUT_SEC", 1.0)
    native_check = AsyncMock(return_value=None)
    monkeypatch.setattr(codex_login, "launch_problem", native_check)
    result = asyncio.run(asyncio.wait_for(_worker_run(tmp_path, monkeypatch, _launch(executable=executable), gate=True), 8))
    invocations = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    native_check.assert_awaited_once()
    if problem is None:
        assert result["status"] == "completed"
        assert [argv[0] for argv in invocations] == ["models", "codex"]
    else:
        assert result["status"] == "failed" and problem.message("codex") in result["error"]
        assert all(argv[0] == "models" for argv in invocations)
        assert not list((tmp_path / "runtime").rglob("argv.jsonl"))


@pytest.mark.parametrize("mode", ["success", "empty-result"])
def test_worker_reads_codex_stream_after_wrapper_banners_and_handles_false_success(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    executable, calls = _wrapper(tmp_path, mode=mode)
    launch = _launch(executable=executable)
    result = asyncio.run(asyncio.wait_for(_worker_run(tmp_path, monkeypatch, launch), 8))
    assert json.loads(calls.read_text().splitlines()[0])[0] == "codex"
    if mode == "success":
        assert result["status"] == "completed" and result["summary"] == "done"
    else:
        assert result["status"] == "failed"
        assert result["error"] == "Codex through TTADK ended without a result"
        assert "boom" in result["stderr"]
    assert launch.exit_code_reliable is False
    assert _launch(Launcher.NATIVE).exit_code_reliable is True


def test_resident_codex_keeps_wrapper_and_resumes_its_thread_after_banners(
    tmp_path: Path, agent_db: Any,
) -> None:
    executable, calls = _wrapper(tmp_path)
    launch = _launch(executable=executable)
    command = CODEX.resident_command(executable, cwd=tmp_path,
        prompt_file=SystemPrompt("PROMPT").write_file(tmp_path / "runtime"), launch=launch)

    async def run() -> Any:
        session = CODEX.resident_session("planner-ttadk")
        seen: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            seen.append(event)

        try:
            await session.start(command, cwd=str(tmp_path), signature="same", env=launch.environment())
            one = await session.run_turn("first", on_event)
            two = await session.run_turn("second", on_event)
            assert await CODEX.after_run(launch, session._turn_argv()) == []
            return one, two, seen
        finally:
            await session.stop()

    one, two, seen = asyncio.run(asyncio.wait_for(run(), 8))
    assert one.result_event["subtype"] == two.result_event["subtype"] == "success"
    assert {event.get("session_id") for event in seen} == {"thread-ttadk"}
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == 2 and "resume" not in invocations[0]
    assert invocations[1][-3:] == ["resume", "thread-ttadk", "-"]
    assert one.usage == two.usage


def test_ttadk_login_config_excludes_gateway_key_and_native_config_is_unchanged(tmp_path: Path) -> None:
    native = _launch(Launcher.NATIVE, "codex")
    before = config_toml(native, shell_home=tmp_path / "shell")
    wrapped = config_toml(_launch(), shell_home=tmp_path / "shell")
    assert tomllib.loads(wrapped)["shell_environment_policy"]["exclude"] == ["TTADK_OPENAI_API_KEY"]
    assert "exclude" not in tomllib.loads(before)["shell_environment_policy"]
    assert wrapped.replace('exclude = ["TTADK_OPENAI_API_KEY"]\n', "") == before
    assert config_toml(native, shell_home=tmp_path / "shell").encode() == before.encode()


def test_ttadk_environment_removes_wrapper_controls_and_keeps_native_environment() -> None:
    removed = {"CI", "TTADK_NO_TELEMETRY", "TTADK_DEBUG", "DEBUG", "FORCE_COLOR"}
    base = {**dict.fromkeys(removed, "yes"), "KEEP": "value"}
    assert ttadk.environment(base) == {"KEEP": "value", "TTADK_DISABLE_COMPLETION_AUTO_INSTALL": "1"}
    assert set(base) == removed | {"KEEP"}
    def environment(launcher: Launcher) -> dict[str, str]:
        launch = _launch(launcher)
        return replace(launch, settings=replace(launch.settings, env=tuple(base.items()))).environment()

    native, wrapped = environment(Launcher.NATIVE), environment(Launcher.TTADK)
    assert all(native[name] == "yes" for name in removed)
    assert not removed & wrapped.keys() and wrapped["KEEP"] == "value"
    assert removed | {"TTADK_DISABLE_COMPLETION_AUTO_INSTALL"} <= CODEX.provider_env_owned


def test_ttadk_record_and_probe_use_launcher_version_and_native_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = fake_claude(tmp_path / "ttadk", "import sys\nassert sys.argv[1:] == ['--version']\nprint('1.2')\n")
    native_probe = Probe("codex-cli 0.161.0", CODEX.capabilities, None)
    seen: list[str] = []

    def probe(executable: str) -> Probe:
        seen.append(executable)
        return native_probe

    monkeypatch.setattr(CODEX, "probe", probe)
    found = CODEX.probe_for(executable, Launcher.TTADK)
    assert found == Probe("ttadk 1.2 / codex-cli 0.161.0", native_probe.capabilities, None)
    assert seen == ["codex"]
    assert CODEX.launch_record(_launch())["launcher"] == {
        "kind": "ttadk", "version": _launch().profile.runtime.version, "tool": "codex", "mode": "mixed"}
    assert "launcher" not in CODEX.launch_record(_launch(Launcher.NATIVE))


@pytest.mark.parametrize("command", ["custom-codex", "codex --some-option"])
def test_ttadk_probe_uses_single_word_tool_commands_and_explains_fallbacks(
    command: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ttadk, "probe_version", lambda executable: ("1.2", None))
    monkeypatch.setattr(ttadk, "tool_command", lambda tool: command)
    seen: list[str] = []

    def probe(executable: str) -> Probe:
        seen.append(executable)
        return Probe("codex-cli 0.161.0", CODEX.capabilities, None)

    monkeypatch.setattr(CODEX, "probe", probe)
    found = CODEX.probe_for("ttadk", Launcher.TTADK)
    assert seen == [command if command == "custom-codex" else "codex"]
    if command == "custom-codex":
        assert found.error is None
    else:
        assert found.error is not None and "not a single word" in found.error


def test_a_failed_ttadk_version_probe_does_not_probe_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttadk, "probe_version", lambda executable: (None, "unavailable"))
    monkeypatch.setattr(CODEX, "probe", lambda executable: pytest.fail("Codex must not run"))
    assert CODEX.probe_for("ttadk", Launcher.TTADK) == Probe(None, CODEX.capabilities, "unavailable")


def test_launcher_defaults_and_supported_auth_are_typed() -> None:
    assert RuntimeSpec("runtime", "plain", "codex").launcher is Launcher.NATIVE
    assert _launch(executable="").executable("unused-claude") == "ttadk"
    assert CODEX.auth_kinds_for(Launcher.TTADK) == frozenset({AuthKind.OAUTH})
    assert CODEX.auth_kinds_for(Launcher.NATIVE) == CODEX.auth_kinds
    claude = adapter("claude-code")
    assert claude.launchers == frozenset({Launcher.NATIVE})
    assert claude.probe_for("never-called", Launcher.TTADK) == Probe(
        None, claude.capabilities, f"{claude.label} cannot be launched through ttadk")


def test_ttadk_tool_command_uses_only_the_named_tool_and_defaults_when_absent(tmp_path: Path) -> None:
    settings = Path.home() / ".ttadk" / "setting.json"
    assert ttadk.tool_command("codex") == "codex" and ttadk.tool_command("cursor-cli") == "agent"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"ai_tool_commands": {"codex": "custom-codex"}, "token": "never-logged"}))
    assert ttadk.tool_command("codex") == "custom-codex"
    assert ttadk.tool_command("cursor-cli") == "agent"


def test_ttadk_sign_in_answer_is_cached_until_explicitly_forgotten(tmp_path: Path) -> None:
    executable, calls = _wrapper(tmp_path)

    async def check() -> None:
        assert await ttadk.sign_in_problem(executable, "codex") is None
        assert await ttadk.sign_in_problem(executable, "codex") is None
        assert len(calls.read_text().splitlines()) == 1
        ttadk.forget_probe()
        assert await ttadk.sign_in_problem(executable, "codex") is None

    asyncio.run(asyncio.wait_for(check(), 5))
    assert len(calls.read_text().splitlines()) == 2


def test_ttadk_sign_in_cache_is_scoped_to_executable_and_tool_and_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = tmp_path / "checks.jsonl"
    body = (f"import json, sys\nopen({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('{\"models\": []}')\n")
    one = fake_claude(tmp_path / "ttadk-one", body)
    two = fake_claude(tmp_path / "ttadk-two", body)
    now = [100.0]
    # Patch the clock used by this module without changing asyncio's loop clock.
    monkeypatch.setattr(ttadk, "time", type("Clock", (), {"monotonic": staticmethod(lambda: now[0])}))

    async def check() -> None:
        for executable, tool in [(one, "codex"), (one, "cursor-cli"), (two, "codex")]:
            assert await ttadk.sign_in_problem(executable, tool) is None
        now[0] += 59
        assert await ttadk.sign_in_problem(one, "codex") is None
        assert len(calls.read_text().splitlines()) == 3
        now[0] += 2
        assert await ttadk.sign_in_problem(one, "codex") is None

    asyncio.run(asyncio.wait_for(check(), 5))
    assert len(calls.read_text().splitlines()) == 4


@pytest.mark.parametrize("output", ["not-json", "[]", "{}", '{"models": {}}'])
def test_ttadk_unreadable_models_output_is_a_failed_check(output: str, tmp_path: Path) -> None:
    executable = fake_claude(tmp_path / "ttadk", f"print({output!r})\n")
    assert asyncio.run(ttadk.sign_in_problem(executable, "codex")) is ttadk.TtadkProblem.CHECK_FAILED
