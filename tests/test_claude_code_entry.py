"""SessionStart uses the existing local resident-profile reader, with a silent child boundary."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper import ops_config
from tests.db_support import DSN, fresh_database_without_recording


PROFILE = "Name: Ada\nStanding rules:\n- 22:00 后不发主动消息"


def _environment(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ, HOME=str(tmp_path), CLAUDE_CONFIG_DIR=str(tmp_path / "cc"))
    for name in tuple(env):
        if name.startswith(("LOOPER_", "LOOPX_")) and not name.startswith("LOOPER_TEST_"):
            env.pop(name)
    return env


def _runtime(tmp_path: Path, database_url: str) -> Path:
    root = tmp_path / "runtime"
    settings = ops_config.OpsConfig(runtime_root=root, path=root / ops_config.FILE, database_url=database_url)
    ops_config.write_private(settings.path, ops_config.render(settings))
    return root


def _profile(tmp_path: Path, root: Path, *, hook: bool = True,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "looper.cli", "claude-code", "profile",
         *(["--hook"] if hook else []), "--runtime-root", str(root)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=4,
        env=env if env is not None else _environment(tmp_path),
    )


def test_profile_hook_is_silent_when_ops_settings_are_unusable(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    ops_config.write_private(root / ops_config.FILE, "invalid = [")
    result = _profile(tmp_path, root)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_profile_hook_is_silent_when_the_database_is_unreachable(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = _runtime(tmp_path, f"postgresql://synthetic@127.0.0.1:{port}/unavailable")
    result = _profile(tmp_path, root)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_profile_hook_gives_up_at_the_deadline(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        root = _runtime(tmp_path, f"postgresql://synthetic@127.0.0.1:{listener.getsockname()[1]}/unavailable")
        before = time.monotonic()
        result = _profile(tmp_path, root)
        elapsed = time.monotonic() - before
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert elapsed < 3.5
        # Positive control: the profile reader reached the deliberately stalled PostgreSQL handshake.
        listener.settimeout(0.3)
        connection, _ = listener.accept()
        connection.close()


@pytest.mark.parametrize("role", ["manager", "planner", "worker", "reviewer", "processor", "consult"])
def test_profile_hook_is_silent_inside_an_agent_run(tmp_path: Path, role: str) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        root = _runtime(tmp_path, f"postgresql://synthetic@127.0.0.1:{listener.getsockname()[1]}/unavailable")
        env = _environment(tmp_path)
        env["LOOPER_AGENT_RUN"] = role
        result = _profile(tmp_path, root, env=env)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        listener.settimeout(0.05)
        with pytest.raises(TimeoutError):
            listener.accept()


def test_profile_hook_imports_no_database_driver(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    ops_config.write_private(root / ops_config.FILE, "invalid = [")
    script = (
        "import sys\nfrom looper import cli\n"
        f"cli.main(['claude-code', 'profile', '--hook', '--runtime-root', {str(root)!r}])\n"
        "print('asyncpg' in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=4, env=_environment(tmp_path))
    assert (result.returncode, result.stdout, result.stderr) == (0, "False\n", "")


def test_profile_show_is_also_silent_inside_an_agent_run(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        root = _runtime(tmp_path, f"postgresql://synthetic@127.0.0.1:{listener.getsockname()[1]}/unavailable")
        env = _environment(tmp_path)
        env["LOOPER_AGENT_RUN"] = "worker"
        result = subprocess.run(
            [sys.executable, "-m", "looper.cli", "profile", "show", "--runtime-root", str(root)],
            capture_output=True, text=True, timeout=4, env=env,
        )
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        listener.settimeout(0.05)
        with pytest.raises(TimeoutError):
            listener.accept()


def test_profile_hook_prints_the_shared_reader_text_as_session_context(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from looper.agents import claude_code_entry, profile_commands

    calls = []

    def read(args):
        calls.append((args.runtime_root, args.config))
        return PROFILE

    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    monkeypatch.setattr(profile_commands, "read_text", read)
    args = SimpleNamespace(claude_code_command="profile", hook=True,
                           runtime_root=str(tmp_path / "runtime"), config=None)
    assert claude_code_entry.run(args) == 0
    stdout, stderr = capsys.readouterr()
    assert stderr == ""
    assert len(stdout.splitlines()) == 1
    assert json.loads(stdout) == {"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": PROFILE}}
    assert calls == [(args.runtime_root, None)]


def test_profile_without_hook_prints_the_same_shared_text(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from looper.agents import claude_code_entry, profile_commands

    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    monkeypatch.setattr(profile_commands, "read_text", lambda _: PROFILE)
    args = SimpleNamespace(claude_code_command="profile", hook=False, runtime_root=None, config=None)
    assert claude_code_entry.run(args) == 0
    assert capsys.readouterr() == (PROFILE + "\n", "")


def test_profile_without_hook_explains_an_empty_shared_profile(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from looper.agents import claude_code_entry, profile_commands

    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    monkeypatch.setattr(profile_commands, "read_text", lambda _: "")
    args = SimpleNamespace(claude_code_command="profile", hook=False, runtime_root=None, config=None)
    assert claude_code_entry.run(args) == claude_code_entry.EMPTY
    stdout, stderr = capsys.readouterr()
    assert stdout == ""
    assert "knowledge_vault.profile_source" in stderr


def test_profile_context_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import claude_code_entry, profile_commands

    monkeypatch.setattr(profile_commands, "read_text", lambda _: PROFILE + "\n" + "规" * 5000)
    fetched = claude_code_entry.fetch_profile(None, None, environ={})
    assert fetched.code == claude_code_entry.OK
    assert fetched.text.startswith(PROFILE)
    assert fetched.text.endswith("…")
    assert len(fetched.text) <= claude_code_entry.MAX_CONTEXT_CHARS


@pytest.mark.parametrize("failure", [RuntimeError("failed"), SystemExit(9), KeyboardInterrupt()])
def test_profile_hook_discards_all_output_on_unexpected_failure(
        failure: BaseException, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    from looper.agents import claude_code_entry

    def broken(_):
        print("partial profile must not leak")
        print("unexpected error", file=sys.stderr)
        raise failure

    monkeypatch.setattr(claude_code_entry, "profile_command", broken)
    args = SimpleNamespace(claude_code_command="profile", hook=True, runtime_root=None, config=None)
    assert claude_code_entry.run(args) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_real_server_profile_show_and_installed_hook_share_only_marked_entries(tmp_path: Path) -> None:
    from evals.prompt_regression import runner
    from fake_claude import fake_claude
    from manager_mcp_stand_in import _BODY

    runtime, bin_dir, vault = (tmp_path / name for name in ("runtime", "bin", "vault"))
    bin_dir.mkdir()
    (vault / "记忆").mkdir(parents=True)
    (vault / "记忆" / "画像.md").write_text(
        "## 名字\nAda Example\n\n"
        "- Working on Atlas and Beacon [kind:: fact]\n"
        "- No proactive messages from 22:00 to 08:00 [kind:: decision] [share:: hook]\n", encoding="utf-8")
    discovery = "import sys\nif '--mcp-config' not in sys.argv:\n    print('2.1.0')\n    sys.exit(0)\n"
    fake_claude(bin_dir / "claude", discovery + "TURNS = []\n" + _BODY)
    env = _environment(tmp_path)
    settings = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"autoMemoryEnabled": false}\n', encoding="utf-8")

    def command(*parts):
        return subprocess.run(
            [sys.executable, "-m", "looper.cli", *parts, "--runtime-root", str(runtime)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=5, env=env,
        )

    with fresh_database_without_recording(DSN) as (database_url, _):
        with runner.running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir,
                                   log_path=tmp_path / "serve.log", home=tmp_path / "server-home") as api:
            answer = api.post("/api/v1/knowledge_vault/update", {
                "path": str(vault), "profile_source": "记忆/画像.md"})
            assert answer["status"] == "ok"
            original = command("profile", "show")
            assert original.returncode == 0 and original.stderr == b""
            assert original.stdout == "Standing rules:\n- No proactive messages from 22:00 to 08:00".encode()
            resident = api.post("/api/v1/profile/get", {})["result"]["text"]
            assert resident.startswith("Name: Ada Example\n") and "Atlas" in resident and "22:00" in resident
            hook = command("claude-code", "profile", "--hook")
            assert hook.returncode == 0 and hook.stderr == b""
            expected = {"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": original.stdout.decode()}}
            assert json.loads(hook.stdout) == expected
            installed = command("claude-code", "install", "hook", "--yes")
            assert installed.returncode == 0, installed.stdout + installed.stderr
            data = json.loads(settings.read_text())
            assert data["autoMemoryEnabled"] is False
            handlers = data["hooks"]["SessionStart"]
            assert len(handlers) == 1 and len(handlers[0]["hooks"]) == 1
            installed_command = handlers[0]["hooks"][0]["command"]
            shell = subprocess.run(
                ["sh", "-c", installed_command], cwd=tmp_path,
                env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
                capture_output=True, timeout=5,
            )
            assert shell.returncode == 0 and shell.stderr == b""
            assert json.loads(shell.stdout) == expected
            status = command("claude-code", "status")
            assert status.returncode == 0, status.stdout + status.stderr
            assert b"profile hook: installed" in status.stdout
            removed = command("claude-code", "uninstall", "hook", "--yes")
            assert removed.returncode == 0, removed.stdout + removed.stderr
            assert json.loads(settings.read_text()) == {"autoMemoryEnabled": False}
