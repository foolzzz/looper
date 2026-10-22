"""``loopercli`` against a fake Looper: a ``ThreadingHTTPServer`` on loopback that serves the generated OpenAPI document
and records the calls. No database; the CLI itself imports nothing of Looper but its version."""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.loopercli import Api, Unreachable

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "lpc_good"
Request = tuple[str, str, dict[str, str], Any]  # method, path, headers (lower-case names), JSON body


def _send(handler: BaseHTTPRequestHandler, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
    raw = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(raw)


class Server:
    """A server on 127.0.0.1 that records every request and answers with ``respond(handler, request)``."""

    def __init__(self, respond: Callable[[BaseHTTPRequestHandler, Request], None]) -> None:
        self.requests: list[Request] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                request = (self.command, self.path, {k.lower(): v for k, v in self.headers.items()},
                           json.loads(raw) if raw else None)
                server.requests.append(request)
                respond(self, request)

            do_GET = do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


class FakeLooper(Server):
    """The ``/api/v1`` of a Looper: the document, ``whoami`` for ``lpc_good`` and the answers a test queues."""

    def __init__(self) -> None:
        self.answers: list[tuple[int, Any]] = []
        self.document_status = 200
        self.redirects: dict[str, tuple[int, str]] = {}  # path prefix -> (status, location)
        super().__init__(self._respond)

    def _respond(self, handler: BaseHTTPRequestHandler, request: Request) -> None:
        method, path, headers, _ = request
        for prefix, (status, location) in self.redirects.items():
            if path.startswith(prefix):
                _send(handler, status, {}, {"Location": location + path})
                return
        if path == "/api/v1/openapi.json":
            _send(handler, self.document_status,
                  api_v1.document() if self.document_status == 200 else {"status": "error"})
        elif headers.get("authorization") != f"Bearer {TOKEN}":
            _send(handler, 401, {"status": "error", "error": {"code": "unauthorized", "message": "no"}})
        elif path == "/api/v1/whoami":
            _send(handler, 200, api_v1.whoami(api_v1.Principal("client", "c-1", "ci")))
        else:
            status, payload = self.answers.pop(0) if self.answers else (200, {"status": "ok", "result": {}})
            _send(handler, status, payload)

    def calls(self) -> list[Request]:
        """The recorded POSTs of operations and ``confirm``."""
        return [r for r in self.requests if r[0] == "POST"]


@pytest.fixture
def looper() -> Iterator[FakeLooper]:
    server = FakeLooper()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LOOPER_SESSION", "LOOPER_CLIENT_FILE", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)


def write_client_file(path: Path, url: str, token: str = TOKEN, mode: int = 0o600) -> Path:
    path.write_text(json.dumps({"url": url, "token": token}))
    path.chmod(mode)
    return path


@pytest.fixture
def client(tmp_path: Path, looper: FakeLooper) -> Path:
    return write_client_file(tmp_path / "client.json", looper.url)


class Run:
    def __init__(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, client: Path) -> None:
        self.capsys, self.monkeypatch, self.client = capsys, monkeypatch, client

    def __call__(self, *argv: str, stdin: str | None = None, client: bool = True) -> tuple[int, str, str]:
        if stdin is not None:
            self.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        args = ["--client-file", str(self.client), *argv] if client else list(argv)
        code = loopercli.main(args)
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, client: Path) -> Run:
    return Run(capsys, monkeypatch, client)


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- imports ---


def test_the_module_imports_only_the_standard_library_and_the_version() -> None:
    code = "import sys, looper.loopercli; print('\\n'.join(sorted(sys.modules)))"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (str(REPO_ROOT), os.environ.get("PYTHONPATH"))))}
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    modules = proc.stdout.split()
    assert "looper.loopercli" in modules
    assert not [m for m in modules if m.startswith(("looper.agents", "looper.db"))]
    assert "asyncpg" not in modules and "httpx" not in modules


# --- the client file ---


def test_the_client_file_is_the_override_then_the_environment_then_xdg_then_home(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert loopercli.client_file(None) == tmp_path / "home" / ".config" / "looper" / "client.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert loopercli.client_file(None) == tmp_path / "xdg" / "looper" / "client.json"
    monkeypatch.setenv("LOOPER_CLIENT_FILE", str(tmp_path / "env.json"))
    assert loopercli.client_file(None) == tmp_path / "env.json"
    assert loopercli.client_file(str(tmp_path / "flag.json")) == tmp_path / "flag.json"


def test_a_missing_client_file_says_how_to_log_in(cli: Run, tmp_path: Path) -> None:
    code, out, err = cli("--client-file", str(tmp_path / "nope.json"), "whoami")
    assert code == 4
    assert "loopercli login" in err and "Settings → API clients" in err


def test_a_client_file_with_a_loose_mode_is_refused(cli: Run, client: Path) -> None:
    client.chmod(0o644)
    code, _, err = cli("whoami")
    assert code == 4
    assert f"chmod 600 {client}" in err
    assert TOKEN not in err


def test_a_symlink_to_a_good_client_file_is_refused(cli: Run, client: Path, tmp_path: Path) -> None:
    link = tmp_path / "link.json"
    link.symlink_to(client)
    code, _, err = cli("--client-file", str(link), "whoami")
    assert code == 4 and "mode 0600" in err


def test_a_client_file_that_is_not_a_regular_file_is_refused(cli: Run, tmp_path: Path) -> None:
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo, 0o600)
    code, _, err = cli("--client-file", str(fifo), "whoami")
    assert code == 4 and "regular file" in err


@pytest.mark.parametrize("url", ["http://example.com:8767", "http://10.0.0.1:8767", "ftp://127.0.0.1",
                                 "http://127.0.0.1@example.com", "http://localhost.example.com", "127.0.0.1:8767"])
def test_a_client_file_for_a_looper_on_another_machine_is_refused(cli: Run, client: Path, url: str) -> None:
    write_client_file(client, url)
    code, _, err = cli("whoami")
    assert code == 4 and "must be a loopback address" in err


@pytest.mark.parametrize("url", ["http://localhost:8767/", "http://[::1]:8767", "https://127.0.0.2"])
def test_loopback_urls_are_accepted_without_a_trailing_slash(url: str) -> None:
    assert loopercli.check_url(url) == url.rstrip("/")


@pytest.mark.parametrize("content", ["not json", "[]", '{"url": "http://127.0.0.1"}',
                                     '{"url": "http://127.0.0.1", "token": "other"}'])
def test_a_client_file_without_a_url_and_an_api_client_token_is_refused(cli: Run, client: Path, content: str) -> None:
    client.write_text(content)
    code, _, err = cli("whoami")
    assert code == 4 and "loopercli login" in err


def test_a_call_carries_the_token_and_the_session_from_the_flag_or_the_environment(
        cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli("--session", "s-flag", "tasks", "list")[0] == 0
    monkeypatch.setenv("LOOPER_SESSION", "s-env")
    assert cli("tasks", "list")[0] == 0
    assert cli("--session", "s-flag", "tasks", "list")[0] == 0
    monkeypatch.delenv("LOOPER_SESSION")
    assert cli("tasks", "list")[0] == 0
    sessions = [r[2].get("x-looper-session") for r in looper.calls()]
    assert sessions == ["s-flag", "s-env", "s-flag", None]
    assert {r[2]["authorization"] for r in looper.requests} == {f"Bearer {TOKEN}"}
    assert {r[2]["content-type"] for r in looper.calls()} == {"application/json"}


def test_a_proxy_in_the_environment_does_not_carry_the_request(
        cli: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{closed_port()}")
    assert cli("whoami")[0] == 0


def test_the_client_file_is_read_on_every_call(cli: Run, client: Path, looper: FakeLooper, tmp_path: Path) -> None:
    assert cli("whoami")[0] == 0
    write_client_file(client, looper.url, "lpc_other")
    code, _, err = cli("whoami")
    assert code == 4 and "refused the token" in err


# --- login ---


def test_login_reads_the_token_from_stdin_and_writes_a_private_file(
        cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    target = tmp_path / "new" / "looper" / "client.json"
    code, out, err = cli("--client-file", str(target), "login", "--url", looper.url + "/", stdin=f"{TOKEN}\nrest\n",
                         client=False)
    assert code == 0, err
    assert out.strip() == f'signed in as API client "ci"; wrote {target}'
    assert json.loads(target.read_text()) == {"url": looper.url, "token": TOKEN}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert TOKEN not in out + err
    assert [p.name for p in target.parent.iterdir()] == ["client.json"]
    assert cli("whoami")[0] == 0  # the same file works for the other commands


def test_login_with_a_refused_token_exits_4_and_writes_nothing(cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    target = tmp_path / "dir" / "client.json"
    code, out, err = cli("--client-file", str(target), "login", "--url", looper.url, stdin="lpc_revoked\n",
                         client=False)
    assert code == 4 and "refused the token" in err
    assert not target.parent.exists()
    assert "lpc_revoked" not in out + err


def test_login_refuses_a_remote_url_before_any_request(cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    target = tmp_path / "login.json"
    code, out, err = cli("--client-file", str(target), "login", "--url", "http://10.0.0.1:1", stdin=f"{TOKEN}\n",
                         client=False)
    assert code in (2, 4) and "loopback" in err
    assert not target.exists() and TOKEN not in out + err


def test_login_refuses_text_that_is_not_an_api_client_token(cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    target = tmp_path / "login.json"
    code, out, err = cli("--client-file", str(target), "login", "--url", looper.url, stdin="operator-secret\n",
                         client=False)
    assert code == 2 and "lpc_" in err
    assert looper.requests == [] and not target.exists()
    assert "operator-secret" not in out + err


def test_login_takes_no_token_argument(cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    code, _, err = cli("--client-file", str(tmp_path / "c.json"), "login", "--token", TOKEN, client=False)
    assert code == 2 and TOKEN not in err
    assert looper.requests == []


def test_login_defaults_to_the_local_dashboard_address(
        cli: Run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def whoami(self: Api, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
        seen.append(self.url)
        return 200, api_v1.whoami(api_v1.Principal("client", "c-1", "ci"))

    monkeypatch.setattr(Api, "call", whoami)
    target = tmp_path / "c.json"
    assert cli("--client-file", str(target), "login", stdin=f"{TOKEN}\n", client=False)[0] == 0
    assert seen == ["http://127.0.0.1:8767"]


# --- the generated commands ---


def test_a_command_is_generated_for_each_operation_without_a_hand_written_one() -> None:
    names = {(head, tail) for head, tail, *_ in loopercli.generated_operations(api_v1.document())}
    assert ("tasks", "list") in names and ("task-create", "") in names and ("whoami", "") in names
    assert ("task-control", "start-over") in names
    hand = {op.operation_id.partition(".")[0].replace("_", "-") for op in api_v1.operations() if op.hand}
    assert {"chat", "confirm", "openapi"} <= hand
    assert not {head for head, *_ in names} & hand


def test_confirm_is_listed_once_and_chat_is_not_generated(cli: Run) -> None:
    code, out, _ = cli("--help")
    assert code == 0
    commands = [line.split()[0] for line in out.splitlines() if line.startswith("    ") and line.split()]
    assert commands.count("confirm") == 1 and "login" in commands and "whoami" in commands
    assert "chat" not in commands and "openapi" not in commands
    assert loopercli.main(["--client-file", str(cli.client), "chat-send"]) == 2


def test_tasks_list_posts_its_options(cli: Run, looper: FakeLooper) -> None:
    code, _, err = cli("tasks", "list", "--limit", "5", "--group", "ready")
    assert code == 0, err
    [(method, path, _, body)] = looper.calls()
    assert (method, path, body) == ("POST", "/api/v1/ops/tasks/list", {"limit": 5, "group": "ready"})


def test_task_create_reads_a_string_option_from_a_file(cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    requirement = tmp_path / "req.md"
    requirement.write_text("# Build it\n\nWith care.\n")
    code, _, err = cli("task-create", "--requirement-file", str(requirement), "--title", "T", "--project", "P")
    assert code == 0, err
    assert looper.calls()[0][3] == {"requirement": "# Build it\n\nWith care.\n", "title": "T", "project": "P"}


def test_a_string_option_reads_stdin_when_the_file_is_a_dash(cli: Run, looper: FakeLooper) -> None:
    assert cli("task-create", "--requirement-file", "-", "--title", "T", stdin="from stdin")[0] == 0
    assert looper.calls()[0][3] == {"requirement": "from stdin", "title": "T"}


def test_only_one_option_may_read_stdin(cli: Run, looper: FakeLooper) -> None:
    code, _, err = cli("task-update", "--task-id", "x", "--requirement-file", "-", "--body", "-", stdin="{}")
    assert code == 2 and "stdin" in err
    assert looper.calls() == []


def test_a_string_option_and_its_file_exclude_each_other(cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    file = tmp_path / "r"
    file.write_text("x")
    assert cli("task-create", "--requirement", "a", "--requirement-file", str(file))[0] == 2
    assert looper.calls() == []


def test_a_missing_file_option_is_a_usage_error(cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    code, _, err = cli("task-create", "--requirement-file", str(tmp_path / "nope"))
    assert code == 2 and "--requirement-file" in err
    assert looper.calls() == []


def test_an_object_option_is_json_text(cli: Run, looper: FakeLooper) -> None:
    code, _, err = cli("task-update", "--task-id", "X", "--profiles", '{"worker": null}')
    assert code == 0, err
    assert looper.calls()[0][3] == {"task_id": "X", "profiles": {"worker": None}}


def test_an_option_with_a_nullable_or_no_type_is_json_text(cli: Run, looper: FakeLooper) -> None:
    assert cli("models", "update", "--model", '"claude-x"', "--efforts", '["low", "high"]',
               "--context-window", "null")[0] == 0
    assert looper.calls()[0][3] == {"model": "claude-x", "efforts": ["low", "high"], "context_window": None}


def test_a_boolean_option_has_a_negative_form(cli: Run, looper: FakeLooper) -> None:
    assert cli("catalog", "list", "--enabled")[0] == 0
    assert cli("catalog", "list", "--no-enabled")[0] == 0
    assert [call[3] for call in looper.calls()] == [{"enabled": True}, {"enabled": False}]


def test_the_body_option_is_the_base_and_the_options_override_it(
        cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    base = tmp_path / "body.json"
    base.write_text(json.dumps({"title": "from body", "project": "P", "profiles": {"worker": "w"}}))
    assert cli("task-create", "--body", str(base), "--title", "from option")[0] == 0
    assert cli("task-create", "--body", "-", "--project", "Q", stdin='{"title": "t", "project": "P"}')[0] == 0
    assert [call[3] for call in looper.calls()] == [
        {"title": "from option", "project": "P", "profiles": {"worker": "w"}}, {"title": "t", "project": "Q"}]


@pytest.mark.parametrize("body", ["[1]", "{bad", '"text"'])
def test_a_body_that_is_not_a_json_object_is_a_usage_error(cli: Run, looper: FakeLooper, body: str) -> None:
    code, _, err = cli("task-create", "--body", "-", stdin=body)
    assert code == 2 and "--body" in err
    assert looper.calls() == []


def test_a_bad_json_option_is_a_usage_error_naming_the_option(cli: Run, looper: FakeLooper) -> None:
    code, _, err = cli("task-update", "--task-id", "X", "--profiles", "{worker")
    assert code == 2 and "--profiles" in err
    assert looper.calls() == []


def test_a_number_option_that_is_not_one_is_a_usage_error(cli: Run, looper: FakeLooper) -> None:
    assert cli("tasks", "list", "--limit", "many")[0] == 2
    assert cli("tasks", "list", "--group", "nowhere")[0] == 2
    assert looper.calls() == []


def test_whoami_is_a_command_that_sends_a_get_and_no_body(cli: Run, looper: FakeLooper) -> None:
    code, out, _ = cli("whoami")
    assert code == 0
    assert "principal: client" in out and "looper_version:" in out
    assert looper.requests[-1][:2] == ("GET", "/api/v1/whoami") and looper.requests[-1][3] is None


def test_a_get_operation_takes_no_options(cli: Run) -> None:
    assert cli("whoami", "--limit", "1")[0] == 2


def test_the_help_of_a_command_shows_its_options_with_their_choices(cli: Run) -> None:
    code, out, _ = cli("tasks", "list", "--help")
    assert code == 0
    flat = " ".join(out.split())
    assert "--group" in flat and "needs_you,in_progress,ready" in flat
    assert "--limit" in flat and "--body" in flat


def test_an_unknown_command_is_a_usage_error(cli: Run) -> None:
    code, _, err = cli("frobnicate")
    assert code == 2 and "frobnicate" in err


def test_global_options_may_follow_the_command(cli: Run, looper: FakeLooper, client: Path) -> None:
    code, out, _ = cli("tasks", "list", "--json", "--session", "s1", client=True)
    assert code == 0 and json.loads(out) == {"status": "ok", "result": {}}
    assert looper.calls()[0][2]["x-looper-session"] == "s1"


def test_version_needs_no_server_or_client_file(cli: Run) -> None:
    from looper import __version__

    code, out, _ = cli("--version", client=False)
    assert code == 0 and out.strip() == f"loopercli {__version__}"


def test_without_the_document_help_lists_the_hand_written_commands_and_the_reason(
        cli: Run, looper: FakeLooper) -> None:
    looper.document_status = 500
    code, out, _ = cli("--help")
    assert code == 0
    assert "login" in out and "confirm" in out and "did not serve its API description" in out
    code, out, err = cli("tasks", "list")
    assert code == 4 and "did not serve its API description" in err and "login" in err
    assert looper.calls() == []


def test_hand_written_commands_work_without_the_document(cli: Run, looper: FakeLooper) -> None:
    looper.document_status = 500
    looper.answers.append((200, {"status": "ok", "result": {"done": True}}))
    code, out, _ = cli("confirm", "c-9")
    assert code == 0 and "done: True" in out
    assert "/api/v1/openapi.json" not in [r[1] for r in looper.requests]


# --- the answers ---


def test_an_ok_answer_renders_scalars_then_a_table_then_notes(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((200, {"status": "ok", "notes": ["first note", "second note"], "result": {
        "tasks": [{"task_id": "t1", "name": "n", "status": "ready", "extra": {"a": 1}}], "more": False,
        "counts": {"ready": 1}, "tags": ["a", "b"]}}))
    code, out, _ = cli("tasks", "list")
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "more: False"
    assert "tasks:" in lines
    header = next(line for line in lines if "task_id" in line)
    assert header.split() == ["task_id", "name", "status"]  # only the scalar columns
    assert next(line for line in lines if "t1" in line).split() == ["t1", "n", "ready"]
    assert 'counts: {"ready": 1}' in lines and 'tags: ["a", "b"]' in lines
    assert out.endswith("first note\n\nsecond note\n")


def test_a_table_clips_its_cells_and_columns(cli: Run, looper: FakeLooper) -> None:
    row = {f"c{i}": "x" * 100 if i == 0 else i for i in range(12)}
    looper.answers.append((200, {"status": "ok", "result": {"rows": [row], "blob": {"k": "y" * 300}}}))
    _, out, _ = cli("tasks", "list")
    header, line = [ln for ln in out.splitlines() if ln.startswith("  ")][:2]
    assert header.split() == [f"c{i}" for i in range(8)]
    assert line.split()[0] == "x" * 59 + "…"
    blob = next(ln for ln in out.splitlines() if ln.startswith("blob: "))
    assert len(blob) == len("blob: ") + 200 and blob.endswith("…")


def test_a_result_that_is_not_an_object_is_printed_as_json(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((200, {"status": "ok", "result": ["a", "b"]}))
    assert json.loads(cli("tasks", "list")[1]) == ["a", "b"]


def test_needs_confirmation_exits_3_and_says_how_to_confirm(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((202, {"status": "needs_confirmation", "confirmation_id": "c-1", "impact": "Cancels t1.",
                                 "expires_in_seconds": 300}))
    code, out, _ = cli("task-control", "cancel", "--task-id", "t1")
    assert code == 3
    assert out.splitlines() == ["Cancels t1.", "Confirm with: loopercli confirm c-1",
                                "(expires in 300 s; same client and session)"]


def test_confirm_posts_the_confirmation_id(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((200, {"status": "ok", "result": {"cancelled": True}}))
    code, out, _ = cli("--session", "s1", "confirm", "c-1")
    assert code == 0 and "cancelled: True" in out
    [(method, path, headers, body)] = looper.calls()
    assert (method, path, body) == ("POST", "/api/v1/confirm", {"confirmation_id": "c-1"})
    assert headers["x-looper-session"] == "s1"


def test_an_error_answer_exits_1_with_its_code_and_message_on_stderr(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((409, {"status": "error", "error": {"code": "conflict", "message": "task is running"}}))
    code, out, err = cli("task-control", "start", "--task-id", "t1")
    assert code == 1 and out == ""
    assert err.strip() == "error: conflict: task is running"


def test_json_prints_the_answer_as_it_is(cli: Run, looper: FakeLooper) -> None:
    answer = {"status": "ok", "result": {"tasks": [{"task_id": "é"}], "more": False}, "notes": ["n"]}
    looper.answers.append((200, answer))
    code, out, _ = cli("--json", "tasks", "list")
    assert code == 0 and out == json.dumps(answer, ensure_ascii=False, indent=2) + "\n"
    looper.answers.append((409, {"status": "error", "error": {"code": "conflict", "message": "m"}}))
    code, out, err = cli("--json", "tasks", "list")
    assert code == 1 and json.loads(out)["error"]["code"] == "conflict" and err == ""


# --- Looper unreachable or not trusting the token ---


def test_a_closed_port_exits_4_without_the_token(cli: Run, client: Path) -> None:
    write_client_file(client, f"http://127.0.0.1:{closed_port()}")
    code, out, err = cli("tasks", "list")
    assert code == 4 and "is looper serve running?" in err
    assert TOKEN not in out + err


def test_a_revoked_token_exits_4_without_the_token(cli: Run, client: Path, looper: FakeLooper) -> None:
    write_client_file(client, looper.url, "lpc_revoked")
    code, out, err = cli("whoami")
    assert code == 4 and "refused the token" in err and "loopercli login" in err
    assert "lpc_revoked" not in out + err


def test_an_answer_that_is_not_json_exits_4(cli: Run) -> None:
    def html(handler: BaseHTTPRequestHandler, request: Request) -> None:
        raw = b"<html>nginx</html>"
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    other = Server(html)
    try:
        write_client_file(cli.client, other.url)
        code, _, err = cli("whoami")
    finally:
        other.close()
    assert code == 4 and "without a JSON object" in err


# --- redirects ---


@pytest.fixture
def elsewhere() -> Iterator[Server]:
    server = Server(lambda handler, request: _send(handler, 200, {"status": "ok", "result": {}}))
    yield server
    server.close()


@pytest.mark.parametrize("status", [302, 307])
@pytest.mark.parametrize(("kind", "prefix"), [
    ("login", "/api/v1/whoami"),
    ("call", "/api/v1/ops/"),
    ("document", "/api/v1/openapi.json"),
])
def test_a_redirect_is_never_followed(cli: Run, looper: FakeLooper, elsewhere: Server, client: Path, tmp_path: Path,
                                      status: int, kind: str, prefix: str) -> None:
    looper.redirects[prefix] = (status, elsewhere.url)
    target = tmp_path / "login" / "client.json"
    if kind == "login":
        code, out, err = cli("--client-file", str(target), "login", "--url", looper.url, stdin=f"{TOKEN}\n",
                             client=False)
    else:
        code, out, err = cli("tasks", "list")
    assert code == 4
    assert "does not follow redirects" in err and str(status) in err
    assert elsewhere.requests == []
    assert TOKEN not in out + err
    assert not target.parent.exists()


@pytest.mark.parametrize("status", [302, 307])
def test_a_redirected_stream_is_never_followed(looper: FakeLooper, elsewhere: Server, status: int) -> None:
    looper.redirects["/api/v1/chat/send"] = (status, elsewhere.url)
    api = Api(looper.url, TOKEN, "s1")
    with pytest.raises(Unreachable, match="does not follow redirects") as raised:
        list(api.stream("/api/v1/chat/send", {"text": "hi"}))
    assert TOKEN not in str(raised.value)
    assert elsewhere.requests == []


# --- the stream ---


def test_stream_yields_the_events_and_skips_comments(looper: FakeLooper) -> None:
    body = (b": hello\n\nevent: accepted\ndata: {\"conversation_id\": \"cli-1\"}\n\n"
            b"event: delta\r\ndata: {\"text\": \"a\"}\r\n\r\n"
            b"event: heartbeat\ndata: {}\n\n"
            b"event: final\ndata: {\"text\":\ndata: \"done\"}\n\n")

    def respond(handler: BaseHTTPRequestHandler, request: Request) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    server = Server(respond)
    try:
        events = list(Api(server.url, TOKEN, "s1").stream("/api/v1/chat/send", {"text": "hi"}))
    finally:
        server.close()
    assert events == [("accepted", {"conversation_id": "cli-1"}), ("delta", {"text": "a"}), ("heartbeat", {}),
                      ("final", {"text": "done"})]
    method, path, headers, sent = server.requests[0]
    assert (method, path, sent) == ("POST", "/api/v1/chat/send", {"text": "hi"})
    assert headers["accept"] == "text/event-stream" and headers["x-looper-session"] == "s1"


def test_stream_of_an_error_answer_is_that_answer(looper: FakeLooper) -> None:
    looper.answers.append((400, {"status": "error", "error": {"code": "invalid", "message": "text"}}))
    events = list(Api(looper.url, TOKEN).stream("/api/v1/chat/send", {}))
    assert events == [("answer", {"status": "error", "error": {"code": "invalid", "message": "text"}})]
