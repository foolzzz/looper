"""One-shot service checks use the listening address and bot-only notification transports."""

from __future__ import annotations

import json
import socket
import stat
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fake_claude import fake_claude

from looper import cli
from looper.agents import probe
from looper.ops_config import OpsConfig

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
HEALTH = {"status": "ok", "scheduler": {"running": True, "last_ok_tick_age_seconds": 2}}


@contextmanager
def serving(body=HEALTH, code=200, host="127.0.0.1"):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/api/v1/system/health"
            assert self.rfile.read(int(self.headers["Content-Length"])) == b"{}"
            self.server.authorization = self.headers.get("Authorization")
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

        def log_message(self, *args):
            pass

    class Server(HTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    with Server((host, 0), Handler) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05))
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize(("state", "verdict", "at", "action", "first_bad"), [
    (probe.State(), probe.Verdict.OK, NOW, probe.Action.NONE, None),
    (probe.State(last_sample_at=NOW, last_ok_at=NOW), probe.Verdict.OK, NOW + MINUTE, probe.Action.NONE, None),
    (probe.State(first_bad_at=NOW, last_sample_at=NOW + 3 * MINUTE), probe.Verdict.NO_ANSWER,
     NOW + 4 * MINUTE, probe.Action.NONE, NOW),
    (probe.State(first_bad_at=NOW, last_sample_at=NOW + 4 * MINUTE), probe.Verdict.NO_ANSWER,
     NOW + 5 * MINUTE, probe.Action.ALERT, NOW),
    (probe.State(first_bad_at=NOW, last_sample_at=NOW + 5 * MINUTE, last_send_error="missing CLI"),
     probe.Verdict.NO_ANSWER, NOW + 6 * MINUTE, probe.Action.ALERT, NOW),
    (probe.State(first_bad_at=NOW, last_sample_at=NOW + 5 * MINUTE, alert_started_at=NOW + 5 * MINUTE),
     probe.Verdict.NO_ANSWER, NOW + 6 * MINUTE, probe.Action.NONE, NOW),
    (probe.State(first_bad_at=NOW, alert_started_at=NOW), probe.Verdict.OK,
     NOW + MINUTE, probe.Action.RECOVER, None),
    (probe.State(first_bad_at=NOW, last_sample_at=NOW + MINUTE), probe.Verdict.NO_ANSWER,
     NOW + timedelta(hours=2), probe.Action.NONE, NOW + timedelta(hours=2)),
    (probe.State(first_bad_at=NOW, last_sample_at=NOW + MINUTE), probe.Verdict.UNAUTHORIZED,
     NOW - MINUTE, probe.Action.NONE, NOW - MINUTE),
])
def test_step(state, verdict, at, action, first_bad):
    before = asdict(state)
    updated, actual = probe.step(state, verdict, at, 5 * MINUTE)
    assert asdict(state) == before  # stepping never mutates persisted input
    assert actual == action
    assert updated.first_bad_at == first_bad
    assert updated.last_sample_at == at and updated.last_verdict == verdict.value
    assert updated.alert_started_at == state.alert_started_at
    if verdict == probe.Verdict.OK:
        assert updated.last_ok_at == at


@pytest.mark.parametrize(("body", "code", "verdict"), [
    (HEALTH, 200, probe.Verdict.OK),
    ({"status": "ok", "result": HEALTH}, 200, probe.Verdict.OK),
    ({"status": "ok", "scheduler": {"running": False}}, 200, probe.Verdict.SCHEDULER_STOPPED),
    ({"status": "ok", "scheduler": {"running": True, "last_ok_tick_age_seconds": 300}},
     200, probe.Verdict.TICK_STALE),
    ({"status": "ok", "scheduler": {"running": True, "last_ok_tick_age_seconds": None}},
     200, probe.Verdict.TICK_STALE),
    ({"status": "ok", "scheduler": {"running": True, "last_ok_tick_age_seconds": 120}},
     200, probe.Verdict.OK),
    ({}, 401, probe.Verdict.UNAUTHORIZED),
    ({}, 500, probe.Verdict.NO_ANSWER),
    (b"not json", 200, probe.Verdict.NO_ANSWER),
    ({"status": "error"}, 200, probe.Verdict.NO_ANSWER),
    ([], 200, probe.Verdict.NO_ANSWER),
    ({"status": "ok", "scheduler": []}, 200, probe.Verdict.NO_ANSWER),
    ({"status": "ok", "scheduler": {"running": True, "last_ok_tick_age_seconds": "bad"}},
     200, probe.Verdict.NO_ANSWER),
])
def test_sample(body, code, verdict, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with serving(body, code) as server:
        actual, reason = probe.sample(server.server_port, "operator-test")
        assert actual == verdict
        assert reason
        assert server.authorization == "Bearer operator-test"


def test_sample_connection_refused():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    verdict, reason = probe.sample(port, None)
    assert verdict == probe.Verdict.NO_ANSWER
    assert "refused" in reason.lower()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_probe_run_samples_the_configured_listening_address(tmp_path, host):
    with serving(host=host) as server:
        assert probe.run(ops(tmp_path, host=host, port=server.server_port), tmp_path) == 0
    state = json.loads((tmp_path / ".local/probe/state.json").read_text())
    assert state["last_verdict"] == "ok"


@pytest.mark.parametrize(("host", "address"), [("", "127.0.0.1"), ("localhost", "127.0.0.1"),
                                             ("0.0.0.0", "127.0.0.1"), ("::", "127.0.0.1"),
                                             ("::1", "[::1]"), ("192.0.2.7", "192.0.2.7"),
                                             ("2001:db8::7", "[2001:db8::7]")])
def test_probe_addresses_wildcards_and_specific_hosts(monkeypatch, host, address):
    import io
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == f"http://{address}:12345/api/v1/system/health"
            return io.BytesIO(json.dumps(HEALTH).encode())
    monkeypatch.setattr(probe, "build_opener", lambda *args: Opener())
    assert probe.sample(12345, None, host) == (probe.Verdict.OK, "ok")


def test_sample_timeout(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            assert timeout == probe.HTTP_TIMEOUT_SECONDS
            raise TimeoutError("timed out")
    monkeypatch.setattr(probe, "build_opener", lambda *args: Opener())
    assert probe.sample(12345, None) == (probe.Verdict.NO_ANSWER, "timed out")


def ops(root, **kwargs):
    return OpsConfig(runtime_root=root, path=root / "ops/server.toml", probe_notify_channel="lark",
                     probe_notify_chat="ou_abcdefgh12", **kwargs)


@pytest.mark.parametrize(("target", "flag"), [("ou_abcdefgh12", "--user-id"), ("oc_abcdefgh12", "--chat-id")])
def test_lark_send_is_bot_only(tmp_path, lark_cli_stand_in, target, flag):
    config = replace(ops(tmp_path), probe_notify_chat=target)
    assert probe.send(config, tmp_path, "test message", "looper-probe-123") is None
    argv = json.loads(lark_cli_stand_in.read_text().splitlines()[0])
    assert argv[argv.index("--as") + 1] == "bot"
    assert argv[argv.index(flag) + 1] == target
    assert argv[argv.index("--idempotency-key") + 1] == "looper-probe-123"
    assert "user" not in argv


def test_lark_missing_cli_and_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert "lark-cli" in probe.send(ops(tmp_path), tmp_path, "test", "key")
    from subprocess import CompletedProcess
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs:
                        CompletedProcess(args, 1, b"", b"first line\nlast line\n"))
    assert probe.send(ops(tmp_path), tmp_path, "test", "key") == "last line"


def test_lark_send_keeps_profile_environment_in_the_child(tmp_path, monkeypatch):
    log = tmp_path / "child.json"
    fake_claude(tmp_path / "lark-cli", f"import os, json, sys\n"
                f"open({str(log)!r}, 'w').write(json.dumps({{'env': dict(os.environ), 'argv': sys.argv[1:]}}))\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("LARK_PROFILE", "alert-bot")
    monkeypatch.setenv("LOOPER_DATABASE_URL", "must-not-leak")
    assert probe.send(ops(tmp_path), tmp_path, "test", "key") is None
    child = json.loads(log.read_text())
    assert child["env"]["LARK_PROFILE"] == "alert-bot"
    assert "LOOPER_DATABASE_URL" not in child["env"]
    assert child["argv"][child["argv"].index("--as") + 1] == "bot"


@pytest.mark.parametrize("cached", [True, False])
def test_bot_falls_back_to_credentials_cache(tmp_path, monkeypatch, cached):
    from looper.agents.channels import lark_api

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = replace(ops(tmp_path), probe_notify_channel="lark_bot",
                     database_url=f"postgresql://postgres:postgres@127.0.0.1:{port}/unused")
    cache = tmp_path / ".local/probe/lark_bot.json"
    if cached:
        cache.parent.mkdir(parents=True, mode=0o700)
        cache.write_text(json.dumps({"app_id": "test-app", "app_secret": "test-secret", "domain": "feishu"}))
        cache.chmod(0o600)
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            assert json.loads(request.content) == {"app_id": "test-app", "app_secret": "test-secret"}
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-test", "expire": 7200})
        assert request.url.path == "/open-apis/im/v1/messages"
        assert request.headers["Authorization"] == "Bearer tenant-test"
        assert request.url.params["receive_id_type"] == "open_id"
        assert json.loads(request.content)["uuid"] == "looper-probe-123"
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_test"}})

    api_type = lark_api.LarkApi
    clients = []
    class Api(api_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, http=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
            self._owns_http = True
            clients.append(self._http)
    monkeypatch.setattr(lark_api, "LarkApi", Api)
    error = probe.send(config, tmp_path, "test", "looper-probe-123")
    if cached:
        assert error is None
        assert len(calls) == 2
        assert clients[0].is_closed
    else:
        assert error == "no lark_bot credentials: the database is unreachable and no cached copy exists"
        assert not calls


@pytest.mark.parametrize("close_fails", [False, True])
def test_bot_credentials_cache_is_private_and_survives_connection_close_failure(tmp_path, monkeypatch, close_fails):
    import asyncio
    import asyncpg

    cache = tmp_path / ".local/probe/lark_bot.json"
    credentials = {"app_id": "test-app", "app_secret": "test-secret", "domain": "lark"}
    if close_fails:
        cache.parent.mkdir(parents=True, mode=0o700)
        cache.write_text(json.dumps(credentials))
        cache.chmod(0o600)
    terminated = []
    class Connection:
        async def fetch(self, query):
            if close_fails:
                raise ConnectionError("database lost")
            assert "lark_bot" in query
            return [{"channel_id": "channel-lark_bot-2", "config": json.dumps({"app_id": "test-app", "domain": "lark"}),
                     "bot_token": "test-secret"}]

        async def close(self, timeout):
            if close_fails:
                raise TimeoutError()

        def terminate(self):
            terminated.append(True)

    async def connect(*args, **kwargs):
        assert kwargs["timeout"] == 5
        return Connection()

    monkeypatch.setattr(asyncpg, "connect", connect)
    assert asyncio.run(probe._bot_credentials(ops(tmp_path), tmp_path)) == credentials
    assert json.loads(cache.read_text()) == credentials
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache.parent.stat().st_mode) == 0o700
    assert bool(terminated) == close_fails


@pytest.mark.parametrize(("profile", "rows", "selected"), [
    ("", [("channel-lark_bot-2", "")], "channel-lark_bot-2"),
    ("ops", [("channel-lark_bot", "other"), ("channel-lark_bot-2", "ops")], "channel-lark_bot-2"),
    ("channel-lark_bot-2", [("channel-lark_bot", ""), ("channel-lark_bot-2", "")], "channel-lark_bot-2"),
    ("", [("channel-lark_bot", ""), ("channel-lark_bot-2", "")], None),
    ("missing", [("channel-lark_bot-2", "ops")], None),
])
def test_bot_credentials_select_the_configured_channel(tmp_path, monkeypatch, profile, rows, selected):
    import asyncio
    import asyncpg
    class Connection:
        async def fetch(self, query):
            assert "lark_bot" in query
            return [{"channel_id": name, "config": json.dumps({"profile": value, "app_id": name}),
                     "bot_token": "secret"} for name, value in rows]
        async def close(self, timeout):
            pass
    async def connect(*args, **kwargs):
        return Connection()
    monkeypatch.setattr(asyncpg, "connect", connect)
    credentials = asyncio.run(probe._bot_credentials(ops(tmp_path, probe_lark_profile=profile), tmp_path))
    assert (credentials["app_id"] if credentials else None) == selected


def test_bot_cache_is_not_reused_for_another_profile(tmp_path, monkeypatch):
    import asyncio
    import asyncpg
    path = tmp_path / ".local/probe/lark_bot.json"
    probe._write_json(path, {"app_id": "old-app", "app_secret": "old-secret", "domain": "feishu", "profile": "old"})
    async def offline(*args, **kwargs):
        raise ConnectionError("database stopped")
    monkeypatch.setattr(asyncpg, "connect", offline)
    assert asyncio.run(probe._bot_credentials(ops(tmp_path, probe_lark_profile="new"), tmp_path)) is None
    assert asyncio.run(probe._bot_credentials(ops(tmp_path, probe_lark_profile="old"), tmp_path))["app_id"] == "old-app"


def test_healthy_sample_primes_and_refreshes_bot_cache_before_database_outage(tmp_path, monkeypatch):
    import asyncpg
    from looper.agents.channels import lark_api
    calls, sends = [], []
    online = True
    secret = "first-secret"
    class Connection:
        async def fetch(self, query):
            return [{"channel_id": "channel-lark_bot-7", "config": {"app_id": "test-app"}, "bot_token": secret}]
        async def close(self, timeout):
            pass
    async def connect(*args, **kwargs):
        calls.append(online)
        if not online:
            raise ConnectionError("database stopped")
        return Connection()
    class Api:
        def __init__(self, app_id, app_secret, **kwargs):
            assert app_id == "test-app" and app_secret == "rotated-secret"
        async def send(self, **kwargs):
            sends.append(kwargs)
        async def aclose(self):
            pass
    class Clock(datetime):
        current = NOW
        @classmethod
        def now(cls, tz):
            return cls.current
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(lark_api, "LarkApi", Api)
    monkeypatch.setattr(probe, "datetime", Clock)
    with serving() as server:
        config = replace(ops(tmp_path, port=server.server_port, probe_after_minutes=1), probe_notify_channel="lark_bot")
        assert probe.run(config, tmp_path) == 0
        cache = tmp_path / ".local/probe/lark_bot.json"
        assert json.loads(cache.read_text())["app_secret"] == "first-secret"
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600
        secret = "rotated-secret"
        Clock.current += MINUTE
        assert probe.run(config, tmp_path) == 0
        assert json.loads(cache.read_text())["app_secret"] == secret
    assert calls == [True, True] and not sends
    online = False
    for _ in range(2):
        Clock.current += MINUTE
        assert probe.run(config, tmp_path) == 0
    assert len(sends) == 1 and "告警：" in sends[0]["text"]
    state = json.loads((tmp_path / ".local/probe/state.json").read_text())
    assert state["alert_started_at"] and state["last_send_error"] is None


def test_run_alerts_once_then_recovers(tmp_path, lark_cli_stand_in, monkeypatch, capsys):
    class Clock(datetime):
        current = NOW
        @classmethod
        def now(cls, tz):
            return cls.current
    monkeypatch.setattr(probe, "datetime", Clock)
    (tmp_path / "operator-token").write_text("operator-test")
    # A reachable but failing HTTP service exercises sampling as well as persistence and the CLI transport.
    with serving({}, 500) as server:
        config = ops(tmp_path, port=server.server_port)
        for minute in range(6):
            Clock.current = NOW + minute * MINUTE
            assert probe.run(config, tmp_path) == 0
    assert len(lark_cli_stand_in.read_text().splitlines()) == 1
    with serving({"status": "ok", "result": HEALTH}) as server:
        Clock.current += MINUTE
        assert probe.run(replace(config, port=server.server_port), tmp_path) == 0
    argv = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
    assert len(argv) == 2
    assert "告警：" in argv[0][argv[0].index("--text") + 1]
    assert "恢复：" in argv[1][argv[1].index("--text") + 1]
    state_file = tmp_path / ".local/probe/state.json"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_file.parent.stat().st_mode) == 0o700
    state = json.loads(state_file.read_text())
    assert state["alert_started_at"] is None and state["last_verdict"] == "ok"
    assert len(capsys.readouterr().err.splitlines()) == 3


def test_run_persists_send_failure_and_retries(tmp_path, monkeypatch):
    directory = tmp_path / ".local/probe"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "state.json").write_text(json.dumps({
        "last_sample_at": (NOW - MINUTE).isoformat(), "first_bad_at": (NOW - 5 * MINUTE).isoformat()}))
    monkeypatch.setattr(probe, "datetime", type("Clock", (), {"now": staticmethod(lambda tz: NOW),
                                                            "fromisoformat": datetime.fromisoformat}))
    monkeypatch.setattr(probe, "sample", lambda *args: (probe.Verdict.UNAUTHORIZED, "unauthorized"))
    monkeypatch.setenv("PATH", str(tmp_path))
    assert probe.run(ops(tmp_path), tmp_path) == 0
    state = json.loads((directory / "state.json").read_text())
    assert state["alert_started_at"] is None
    assert "lark-cli" in state["last_send_error"]
    sends = []
    monkeypatch.setattr(probe, "send", lambda *args: sends.append(args) or None)
    assert probe.run(ops(tmp_path), tmp_path) == 0
    assert len(sends) == 1
    assert json.loads((directory / "state.json").read_text())["alert_started_at"] is not None


def test_run_retries_recovery_with_a_stable_duration_and_key(tmp_path, monkeypatch):
    directory = tmp_path / ".local/probe"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "state.json").write_text(json.dumps({
        "last_sample_at": (NOW - MINUTE).isoformat(), "first_bad_at": (NOW - 10 * MINUTE).isoformat(),
        "alert_started_at": (NOW - 5 * MINUTE).isoformat(), "last_verdict": "no_answer"}))
    class Clock(datetime):
        current = NOW

        @classmethod
        def now(cls, tz):
            return cls.current

    monkeypatch.setattr(probe, "datetime", Clock)
    monkeypatch.setattr(probe, "sample", lambda *args: (probe.Verdict.OK, "ok"))
    sends = []

    def send(*args):
        sends.append(args)
        return "temporarily unavailable" if len(sends) == 1 else None

    monkeypatch.setattr(probe, "send", send)
    assert probe.run(ops(tmp_path), tmp_path) == 0
    failed = json.loads((directory / "state.json").read_text())
    assert failed["alert_started_at"] == (NOW - 5 * MINUTE).isoformat()
    assert failed["first_bad_at"] is None
    assert failed["last_send_error"] == "temporarily unavailable"
    Clock.current += MINUTE
    assert probe.run(ops(tmp_path), tmp_path) == 0
    assert "中断约 5 分钟" in sends[0][2]
    assert "中断约 6 分钟" in sends[1][2]
    assert sends[0][3] == sends[1][3] == f"looper-probe-recovered-{int((NOW - 5 * MINUTE).timestamp())}"
    recovered = json.loads((directory / "state.json").read_text())
    assert recovered["alert_started_at"] is None and recovered["last_send_error"] is None


@pytest.mark.parametrize("contents", ["{bad json", "[]", '{"last_sample_at": "bad"}'])
def test_run_discards_corrupt_state(tmp_path, monkeypatch, contents):
    directory = tmp_path / ".local/probe"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "state.json").write_text(contents)
    monkeypatch.setattr(probe, "sample", lambda *args: (probe.Verdict.NO_ANSWER, "connection refused"))
    assert probe.run(ops(tmp_path), tmp_path) == 0
    assert json.loads((directory / "state.json").read_text())["first_bad_at"] is not None


def test_service_probe_cli_without_configuration(tmp_path, capsys):
    assert cli.main(["service", "probe", "--runtime-root", str(tmp_path)]) == 0
    assert "probe" in capsys.readouterr().out.lower()
    assert not (tmp_path / ".local/probe").exists()


def test_service_probe_cli_reads_explicit_config_and_runtime_root(tmp_path):
    from looper.ops_config import write_private

    runtime = tmp_path / "runtime"
    with serving({"status": "ok", "result": HEALTH}) as server:
        config = tmp_path / "deployment/server.toml"
        write_private(config, f'[server]\nport = {server.server_port}\n'
                      '[probe]\nnotify_channel = "lark"\nnotify_chat = "oc_testchat"\n')
        assert cli.main(["service", "probe", "--config", str(config), "--runtime-root", str(runtime)]) == 0
    state = json.loads((runtime / ".local/probe/state.json").read_text())
    assert state["last_verdict"] == "ok" and state["last_ok_at"] is not None
