"""pi's identity and its version/help probe (the pi runtime RFC, sections 1 and 6).

The launch, sessions and stream are refused. Capabilities stay empty. A probe
runs only ``--version`` and ``--help``, offline, in temporary directories.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from fake_claude import fake_claude
from looper.agents.profile_registry import servable_roles
from looper.agents.runtimes import (
    ADAPTERS, ApiProtocol, AuthKind, Probe, RuntimeAdapter, RuntimeUnsupported, adapter,
)
from looper.agents.runtimes.pi import PROBE_HELP_TOKENS, PiRuntime

PI = PiRuntime()


def test_the_module_imports_and_pi_runtime_can_be_constructed() -> None:
    assert isinstance(PI, RuntimeAdapter)
    assert (PI.type, PI.label, PI.default_executable) == ("pi", "pi", "pi")
    assert PI.capabilities == frozenset()
    assert PI.capabilities_for("1.3.0") == frozenset()
    assert PI.capabilities_for(None) == frozenset()
    assert PI.protocols == frozenset({ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT})
    assert ApiProtocol.ANTHROPIC_MESSAGES not in PI.protocols
    assert PI.auth_kinds == frozenset({AuthKind.API_KEY})
    assert AuthKind.OAUTH not in PI.auth_kinds
    assert adapter("pi") is ADAPTERS["pi"]
    assert servable_roles(PI.capabilities) == []


def test_the_probe_requires_the_launch_flags() -> None:
    assert PROBE_HELP_TOKENS == (
        "-p", "--mode", "json", "rpc", "--offline", "--no-approve", "--no-context-files", "--no-extensions",
        "--no-mcp", "--no-skills", "--no-prompt-templates", "--no-themes", "-e", "--provider", "--model",
        "--thinking", "--system-prompt", "--append-system-prompt", "--tools", "--no-session", "--session-dir",
        "--session",
    )


def _help(*without: str) -> str:
    return " ".join(token for token in PROBE_HELP_TOKENS if token not in without)


def _fake(path: Path, *, version: str = "1.3.0", help_text: str | None = None, empty: bool = False,
          help_code: int = 0) -> str:
    """A stand-in pi. ``empty`` makes ``--version`` print nothing."""
    if help_text is None:
        help_text = _help()
    version_line = "sys.exit(0)\n" if empty else f"print({version!r})\n    sys.exit(0)\n"
    return fake_claude(path, (
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        f"    {version_line}"
        "if args == ['--help']:\n"
        f"    print({help_text!r})\n"
        f"    sys.exit({help_code})\n"
        "sys.exit(9)\n"
    ))


@pytest.mark.parametrize("version", ["1.3.0", "1.10.0", "2.0.0"])
def test_a_new_enough_pi_with_the_flags_probes_clean_and_offers_nothing(tmp_path: Path, version: str) -> None:
    probe = PI.probe(_fake(tmp_path / "pi", version=version))
    assert probe == Probe(version, frozenset())


def test_a_missing_command_is_refused(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing-pi")
    probe = PI.probe(missing)
    assert probe.version is None and probe.capabilities == frozenset()
    assert probe.error == f"{missing!r} was not found"


def test_empty_version_output_is_refused(tmp_path: Path) -> None:
    probe = PI.probe(_fake(tmp_path / "pi", empty=True))
    assert probe == Probe(None, frozenset(), f"{tmp_path / 'pi'} --version printed nothing")


def test_a_timeout_names_the_command_and_the_deadline_is_ten_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["timeout"] = kwargs["timeout"]
        seen["env"] = kwargs["env"]
        raise subprocess.TimeoutExpired(command, float(kwargs["timeout"]))  # type: ignore[arg-type]

    monkeypatch.setattr("looper.agents.runtimes.pi.subprocess.run", run)
    probe = PI.probe("pi")
    assert probe.version is None and probe.capabilities == frozenset()
    assert probe.error is not None and probe.error.startswith("pi --version did not run: TimeoutExpired:")
    assert seen["command"] == ["pi", "--version"]
    assert seen["timeout"] == 10.0
    env = seen["env"]
    assert isinstance(env, dict)
    assert not Path(env["HOME"]).exists()


def test_an_old_version_asks_for_an_upgrade(tmp_path: Path) -> None:
    executable = _fake(tmp_path / "pi", version="1.2.9")
    probe = PI.probe(executable)
    assert probe.version == "1.2.9" and probe.capabilities == frozenset()
    assert probe.error == (f"1.2.9 from {executable} --version is older than 1.3.0, the oldest pi Looper supports; "
                           "upgrade pi and probe again")


def test_an_unreadable_version_asks_for_1_3_0(tmp_path: Path) -> None:
    executable = _fake(tmp_path / "pi", version="pi dev")
    probe = PI.probe(executable)
    assert probe.version == "pi dev" and probe.capabilities == frozenset()
    assert probe.error == (f"{executable} --version printed 'pi dev', which is not a pi version Looper can read; "
                           "install pi 1.3.0 or newer and probe again")


def test_help_without_a_required_flag_names_it_and_not_a_longer_flag(tmp_path: Path) -> None:
    executable = _fake(tmp_path / "pi", help_text=_help("--no-mcp", "--session") + " --session-id")
    probe = PI.probe(executable)
    assert probe.version == "1.3.0" and probe.capabilities == frozenset()
    assert probe.error == (f"{executable} --help does not show --no-mcp, --session; "
                           "upgrade pi to 1.3.0 or newer and probe again")


def test_help_that_fails_keeps_the_version_and_asks_for_another_probe(tmp_path: Path) -> None:
    executable = _fake(tmp_path / "pi", help_text="broken", help_code=2)
    probe = PI.probe(executable)
    assert probe.version == "1.3.0" and probe.capabilities == frozenset()
    assert probe.error == f"{executable} --help failed (exit code 2): broken; probe again"


def test_a_probe_uses_a_private_offline_home_and_only_version_and_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = tmp_path / "operator"
    for path in (operator, operator / "agent", operator / "sessions", operator / "package"):
        path.mkdir()
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(operator / "agent"))
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(operator / "sessions"))
    monkeypatch.setenv("PI_PACKAGE_DIR", str(operator / "package"))
    monkeypatch.setenv("PI_OFFLINE", "0")
    monkeypatch.setenv("PI_TELEMETRY", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(operator / "xdg"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-operator")
    monkeypatch.setenv("CODEX_HOME", str(operator / "codex"))
    monkeypatch.setenv("GROK_HOME", str(operator / "grok"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(operator / "claude"))
    record = tmp_path / "record.jsonl"
    body = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"record = Path({str(record)!r})\n"
        "env = os.environ\n"
        "entry = {\n"
        "    'argv': sys.argv[1:], 'cwd': os.getcwd(), 'home': env.get('HOME'),\n"
        "    'agent': env.get('PI_CODING_AGENT_DIR'), 'session': env.get('PI_CODING_AGENT_SESSION_DIR'),\n"
        "    'offline': env.get('PI_OFFLINE'), 'telemetry': env.get('PI_TELEMETRY'),\n"
        "    'skip_version': env.get('PI_SKIP_VERSION_CHECK'),\n"
        "    'leaked': sorted(name for name in ('ANTHROPIC_API_KEY', 'PI_PACKAGE_DIR', 'CODEX_HOME', 'GROK_HOME',\n"
        "                                       'CLAUDE_CONFIG_DIR', 'XDG_CONFIG_HOME') if name in env),\n"
        "    'modes': {name: (Path(env[name]).stat().st_mode & 0o777) for name in\n"
        "              ('HOME', 'PI_CODING_AGENT_DIR', 'PI_CODING_AGENT_SESSION_DIR')},\n"
        "}\n"
        "with record.open('a', encoding='utf-8') as out:\n"
        "    out.write(json.dumps(entry) + '\\n')\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('1.3.0')\n"
        "elif sys.argv[1:] == ['--help']:\n"
        f"    print({_help()!r})\n"
        "else:\n"
        "    sys.exit(9)\n"
    )
    executable = fake_claude(tmp_path / "pi", body)
    probe = PI.probe(executable)
    assert probe == Probe("1.3.0", frozenset())
    entries = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert [entry["argv"] for entry in entries] == [["--version"], ["--help"]]
    home, agent, session = Path(entries[0]["home"]), Path(entries[0]["agent"]), Path(entries[0]["session"])
    for entry in entries:
        assert Path(entry["cwd"]).resolve() == home.resolve()
        assert (entry["home"], entry["agent"], entry["session"]) == (str(home), str(agent), str(session))
        assert (entry["offline"], entry["telemetry"], entry["skip_version"]) == ("1", "0", "1")
        assert entry["leaked"] == []
        assert entry["modes"] == {"HOME": 0o700, "PI_CODING_AGENT_DIR": 0o700,
                                  "PI_CODING_AGENT_SESSION_DIR": 0o700}
    assert agent == home / "agent"
    assert session.name == "session" and not session.is_relative_to(home)
    assert home != operator and agent != operator / "agent" and session != operator / "sessions"
    assert not home.exists() and not session.exists()
    assert {path.name for path in operator.iterdir()} == {"agent", "sessions", "package"}
    assert all(not any((operator / name).iterdir()) for name in ("agent", "sessions", "package"))


@pytest.mark.parametrize("call", [
    lambda: PI.prompt_blocks(None, "prompt"),  # type: ignore[arg-type]
    lambda: PI.isolation_args("."),
    lambda: PI.bind_provider(None, "model", "low"),  # type: ignore[arg-type]
    lambda: PI.launch_args(None),  # type: ignore[arg-type]
    lambda: PI.environment(None),  # type: ignore[arg-type]
    lambda: PI.resident_command("pi", cwd=".", prompt_file=Path("prompt"), launch=None, max_turns=3),  # type: ignore[arg-type]
    lambda: PI.resume_args("session"),
    lambda: PI.run_command("pi", cwd=".", prompt_file=Path("prompt"), launch=None, max_turns=3,  # type: ignore[arg-type]
                           session_dir=Path("sessions")),
    lambda: PI.resident_session("manager"),
    lambda: PI.parse(None),  # type: ignore[arg-type]
    lambda: PI.tool_summary("read", {}),
    lambda: PI.cost(None, None),  # type: ignore[arg-type]
    lambda: PI.budget_error(None),
    lambda: PI.instruction_files("."),
    lambda: PI.steer_command("pi", cwd=".", prompt_file=Path("prompt"), launch=None, max_turns=3,  # type: ignore[arg-type]
                             session_dir=Path("sessions")),
    lambda: PI.consult_command("pi", cwd=".", prompt_file=Path("prompt"), launch=None, session_id="s",  # type: ignore[arg-type]
                               session_dir=Path("sessions"), fork_dir=Path("fork"), max_turns=3),
])
def test_an_unimplemented_path_is_refused(call: object) -> None:
    with pytest.raises(RuntimeUnsupported, match="pi"):
        call()  # type: ignore[operator]


def test_a_turn_write_is_refused() -> None:
    with pytest.raises(RuntimeUnsupported, match="does not implement this yet"):
        asyncio.run(PI.write_turn(None, "hello"))  # type: ignore[arg-type]
