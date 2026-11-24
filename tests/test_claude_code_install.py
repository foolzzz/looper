"""The opt-in hook edits only temporary user settings and preserves unrelated data."""

import json
import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shlex
import stat
import subprocess
import sys

import pytest

from looper import cli, ops_config
from looper.agents import claude_code_entry as cc
from looper.agents import service

_REAL_LOOPER_COMMAND = service.looper_command


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path.read_bytes()


def _backups(path):
    return list(path.parent.glob(f"{path.name}.looper-backup-*"))


def _handlers(path):
    data = json.loads(path.read_text())
    return [handler for group in data.get("hooks", {}).get("SessionStart", [])
            for handler in group["hooks"] if cc._is_looper_handler(handler)]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    monkeypatch.setattr(service, "looper_command", lambda _: ["/opt/looper/bin/looper"])
    runtime = tmp_path / "rt"
    ops_config.write_private(runtime / ops_config.FILE, "[server]\nport = 18768\n")
    return runtime, tmp_path / "cc" / "settings.json"


def _run(sandbox, monkeypatch, command="install", *, answer=True, flags=()):
    parts = [] if command == "status" else ["hook"]
    args = cli.build_parser().parse_args(["claude-code", command, *parts, "--runtime-root", str(sandbox[0]), *flags])
    return cc.run(args, confirm=lambda _: answer() if callable(answer) else answer)


def test_install_shows_the_change_and_writes_only_after_yes(sandbox, monkeypatch, capsys):
    runtime, path = sandbox
    original = _write(path, {"autoMemoryEnabled": False, "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo other"}]}]}})
    path.chmod(0o640)
    desired = cc.desired_handler(runtime, None)
    assert _run(sandbox, monkeypatch, answer=False) == 1
    output = capsys.readouterr().out
    assert str(path) in output and "Adds to hooks.SessionStart:" in output
    assert desired["command"] in output
    assert any(line.startswith("+") and desired["command"] in line for line in output.splitlines())
    assert path.read_bytes() == original and not _backups(path)
    assert _run(sandbox, monkeypatch) == 0
    after = json.loads(path.read_text())
    assert after["autoMemoryEnabled"] is False
    assert after["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": "echo other"}]}, {"hooks": [desired]}]
    assert len(_backups(path)) == 1 and _backups(path)[0].read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_install_without_a_terminal_needs_yes(sandbox):
    runtime, path = sandbox
    original = _write(path, {"theme": "dark"})
    result = subprocess.run([sys.executable, "-m", "looper.cli", "claude-code", "install", "hook",
                             "--runtime-root", str(runtime)], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "confirmation needed" in result.stdout + result.stderr
    assert path.read_bytes() == original and not _backups(path)


def test_install_dry_run_writes_nothing(sandbox, monkeypatch, capsys):
    _, path = sandbox
    original = _write(path, {"theme": "dark"})
    assert _run(sandbox, monkeypatch, flags=("--dry-run",)) == 0
    assert "+++" in capsys.readouterr().out
    assert path.read_bytes() == original and not _backups(path)


def test_install_is_idempotent(sandbox, monkeypatch, capsys):
    _, path = sandbox
    _write(path, {})
    assert _run(sandbox, monkeypatch) == 0
    before = path.stat().st_mtime_ns, len(_backups(path))
    capsys.readouterr()
    assert _run(sandbox, monkeypatch) == 0
    assert "already installed" in capsys.readouterr().out
    assert (path.stat().st_mtime_ns, len(_backups(path))) == before


def test_install_replaces_a_stale_looper_entry(sandbox, monkeypatch, capsys):
    runtime, path = sandbox
    stale = {"type": "command", "command": "'/old/bin/looper' claude-code profile --hook 2>/dev/null || true"}
    _write(path, {"hooks": {"SessionStart": [{"hooks": [stale]}, {"hooks": [stale]}]}})
    assert _run(sandbox, monkeypatch) == 0
    assert "Replaces the Looper hook" in capsys.readouterr().out
    assert _handlers(path) == [cc.desired_handler(runtime, None)]


@pytest.mark.parametrize("original", [b"{not json", b"[]", b'{"hooks": []}',
    b'{"hooks": {"SessionStart": {}}}', b'{"hooks": {"SessionStart": [{"hooks": "x"}]}}',
    b'{"hooks": {"SessionStart": [false]}}', b"\xff"])
def test_install_refuses_settings_it_cannot_read(sandbox, monkeypatch, original):
    _, path = sandbox
    path.parent.mkdir()
    path.write_bytes(original)
    assert _run(sandbox, monkeypatch) == 2
    assert path.read_bytes() == original and not _backups(path)


def test_install_never_writes_managed_settings(sandbox, monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    monkeypatch.setattr(cc, "MANAGED_ROOTS", (managed,))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(managed))
    assert _run(sandbox, monkeypatch) == 4
    assert not managed.exists()


def test_install_rejects_a_symlink_into_managed_settings(sandbox, monkeypatch, tmp_path):
    _, path = sandbox
    target = tmp_path / "managed" / "settings.json"
    original = _write(target, {"theme": "dark"})
    path.parent.mkdir()
    path.symlink_to(target)
    monkeypatch.setattr(cc, "MANAGED_ROOTS", (target.parent,))
    assert _run(sandbox, monkeypatch) == 4
    assert path.is_symlink() and target.read_bytes() == original
    assert not _backups(target)


def test_install_aborts_when_the_file_changes_before_confirmation(sandbox, monkeypatch):
    _, path = sandbox
    _write(path, {})
    def concurrent_edit():
        path.write_text('{"theme": "dark"}', encoding="utf-8")
        return True
    assert _run(sandbox, monkeypatch, answer=concurrent_edit) == 1
    assert path.read_text() == '{"theme": "dark"}' and not _backups(path)


def test_install_writes_through_a_symlink(sandbox, monkeypatch, tmp_path):
    runtime, path = sandbox
    target = tmp_path / "dotfiles" / "settings.json"
    original = _write(target, {"autoMemoryEnabled": False})
    path.parent.mkdir()
    path.symlink_to(target)
    assert _run(sandbox, monkeypatch) == 0
    assert path.is_symlink() and _handlers(target) == [cc.desired_handler(runtime, None)]
    assert len(_backups(target)) == 1 and _backups(target)[0].read_bytes() == original
    assert not _backups(path)


@pytest.mark.parametrize("other", [False, True])
def test_uninstall_removes_only_the_looper_entry(sandbox, monkeypatch, other):
    runtime, path = sandbox
    expected = {"autoMemoryEnabled": False}
    data = {**expected, "hooks": {"SessionStart": [{"hooks": [cc.desired_handler(runtime, None)]}]}}
    if other:
        expected["hooks"] = {"SessionStart": [{"hooks": [{"type": "command", "command": "echo other"}]}],
                             "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo prompt"}]}]}
        data["hooks"]["SessionStart"].insert(0, expected["hooks"]["SessionStart"][0])
        data["hooks"]["UserPromptSubmit"] = expected["hooks"]["UserPromptSubmit"]
    _write(path, data)
    assert _run(sandbox, monkeypatch, "uninstall") == 0
    assert json.loads(path.read_text()) == expected


def test_uninstall_when_not_installed_changes_nothing(sandbox, monkeypatch, capsys):
    _, path = sandbox
    original = _write(path, {"theme": "dark"})
    assert _run(sandbox, monkeypatch, "uninstall") == 0
    assert "not installed" in capsys.readouterr().out
    assert path.read_bytes() == original and not _backups(path)


def test_install_is_refused_inside_an_agent_run(sandbox, monkeypatch, capsys):
    _, path = sandbox
    monkeypatch.setenv("LOOPER_AGENT_RUN", "1")
    assert _run(sandbox, monkeypatch) == 4
    assert "refused inside a Looper agent run" in "".join(capsys.readouterr())
    assert not path.exists()


def test_install_creates_private_settings_and_carries_an_absolute_config(sandbox, monkeypatch, tmp_path):
    runtime, path = sandbox
    config = tmp_path / "custom ops.toml"
    ops_config.write_private(config, "[server]\nport = 18768\n")
    assert _run(sandbox, monkeypatch, flags=("--yes", "--config", str(config))) == 0
    desired = _handlers(path)[0]
    argv = shlex.split(desired["command"])
    assert argv[argv.index("--config") + 1] == str(config)
    assert argv[argv.index("--runtime-root") + 1] == str(runtime)
    assert desired["timeout"] == 5 and "op-token" not in desired["command"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_unknown_install_parts_are_usage_errors(sandbox, monkeypatch):
    _, path = sandbox
    args = cli.build_parser().parse_args(["claude-code", "install", "--runtime-root", str(sandbox[0])])
    args.parts = ["unknown"]
    assert cc.run(args) == 2
    assert not path.exists()


@pytest.mark.parametrize("installed,missing", [(True, False), (False, False), (True, True)])
def test_status_reports_the_hook_and_the_profile(sandbox, monkeypatch, capsys, installed, missing):
    runtime, path = sandbox
    executable = runtime / ("missing-looper" if missing else "looper")
    if not missing:
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
    monkeypatch.setattr(service, "looper_command", lambda _: [str(executable)])
    data = {"hooks": {"SessionStart": [{"hooks": [cc.desired_handler(runtime, None)]}]}} if installed else {}
    _write(path, data)
    monkeypatch.setattr(cc, "fetch_profile", lambda *args, **kwargs: cc.Fetched(cc.OK, text="Name: Ada"))
    assert _run(sandbox, monkeypatch, "status") == (0 if installed and not missing else 1)
    output = capsys.readouterr().out
    assert "profile: 9 characters" in output
    if not installed:
        assert "profile hook: not installed" in output
        assert "looper claude-code install hook" in output
    elif missing:
        assert "its looper is missing" in output
    else:
        assert "profile hook: installed" in output


def test_status_does_not_claim_an_unavailable_profile_is_healthy(sandbox, monkeypatch, capsys):
    runtime, path = sandbox
    monkeypatch.setattr(service, "looper_command", lambda _: [sys.executable])
    _write(path, {"hooks": {"SessionStart": [{"hooks": [cc.desired_handler(runtime, None)]}]}})
    monkeypatch.setattr(cc, "fetch_profile", lambda *args, **kwargs: cc.Fetched(cc.EMPTY, reason="the profile is empty"))
    assert _run(sandbox, monkeypatch, "status") == 1
    assert "the profile is empty" in capsys.readouterr().out


def test_the_installed_command_runs_from_anywhere_and_never_fails(sandbox, monkeypatch, tmp_path):
    # Positive profile readback is covered against the real disposable database in the integration smoke.
    runtime, _ = sandbox
    monkeypatch.setattr(service, "looper_command", _REAL_LOOPER_COMMAND)
    ops_config.write_private(runtime / ops_config.FILE, "invalid = [")
    command = cc.desired_handler(runtime, None)["command"]
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "cc")}
    result = subprocess.run(["sh", "-c", command], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=5)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    missing = shlex.join([str(tmp_path / "no-looper"), *cc.HOOK_ARGS]) + " 2>/dev/null || true"
    result = subprocess.run(["sh", "-c", missing], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=5)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_install_aborts_when_the_settings_symlink_is_retargeted(sandbox, monkeypatch, tmp_path):
    _, path = sandbox
    first, second = (tmp_path / name / "settings.json" for name in ("first", "second"))
    original = _write(first, {"theme": "dark"})
    _write(second, {"theme": "light"})
    path.parent.mkdir()
    path.symlink_to(first)

    def retarget():
        path.unlink()
        path.symlink_to(second)
        return True

    assert _run(sandbox, monkeypatch, answer=retarget) == 1
    assert path.resolve() == second
    assert first.read_bytes() == original and json.loads(second.read_text()) == {"theme": "light"}
    assert not _backups(first) and not _backups(second)


def test_install_reports_readback_failure_and_keeps_the_backup(sandbox, monkeypatch, capsys):
    _, path = sandbox
    original = _write(path, {"theme": "dark"})
    replace = cc.os.replace

    def concurrent_replace(source, target):
        replace(source, target)
        path.write_bytes(b"not JSON")

    monkeypatch.setattr(cc.os, "replace", concurrent_replace)
    assert _run(sandbox, monkeypatch) == 1
    assert "settings readback failed" in capsys.readouterr().out
    assert len(_backups(path)) == 1 and _backups(path)[0].read_bytes() == original


@pytest.fixture
def fake_claude(tmp_path):
    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir()
    executable.write_text("#!" + sys.executable + "\n" + r'''
import json, pathlib, sys
root = pathlib.Path(__file__).parent
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\n')
state = root / 'registered'
command = sys.argv[2]
if command == 'get':
    sys.exit(0 if state.exists() else 1)
if (root / 'fail').exists():
    print('registration failed', file=sys.stderr)
    sys.exit(3)
if not (root / 'no-change').exists():
    if command == 'add-json': state.touch()
    elif command == 'remove': state.unlink(missing_ok=True)
''')
    executable.chmod(0o700)
    return executable


def _mcp_run(sandbox, executable, command="install", *, answer=True, flags=(), parts=("mcp",)):
    args = cli.build_parser().parse_args(["claude-code", command, *parts, "--runtime-root", str(sandbox[0]),
                                          "--claude", str(executable), *flags])
    return cc.run(args, confirm=lambda _: answer)


def _calls(executable):
    path = executable.parent / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_install_mcp_shows_the_exact_command_and_runs_it_after_yes(sandbox, fake_claude, capsys):
    assert _mcp_run(sandbox, fake_claude, answer=False) == 1
    output = capsys.readouterr().out
    assert "add-json --scope user looper-memory" in output
    assert json.dumps(cc.mcp_entry(sandbox[0], None), indent=2) in output
    assert _calls(fake_claude) == [["mcp", "get", "looper-memory"]]
    assert _mcp_run(sandbox, fake_claude) == 0
    calls = _calls(fake_claude)
    added = [call for call in calls if call[1] == "add-json"]
    assert len(added) == 1
    assert added[0][:5] == ["mcp", "add-json", "--scope", "user", "looper-memory"]
    entry = json.loads(added[0][5])
    assert entry == cc.mcp_entry(sandbox[0], None)
    assert "env" not in entry and "operator-token" not in added[0][5]
    assert calls[-1] == ["mcp", "get", "looper-memory"]


def test_install_mcp_when_already_registered_changes_nothing(sandbox, fake_claude, capsys):
    (fake_claude.parent / "registered").touch()
    assert _mcp_run(sandbox, fake_claude) == 0
    assert "already registered" in capsys.readouterr().out
    assert _calls(fake_claude) == [["mcp", "get", "looper-memory"]]


def test_install_mcp_without_claude_prints_the_command(sandbox, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", "")
    assert _mcp_run(sandbox, tmp_path / "missing-claude") == 1
    assert "add-json --scope user looper-memory" in capsys.readouterr().out


def test_uninstall_mcp_runs_remove_with_user_scope(sandbox, fake_claude):
    (fake_claude.parent / "registered").touch()
    assert _mcp_run(sandbox, fake_claude, "uninstall") == 0
    assert _calls(fake_claude) == [["mcp", "get", "looper-memory"],
        ["mcp", "remove", "--scope", "user", "looper-memory"], ["mcp", "get", "looper-memory"]]


@pytest.mark.parametrize("command,registered", [("install", False), ("uninstall", True)])
def test_mcp_dry_run_does_not_change_registration(sandbox, fake_claude, command, registered):
    if registered:
        (fake_claude.parent / "registered").touch()
    assert _mcp_run(sandbox, fake_claude, command, flags=("--dry-run",)) == 0
    assert _calls(fake_claude) == [["mcp", "get", "looper-memory"]]


@pytest.mark.parametrize("failure", ["fail", "no-change"])
def test_install_mcp_reports_command_failure_or_bad_readback(sandbox, fake_claude, failure):
    (fake_claude.parent / failure).touch()
    assert _mcp_run(sandbox, fake_claude) == 1


def test_mcp_without_a_terminal_needs_yes(sandbox, fake_claude, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    args = cli.build_parser().parse_args(["claude-code", "install", "mcp", "--runtime-root", str(sandbox[0]),
                                         "--claude", str(fake_claude)])
    assert cc.run(args) == 2
    assert _calls(fake_claude) == [["mcp", "get", "looper-memory"]]


def test_mcp_install_is_refused_inside_an_agent_run(sandbox, fake_claude, monkeypatch):
    monkeypatch.setenv("LOOPER_AGENT_RUN", "1")
    assert _mcp_run(sandbox, fake_claude) == 4
    assert _calls(fake_claude) == []


def test_install_without_parts_shows_and_confirms_each_part(sandbox, fake_claude):
    prompts = []
    args = cli.build_parser().parse_args(["claude-code", "install", "--runtime-root", str(sandbox[0]),
                                         "--claude", str(fake_claude)])
    assert cc.run(args, confirm=lambda prompt: prompts.append(prompt) or True) == 0
    assert len(prompts) == 2
    assert "settings.json" in prompts[0] and "MCP" in prompts[1]


@contextlib.contextmanager
def _retrieval_server(runtime, *, readable=True):
    from looper.agents import operator_auth
    token = operator_auth.ensure_token(runtime)
    state = {"readable": readable, "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer " + token
            state["paths"].append(self.path)
            result = ({"configured": True, "manager_read": state["readable"], "path": "notes", "problem": None}
                      if self.path.endswith("knowledge_vault/get") else {"enabled": False})
            body = json.dumps({"status": "ok", "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ops_config.write_private(runtime / ops_config.FILE, f"[server]\nport = {server.server_port}\n")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_status_reports_retrieval(sandbox, fake_claude, capsys):
    (fake_claude.parent / "registered").touch()
    with _retrieval_server(sandbox[0]) as state:
        assert _mcp_run(sandbox, fake_claude, "status", parts=()) == 0
        output = capsys.readouterr().out
        assert "retrieval MCP server (looper-memory): registered" in output
        assert "knowledge vault for the Manager: readable (root notes)" in output
        assert "message leads: off" in output
        assert state["paths"] == ["/api/v1/knowledge_vault/get", "/api/v1/leads/settings_get"]
        state["readable"] = False
        assert _mcp_run(sandbox, fake_claude, "status", parts=()) == 1


def test_status_reports_registered_mcp_without_looper_as_unhealthy(sandbox, fake_claude, capsys):
    (fake_claude.parent / "registered").touch()
    assert _mcp_run(sandbox, fake_claude, "status", parts=()) == 1
    assert "knowledge vault for the Manager: off:" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["fail", "no-change"])
def test_uninstall_mcp_reports_command_failure_or_bad_readback(sandbox, fake_claude, failure):
    (fake_claude.parent / "registered").touch()
    (fake_claude.parent / failure).touch()
    assert _mcp_run(sandbox, fake_claude, "uninstall") == 1


def test_uninstall_mcp_when_not_registered_changes_nothing(sandbox, fake_claude):
    assert _mcp_run(sandbox, fake_claude, "uninstall") == 0
    assert _calls(fake_claude) == [["mcp", "get", "looper-memory"]]


def test_mcp_command_timeout_is_reported(sandbox, fake_claude, monkeypatch, capsys):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30)

    monkeypatch.setattr(cc.subprocess, "run", timeout)
    assert _mcp_run(sandbox, fake_claude) == 1
    assert "timed out" in capsys.readouterr().out


def test_mcp_entry_carries_absolute_config_without_credentials(sandbox, tmp_path):
    config = tmp_path / "settings with spaces.toml"
    entry = cc.mcp_entry(sandbox[0], str(config))
    assert entry["args"] == ["claude-code", "mcp", "--runtime-root", str(sandbox[0]), "--config", str(config)]
    assert set(entry) == {"type", "command", "args"}


def test_explicit_missing_claude_never_falls_back_to_another_install(sandbox, fake_claude, monkeypatch):
    monkeypatch.setenv("PATH", str(fake_claude.parent))
    args = cli.build_parser().parse_args(["claude-code", "install", "mcp", "--runtime-root", str(sandbox[0]),
                                          "--claude", str(fake_claude.parent / "missing"), "--yes"])
    assert cc.run(args) == 1
    assert not (fake_claude.parent / "calls.jsonl").exists()
