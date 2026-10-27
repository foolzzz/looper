"""``loopercli`` against a fake Looper: a ``ThreadingHTTPServer`` on loopback that serves the generated OpenAPI document
and records the calls. No database; the CLI itself imports nothing of Looper but its version."""

from __future__ import annotations

import builtins
import io
import json
import os
import re
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
        self.streams: list[list[tuple[str, dict[str, Any]]]] = []  # the events /chat/send answers with, per call
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
        elif path == "/api/v1/chat/send" and self.streams:
            body = "".join(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                           for name, data in self.streams.pop(0)).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
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


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_config_group_help_discovers_generated_children(cli: Run, looper: FakeLooper, flag: str) -> None:
    code, out, err = cli("config", flag)
    assert code == 0 and err == ""
    # A name in the usage braces does not make it a discoverable, descriptive row.
    descriptions = {}
    for child in (
        "export",
        "import",
        "import-cancel",
        "import-upload",
        "import-apply",
        "import-preview",
    ):
        rows = re.findall(rf"^\s+{re.escape(child)}[ \t]+(\S[^\n]*)$", out, re.MULTILINE)
        assert len(rows) == 1, f"expected one descriptive help row for {child}"
        descriptions[child] = rows[0].lower()
    assert "export" in descriptions["export"]
    assert "configuration" in descriptions["export"]
    assert "json" in descriptions["export"]
    assert "preview" in descriptions["import"]
    assert "confirmation" in descriptions["import"]
    assert TOKEN not in out + err
    assert [(r[0], r[1]) for r in looper.requests] == [("GET", "/api/v1/openapi.json")]


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("failure", ["http", "connection", "client"])
def test_config_group_help_falls_back_when_discovery_fails(
        cli: Run, looper: FakeLooper, client: Path, flag: str, failure: str) -> None:
    looper.document_status = 500
    if failure == "connection":
        write_client_file(client, f"http://127.0.0.1:{closed_port()}")
    elif failure == "client":
        client.unlink()
    code, out, err = cli("config", flag)
    assert code == 0 and err == ""
    assert "usage: loopercli config" in out
    children = out.replace("{", " ").replace("}", " ").replace(",", " ").split()
    assert "export" in children and "import" in children
    for child in ("export", "import"):
        assert re.search(rf"^\s+{child}[ \t]+\S[^\n]*$", out, re.MULTILINE)
    assert "confirm" not in children
    assert "the commands made from Looper's API are not shown" in out
    assert {"http": "HTTP 500", "connection": "is looper serve running?", "client": "loopercli login"}[failure] in out
    assert TOKEN not in out
    assert [(r[0], r[1]) for r in looper.requests] == (
        [("GET", "/api/v1/openapi.json")] if failure == "http" else [])


@pytest.mark.parametrize(("argv", "code", "option"), [
    (["config", "export", "-h"], 0, "--include-projects"),
    (["config", "import", "--help"], 0, "--apply"),
    (["confirm", "--help"], 0, "ID"),
    (["chat", "-h"], 0, "--file"),
    (["config", "--unknown"], 2, "usage:"),
    (["chat", "--file"], 2, "--file"),
])
def test_handwritten_children_and_flags_do_not_discover_openapi(
        cli: Run, looper: FakeLooper, argv: list[str], code: int, option: str) -> None:
    looper.document_status = 500
    actual, out, err = cli(*argv)
    assert actual == code and option in out + err
    assert looper.requests == []


@pytest.mark.parametrize(("action", "flags", "body"), [
    ("import-cancel", ["--import-id", "cfgimp-example"], {"import_id": "cfgimp-example"}),
    ("import-upload", ["--format", "looper-config/v4", "--catalog", "[]"],
     {"format": "looper-config/v4", "catalog": []}),
    ("import-apply", ["--import-id", "cfgimp-example"], {"import_id": "cfgimp-example"}),
    ("import-preview", ["--document", '{"format":"looper-config/v4"}'],
     {"document": {"format": "looper-config/v4"}}),
])
def test_generated_config_children_discover_the_document_from_main(cli: Run, looper: FakeLooper,
                                                                  action: str, flags: list[str], body: Any) -> None:
    answer = {"status": "ok", "result": {"import_id": "cfgimp-example"}}
    looper.answers.append((200, answer))
    code, out, err = cli("config", action, "--session", "config-test", *flags, "--json")
    assert code == 0 and json.loads(out) == answer and err == ""
    assert [(r[0], r[1]) for r in looper.requests] == [
        ("GET", "/api/v1/openapi.json"), ("POST", "/api/v1/config/" + action.replace("-", "_"))]
    assert looper.calls()[0][3] == body
    assert looper.calls()[0][2]["x-looper-session"] == "config-test"


@pytest.mark.parametrize(("argv", "operation", "body"), [
    (["export", "-"], "export", {"include_projects": False, "include_secrets": False}),
    (["import", "-"], "import_preview", {"document": {"format": "looper-config/v4"}}),
    (["import", "--cancel", "cfgimp-example"], "import_cancel", {"import_id": "cfgimp-example"}),
])
def test_handwritten_config_commands_keep_their_parsers(cli: Run, looper: FakeLooper,
                                                       argv: list[str], operation: str, body: Any) -> None:
    document = {"format": "looper-config/v4"}
    answer = {"status": "ok", "result": {"document": document}}
    looper.answers.append((200, answer))
    code, out, err = cli("--json", "config", *argv, stdin=json.dumps(document))
    assert code == 0 and json.loads(out) == (document if operation == "export" else answer) and err == ""
    assert [r[1] for r in looper.requests] == ["/api/v1/openapi.json", "/api/v1/config/" + operation]
    assert looper.calls()[0][3] == body


@pytest.mark.parametrize("argv", [
    ["lpc_ab-cd_ef-gh"],
    ["import-cancel", "--import-id"],
    ["import-cancel", "--unknown", "lpc_ab-cd_ef-gh"],
    ["import-upload", "--catalog", "lpc_ab-cd_ef-gh"],
    ["import", "--cancel"],
])
def test_config_usage_errors_do_not_send_operations_or_leak_tokens(cli: Run, looper: FakeLooper,
                                                                  argv: list[str]) -> None:
    code, out, err = cli("config", *argv)
    assert code == 2 and "usage:" in err
    assert "lpc_ab-cd_ef-gh" not in out + err and "cd_ef" not in out + err
    assert looper.calls() == []
    assert [r[1] for r in looper.requests] == ([] if argv[0] == "import" else ["/api/v1/openapi.json"])


def test_generated_config_child_with_an_unreachable_api_exits_4(cli: Run, client: Path) -> None:
    write_client_file(client, f"http://127.0.0.1:{closed_port()}")
    code, out, err = cli("config", "import-cancel", "--import-id", "cfgimp-example")
    assert code == 4 and "is looper serve running?" in err and TOKEN not in out + err


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


@pytest.mark.parametrize("argv", [
    ("login", "lpc_ab-cd_ef-gh"),
    ("login", "--json=lpc_ab-cd_ef-gh"),
    ("chat", "--file", "lpc_ab-cd_ef-gh"),
])
def test_a_usage_error_hides_a_token_with_hyphens(
        cli: Run, looper: FakeLooper, tmp_path: Path, argv: tuple[str, ...]) -> None:
    # A real token is lpc_ and token_urlsafe, whose alphabet has '-' and '_'.
    token = "lpc_ab-cd_ef-gh"
    code, _, err = cli("--client-file", str(tmp_path / "c.json"), *argv, client=False)
    assert code == 2 and token not in err and "cd_ef" not in err and "-gh" not in err
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


def test_confirm_and_chat_are_listed_once_and_chat_send_is_not_generated(cli: Run) -> None:
    code, out, _ = cli("--help")
    assert code == 0
    commands = [line.split()[0] for line in out.splitlines() if line.startswith("    ") and line.split()]
    assert commands.count("confirm") == 1 and "login" in commands and "whoami" in commands
    assert commands.count("chat") == 1 and "wait" in commands and "openapi" not in commands
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


def test_the_confirm_hint_names_the_session_the_call_ran_in(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((202, {"status": "needs_confirmation", "confirmation_id": "c-1", "impact": "Cancels t1.",
                                 "expires_in_seconds": 300}))
    code, out, _ = cli("--session", "my session", "task-control", "cancel", "--task-id", "t1")
    assert code == 3
    assert out.splitlines()[1] == "Confirm with: loopercli --session 'my session' confirm c-1"


def test_confirm_posts_the_confirmation_id(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((200, {"status": "ok", "result": {"cancelled": True}}))
    code, out, _ = cli("--session", "s1", "confirm", "c-1")
    assert code == 0 and "cancelled: True" in out
    [(method, path, headers, body)] = looper.calls()
    assert (method, path, body) == ("POST", "/api/v1/confirm", {"confirmation_id": "c-1"})
    assert headers["x-looper-session"] == "s1"


@pytest.mark.parametrize("message,shown", [("task is running", "task is running"),
                                           ("invalid lpc_ab-cd_ef-gh", "invalid lpc_…")])
def test_an_error_answer_exits_1_with_its_code_and_message_on_stderr(
        cli: Run, looper: FakeLooper, message: str, shown: str) -> None:
    looper.answers.append((409, {"status": "error", "error": {"code": "conflict", "message": message}}))
    code, out, err = cli("task-control", "start", "--task-id", "t1")
    assert code == 1 and out == ""
    assert err.strip() == f"error: conflict: {shown}"


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


# --- chat ---


def reply_stream(*texts: str, final: str | None = None, outcome: str = "answered",
                 message_id: str = "m-1") -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = [
        ("accepted", {"conversation_id": "cli-1", "message_id": message_id, "manager_id": "manager"})]
    events += [("delta", {"text": text}) for text in texts]
    return [*events, ("final", {"text": "".join(texts) if final is None else final, "outcome": outcome})]


def messages_page(*messages: tuple[str, str, str], next_cursor: str | None, has_more: bool = False) -> tuple[int, Any]:
    return 200, {"status": "ok", "result": {
        "conversation_id": "cli-1", "manager_id": "manager", "has_more": has_more, "next_cursor": next_cursor,
        "messages": [{"message_id": i, "role": role, "content": content, "created_at": "2026-01-01T00:00:00Z"}
                     for i, role, content in messages]}}


def chat_bodies(looper: FakeLooper) -> list[Any]:
    return [r[3] for r in looper.calls() if r[1] == "/api/v1/chat/send"]


def test_chat_prints_the_streamed_reply_then_what_looper_appended(cli: Run, looper: FakeLooper) -> None:
    looper.streams.append([
        ("accepted", {"conversation_id": "cli-1", "message_id": "m-1", "manager_id": "manager"}),
        ("status", {"label": "thinking"}), ("heartbeat", {}), ("delta", {"text": "Hi"}), ("delta", {"text": " there"}),
        ("final", {"text": "Hi there\n\n需要你确认：loopercli confirm c-1", "outcome": "answered"})])
    code, out, err = cli("chat", "hello")
    assert code == 0
    assert out == "Hi there\n\n需要你确认：loopercli confirm c-1\n"
    assert err.splitlines() == ["conversation: cli-1", "[thinking]"]
    assert chat_bodies(looper) == [{"text": "hello"}]


def test_a_final_that_does_not_continue_what_was_printed_is_printed_whole_after_a_newline(
        cli: Run, looper: FakeLooper) -> None:
    looper.streams.append(reply_stream("Draft", final="Rewritten"))
    code, out, _ = cli("chat", "hello")
    assert code == 0 and out == "Draft\nRewritten\n"


def test_chat_sends_the_conversation_the_manager_the_language_and_a_files_content(
        cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    file = tmp_path / "msg.md"
    file.write_text("from a file\n")
    looper.streams.append(reply_stream("ok"))
    code, _, err = cli("chat", "--conversation", "cli-1", "--manager", "manager-review", "--language", "en",
                       "--file", str(file))
    assert code == 0, err
    assert chat_bodies(looper) == [{"text": "from a file\n", "conversation_id": "cli-1",
                                    "manager_id": "manager-review", "language": "en"}]


def test_chat_with_a_dash_or_without_a_message_reads_stdin(cli: Run, looper: FakeLooper) -> None:
    looper.streams += [reply_stream("a"), reply_stream("b")]
    assert cli("chat", "-", stdin="piped")[0] == 0
    assert cli("chat", stdin="also piped")[0] == 0
    assert chat_bodies(looper) == [{"text": "piped"}, {"text": "also piped"}]


def test_chat_refuses_an_empty_message_and_one_over_the_limit_without_a_request(
        cli: Run, looper: FakeLooper, tmp_path: Path) -> None:
    file = tmp_path / "long.md"
    file.write_text("x" * 32_001)
    code, _, err = cli("chat", "--file", str(file))
    assert code == 2
    assert "the message has 32001 characters; at most 32000: summarise it and point to the files" in err
    assert cli("chat", "  ", stdin="")[0] == 2
    assert cli("chat", stdin="\n")[0] == 2
    file.write_text("x" * 32_000)
    looper.streams.append(reply_stream("ok"))
    assert cli("chat", "--file", str(file))[0] == 0
    assert len(chat_bodies(looper)) == 1


def test_a_stream_that_times_out_says_so_and_exits_0(cli: Run, looper: FakeLooper) -> None:
    looper.streams.append([("accepted", {"conversation_id": "cli-1", "message_id": "m-1"}),
                           ("delta", {"text": "Part"}),
                           ("timeout", {"message": "the reply is still being written; read it later"})])
    code, out, err = cli("chat", "hello")
    assert code == 0 and out == "Part\n"
    assert "the reply is still being written; read it later" in err


def test_a_failed_reply_exits_1(cli: Run, looper: FakeLooper) -> None:
    looper.streams.append(reply_stream("Sorry", outcome="failed"))
    code, out, _ = cli("chat", "hello")
    assert code == 1 and out == "Sorry\n"


def test_a_stream_that_ends_without_a_reply_exits_1(cli: Run, looper: FakeLooper) -> None:
    looper.streams.append([("accepted", {"conversation_id": "cli-1", "message_id": "m-1"})])
    code, _, err = cli("chat", "hello")
    assert code == 1 and "loopercli chat --conversation cli-1 --read" in err


def test_an_error_answer_instead_of_a_stream_exits_1(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((404, {"status": "error", "error": {"code": "not_found", "message": "no conversation x"}}))
    code, out, err = cli("chat", "--conversation", "x", "hello")
    assert code == 1 and out == ""
    assert err.strip() == "error: not_found: no conversation x"


def test_chat_json_prints_one_object_and_nothing_while_streaming(cli: Run, looper: FakeLooper) -> None:
    looper.streams.append(reply_stream("Hi", " there", final="Hi there\n\nconfirm?"))
    code, out, err = cli("--json", "chat", "hello")
    assert code == 0
    assert json.loads(out) == {"conversation_id": "cli-1", "message_id": "m-1", "reply": "Hi there\n\nconfirm?",
                               "outcome": "answered"}
    assert err.strip() == "conversation: cli-1"
    looper.streams.append([("accepted", {"conversation_id": "cli-2", "message_id": "m-9"}),
                           ("timeout", {"message": "m"})])
    assert json.loads(cli("--json", "chat", "again")[1]) == {
        "conversation_id": "cli-2", "message_id": "m-9", "reply": None, "outcome": "timeout"}


def test_chat_read_follows_the_servers_cursor_and_prints_each_message_once(cli: Run, looper: FakeLooper) -> None:
    looper.answers += [messages_page(("m-2", "assistant", "first"), ("m-3", "user", "second"), next_cursor="m-3",
                                     has_more=True),
                       messages_page(("m-4", "assistant", "third"), next_cursor="m-4")]
    code, out, err = cli("chat", "--conversation", "cli-1", "--read", "--after", "m-1")
    assert code == 0, err
    assert [call[3] for call in looper.calls()] == [{"conversation_id": "cli-1", "after": "m-1"},
                                                    {"conversation_id": "cli-1", "after": "m-3"}]
    assert looper.calls()[0][1] == "/api/v1/chat/messages"
    assert out == ("assistant · 2026-01-01T00:00:00Z\nfirst\n\nuser · 2026-01-01T00:00:00Z\nsecond\n\n"
                   "assistant · 2026-01-01T00:00:00Z\nthird\n")


def test_chat_read_json_is_one_object_of_all_the_pages(cli: Run, looper: FakeLooper) -> None:
    looper.answers += [messages_page(("m-2", "user", "a"), next_cursor="m-2", has_more=True),
                       messages_page(("m-3", "assistant", "b"), next_cursor="m-3")]
    code, out, _ = cli("--json", "chat", "--conversation", "cli-1", "--read")
    result = json.loads(out)
    assert code == 0 and looper.calls()[0][3] == {"conversation_id": "cli-1"}
    assert [m["message_id"] for m in result["messages"]] == ["m-2", "m-3"]
    assert (result["conversation_id"], result["manager_id"], result["next_cursor"]) == ("cli-1", "manager", "m-3")


def test_an_unknown_cursor_exits_1_with_the_servers_message_and_no_second_request(
        cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((400, {"status": "error", "error": {
        "code": "invalid", "message": "unknown cursor m-x in conversation cli-1; read without --after"}}))
    code, out, err = cli("chat", "--conversation", "cli-1", "--read", "--after", "m-x")
    assert code == 1 and out == ""
    assert "invalid: unknown cursor m-x" in err
    assert len(looper.calls()) == 1


def test_chat_read_needs_a_conversation_and_makes_no_request(cli: Run, looper: FakeLooper) -> None:
    code, _, err = cli("chat", "--read")
    assert code == 2 and "--conversation" in err
    assert looper.calls() == []


def test_chat_list_prints_a_table(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((200, {"status": "ok", "result": {"conversations": [
        {"conversation_id": "cli-1", "manager_id": "manager", "created_at": "c", "last_message_at": "2026-01-02",
         "last_content": "y" * 100},
        {"conversation_id": "cli-2", "manager_id": "manager-review", "created_at": "c", "last_message_at": None,
         "last_content": None}]}}))
    code, out, _ = cli("chat", "--list")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["conversation_id", "manager_id", "last_message_at", "last_content"]
    assert lines[1].split() == ["cli-1", "manager", "2026-01-02", "y" * 59 + "…"]
    assert lines[2].split() == ["cli-2", "manager-review"]
    assert looper.calls()[0][1:4:2] == ("/api/v1/chat/conversations", {})


class Ticks:
    """A sleep that does nothing and raises ``KeyboardInterrupt`` at the ``stop``-th call."""

    def __init__(self, stop: int) -> None:
        self.calls, self.stop = 0, stop

    def __call__(self, seconds: float) -> None:
        self.calls += 1
        if self.calls >= self.stop:
            raise KeyboardInterrupt


def test_follow_after_a_reply_starts_at_the_sent_message_and_prints_new_messages_once(
        cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loopercli.time, "sleep", ticks := Ticks(3))
    looper.streams.append(reply_stream("Hi there", message_id="m-1"))
    looper.answers += [
        messages_page(("m-2", "assistant", "Hi there"), next_cursor="m-2"),  # the reply already printed
        messages_page(("m-3", "user", "later"), next_cursor="m-3", has_more=True),
        messages_page(("m-4", "assistant", "Hi there"), next_cursor="m-4")]
    code, out, err = cli("chat", "--follow", "hello")
    assert code == 0, err
    bodies = [call[3] for call in looper.calls()[1:]]
    assert bodies == [{"conversation_id": "cli-1", "after": "m-1"}, {"conversation_id": "cli-1", "after": "m-2"},
                      {"conversation_id": "cli-1", "after": "m-3"}]
    assert out == ("Hi there\n" "user · 2026-01-01T00:00:00Z\nlater\n\n"
                   "assistant · 2026-01-01T00:00:00Z\nHi there\n")
    assert ticks.calls == 3


def test_follow_prints_a_reply_that_timed_out_and_one_json_line_per_message(
        cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loopercli.time, "sleep", Ticks(2))
    looper.streams.append([("accepted", {"conversation_id": "cli-1", "message_id": "m-1"}),
                           ("timeout", {"message": "later"})])
    looper.answers.append(messages_page(("m-2", "assistant", "late reply"), next_cursor="m-2"))
    code, out, _ = cli("--json", "chat", "--follow", "hello")
    assert code == 0
    turn, _, line = out.strip().rpartition("\n")
    assert json.loads(turn)["outcome"] == "timeout"
    assert json.loads(line)["content"] == "late reply"


def test_read_follow_starts_at_the_last_cursor_of_the_read(
        cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loopercli.time, "sleep", Ticks(2))
    looper.answers += [messages_page(("m-2", "user", "old"), next_cursor="m-2"),
                       messages_page(("m-3", "assistant", "new"), next_cursor="m-3")]
    code, out, _ = cli("chat", "--conversation", "cli-1", "--read", "--follow")
    assert code == 0
    assert [call[3] for call in looper.calls()] == [{"conversation_id": "cli-1"},
                                                    {"conversation_id": "cli-1", "after": "m-2"}]
    assert out.count("old") == 1 and out.count("new") == 1


def test_follow_with_an_unknown_cursor_exits_1(cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loopercli.time, "sleep", Ticks(5))
    looper.answers += [messages_page(next_cursor="m-2"),
                       (400, {"status": "error", "error": {"code": "invalid", "message": "unknown cursor m-2"}})]
    code, _, err = cli("chat", "--conversation", "cli-1", "--read", "--follow")
    assert code == 1 and "unknown cursor m-2" in err


def test_interactive_chat_keeps_the_conversation_between_messages(
        cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    lines = iter(["first", "", "second"])

    def fake_input(prompt: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(builtins, "input", fake_input)
    looper.streams += [reply_stream("one"), reply_stream("two")]
    code, out, err = cli("chat")
    assert code == 0 and out == "one\ntwo\n"
    assert chat_bodies(looper) == [{"text": "first"}, {"text": "second", "conversation_id": "cli-1"}]
    assert err.count("> ") == 4


@pytest.mark.parametrize("interrupt_on", [1, 2])
def test_interrupting_a_reply_in_the_prompt_says_the_reply_is_saved(
        cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch, interrupt_on: int) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    lines = iter(["first", "second"])

    def fake_input(prompt: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    real = Api.stream
    calls = 0

    def stream(self: Api, path: str, body: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        nonlocal calls
        calls += 1
        for event, data in real(self, path, body):
            yield event, data
            if calls == interrupt_on and event == "accepted":
                raise KeyboardInterrupt

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(Api, "stream", stream)
    looper.streams += [reply_stream("one"), reply_stream("two")]
    code, _, err = cli("chat")
    assert code == 0
    assert "(stopped reading; the reply is still saved: loopercli chat --conversation cli-1 --read)" in err
    assert chat_bodies(looper) == [{"text": "first"}, {"text": "second", "conversation_id": "cli-1"}]


# --- wait ---


def task_state(status: str, group: str, **more: Any) -> tuple[int, Any]:
    return 200, {"status": "ok", "result": {"task_id": "t1", "status": status, "group": group, **more}}


GATE = {"gate": "g1", "topic": "Pick a plan", "state": "open", "waiting_for": "user"}


def test_wait_returns_when_a_gate_needs_the_user(cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(loopercli.time, "sleep", sleeps.append)
    looper.answers += [task_state("running", "in_progress"), task_state("running", "in_progress"),
                       task_state("planning", "needs_you",
                                  gates=[GATE, {**GATE, "gate": "g2", "waiting_for": "planner"}])]
    code, out, err = cli("wait", "t1")
    assert code == 0, err
    assert out.splitlines()[0] == "task t1 planning (needs_you)"
    assert "gate g1: Pick a plan" in out and "loopercli gates messages --task-id t1 --gate g1" in out
    assert "g2" not in out
    assert [call[1:4:2] for call in looper.calls()] == [("/api/v1/ops/tasks/get", {"task_id": "t1"})] * 3
    assert sleeps == [10, 10]


def test_wait_for_end_keeps_waiting_through_a_gate_and_prints_the_result(
        cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loopercli.time, "sleep", lambda seconds: None)
    looper.answers += [task_state("running", "needs_you", gates=[GATE]), task_state("running", "needs_you"),
                       task_state("completed", "completed", result="All done")]
    code, out, _ = cli("wait", "t1", "--for", "end")
    assert code == 0 and len(looper.calls()) == 3
    assert out.splitlines() == ["task t1 completed (completed)", "result: All done"]


def test_wait_for_a_gate_ignores_the_end_and_the_interval_has_a_minimum(
        cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(loopercli.time, "sleep", sleeps.append)
    looper.answers += [task_state("failed", "attention"), task_state("planning", "needs_you", gates=[GATE])]
    code, out, _ = cli("wait", "t1", "--for", "gate", "--interval", "0")
    assert code == 0 and "needs_you" in out and sleeps == [2]


def test_wait_times_out_with_exit_6(cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter(range(0, 1000, 3))
    monkeypatch.setattr(loopercli.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(loopercli.time, "sleep", lambda seconds: None)
    looper.answers += [task_state("running", "in_progress")] * 5
    code, out, err = cli("wait", "t1", "--timeout", "1")
    assert code == 6 and out == ""
    assert err.strip() == "still running after 1 s"
    assert len(looper.calls()) == 1


def test_wait_for_a_missing_task_exits_1_and_json_prints_the_result(
        cli: Run, looper: FakeLooper, monkeypatch: pytest.MonkeyPatch) -> None:
    looper.answers.append((404, {"status": "error", "error": {"code": "not_found", "message": "no task nope"}}))
    code, _, err = cli("wait", "nope")
    assert code == 1 and err.strip() == "error: not_found: no task nope"
    looper.answers.append(task_state("completed", "completed"))
    code, out, _ = cli("--json", "wait", "t1")
    assert code == 0 and json.loads(out)["group"] == "completed"


def test_wait_with_an_unreachable_looper_exits_4(cli: Run, client: Path) -> None:
    write_client_file(client, f"http://127.0.0.1:{closed_port()}")
    assert cli("wait", "t1")[0] == 4


# --- the aliases ---


def test_start_and_stop_post_the_task_id_to_task_control(cli: Run, looper: FakeLooper) -> None:
    assert cli("start", "t1")[0] == 0
    assert cli("stop", "t1")[0] == 0
    assert [call[1:4:2] for call in looper.calls()] == [
        ("/api/v1/ops/task_control/start", {"task_id": "t1"}), ("/api/v1/ops/task_control/stop", {"task_id": "t1"})]


def test_status_is_one_task_or_the_overview(cli: Run, looper: FakeLooper) -> None:
    assert cli("status", "t1")[0] == 0
    assert cli("status")[0] == 0
    assert [call[1:4:2] for call in looper.calls()] == [
        ("/api/v1/ops/tasks/get", {"task_id": "t1"}), ("/api/v1/ops/overview", {})]


def test_create_and_list_take_the_options_of_the_operations_they_stand_for(
        cli: Run, looper: FakeLooper) -> None:
    assert cli("create", "--title", "T", "--requirement", "R")[0] == 0
    assert cli("list", "--limit", "5", "--group", "ready")[0] == 0
    assert [call[1:4:2] for call in looper.calls()] == [
        ("/api/v1/ops/task_create", {"title": "T", "requirement": "R"}),
        ("/api/v1/ops/tasks/list", {"limit": 5, "group": "ready"})]
    assert cli("list", "--group", "nowhere")[0] == 2


def test_pipeline_renders_steps_gates_and_waits_as_tables(cli: Run, looper: FakeLooper) -> None:
    looper.answers.append((200, {"status": "ok", "result": {
        "task_id": "t1", "name": "Build", "status": "planning", "group": "needs_you", "phase": None,
        "steps": [{"step": "s1", "kind": "work", "status": "done", "depends_on": [], "role": "worker",
                   "instruction": "do it"},
                  {"step": "s2", "kind": "review", "status": "ready", "depends_on": ["s1", "s0"],
                   "instruction": "check it"}],
        "gates": [GATE], "waits": [{"step": "w1", "state": "open", "outcome": None, "topic": "CI", "check": "ci"}]}}))
    code, out, err = cli("pipeline", "t1")
    assert code == 0, err
    lines = out.splitlines()
    assert "task_id: t1" in lines and "steps:" in lines and "gates:" in lines and "waits:" in lines
    header = next(line for line in lines if "depends_on" in line)
    assert header.split() == ["step", "kind", "status", "depends_on", "role", "instruction"]
    assert next(line for line in lines if "s1,s0" in line).split() == ["s2", "review", "ready", "s1,s0", "check", "it"]
    assert next(line for line in lines if line.split()[:1] == ["g1"]).split()[:4] == ["g1", "open", "user", "Pick"]
    assert next(line for line in lines if line.split()[:1] == ["w1"]).split()[:3] == ["w1", "open", "CI"]
    assert looper.calls()[0][1:4:2] == ("/api/v1/ops/task_pipeline", {"task_id": "t1"})


def test_pipeline_json_and_errors_use_the_generic_answer(cli: Run, looper: FakeLooper) -> None:
    answer = {"status": "ok", "result": {"task_id": "t1", "steps": []}}
    looper.answers.append((200, answer))
    code, out, _ = cli("--json", "pipeline", "t1")
    assert code == 0 and json.loads(out) == answer
    looper.answers.append((404, {"status": "error", "error": {"code": "not_found", "message": "no task t1"}}))
    code, _, err = cli("pipeline", "t1")
    assert code == 1 and "not_found" in err
