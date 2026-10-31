"""``loopercli``: a client of the Looper on this machine, built from its OpenAPI document (RFC ``looper-client-skill``
§6, §12).

It imports the standard library and the version only, so it starts fast and works where Looper's own dependencies are
not installed. Its commands are not written here: ``GET /api/v1/openapi.json`` lists the operations and each becomes a
command, except the few the document marks ``x-looper-cli-hand``, which are written below. The token is read from a
private file on every call, goes only to a loopback address and never follows a redirect.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import getpass
import http.client
import importlib.resources
import ipaddress
import json
import os
import re
import secrets
import shlex
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from looper import __version__

DEFAULT_URL = "http://127.0.0.1:8767"
TOKEN_PREFIX = "lpc_"
TIMEOUT_SECONDS = 120  # a request, or one read of a stream (the server sends a heartbeat every 15 s)
API_PREFIX = "/api/v1"
TABLE_COLUMNS = 8
CELL_CHARS = 60
LINE_CHARS = 200
MAX_MESSAGE_CHARS = 32_000  # what chat.send accepts
FOLLOW_SECONDS = 5  # between the polls of chat --follow
WAIT_INTERVAL_SECONDS, WAIT_MIN_INTERVAL_SECONDS = 10, 2
_MAX_CLIENT_FILE_BYTES = 64 * 1024
# Non-blocking, so a FIFO in the file's place can't hang the read before fstat refuses it.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_CONFIRM, EXIT_UNREACHABLE, EXIT_WAIT_TIMEOUT = 0, 1, 2, 3, 4, 6


class CliError(Exception):
    """Something to say on stderr before exiting with ``code``."""

    code = EXIT_ERROR


class UsageError(CliError):
    code = EXIT_USAGE

    def __init__(self, message: str, usage: str = "") -> None:
        super().__init__(message)
        self.usage = usage


class ClientError(CliError):
    """The client file is missing or unusable."""

    code = EXIT_UNREACHABLE


class Unreachable(CliError):
    """Looper did not answer as a Looper that trusts this token would."""

    code = EXIT_UNREACHABLE


# --- the client file ---


def client_file(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    if os.environ.get("LOOPER_CLIENT_FILE"):
        return Path(os.environ["LOOPER_CLIENT_FILE"]).expanduser()
    config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config) if config else Path.home() / ".config"
    return base / "looper" / "client.json"


def check_url(url: str) -> str:
    """``url`` without a trailing ``/``, when it is the address of a Looper on this machine."""
    refusal = ClientError("loopercli talks only to a Looper on this machine: the URL must be a loopback address")
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 (raises ValueError for a bad port)
    except ValueError:
        raise refusal from None
    if parts.scheme not in ("http", "https") or not host or parts.username is not None:
        raise refusal
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise refusal
        except ValueError:
            raise refusal from None
    return url.rstrip("/")


def _read_private(path: Path) -> bytes:
    """Read a credential only from the user's own regular file with mode 0600, never a symlink."""
    unusable = ClientError(f"{path} must be your own regular file with mode 0600 (run: chmod 600 {path})")
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        raise ClientError(f"no {path}: create a token in Settings → API clients, then run: "
                          "loopercli login --url <the dashboard's address>") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # a symlink
            raise unusable from None
        raise ClientError(f"cannot read {path}: {exc.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise unusable
        raw = os.read(fd, _MAX_CLIENT_FILE_BYTES + 1)
    except OSError as exc:
        raise ClientError(f"cannot read {path}: {exc.strerror}") from None
    finally:
        os.close(fd)
    if len(raw) > _MAX_CLIENT_FILE_BYTES:
        raise ClientError(f"{path} is too large for a credential file")
    return raw


def read_client(path: Path) -> tuple[str, str]:
    """The ``(url, token)`` of the client file, checked every invocation."""
    raw = _read_private(path)
    try:
        data = json.loads(raw) if len(raw) <= _MAX_CLIENT_FILE_BYTES else None
    except ValueError:
        data = None
    url, token = (data.get("url"), data.get("token")) if isinstance(data, dict) else (None, None)
    if not isinstance(url, str) or not isinstance(token, str):
        raise ClientError(f"{path} is not {{\"url\": ..., \"token\": ...}}: run loopercli login again")
    url = check_url(url)
    if not token.startswith(TOKEN_PREFIX):
        raise ClientError(f"{path} holds no API client token ({TOKEN_PREFIX}…): run loopercli login again")
    return url, token


def write_client(path: Path, url: str, token: str) -> None:
    """Write the client file atomically with mode 0600, creating its directory with mode 0700."""
    parent = path.parent
    os.makedirs(parent, mode=0o700, exist_ok=True)
    temporary = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    data = (json.dumps({"url": url, "token": token}, indent=2) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


# --- HTTP ---


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never send a request again: urllib raises ``HTTPError`` with the 3xx status instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Api:
    """The requests to Looper's ``/api/v1``. Every one goes through one opener: no proxy, no redirect."""

    def __init__(self, url: str, token: str, session: str | None = None) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self._session = session
        # The URL is a Looper on this machine; a proxy from the environment must not carry it.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """The HTTP status and the JSON object of the answer, for any status."""
        response = self._open(method, path, body, "application/json")
        try:
            status, payload = response.status, self._json(response)
        finally:
            response.close()
        return status, payload

    def stream(self, path: str, body: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        """POST ``body`` and yield the ``(event, data)`` of the server-sent events of the answer.

        An answer that is not a stream (an error) is one event ``("answer", <the JSON object>)``.
        """
        response = self._open("POST", path, body, "text/event-stream")
        if response.status == 200 and response.headers.get_content_type() == "text/event-stream":
            return self._events(response)
        try:
            payload = self._json(response)
        finally:
            response.close()
        return iter([("answer", payload)])

    def _open(self, method: str, path: str, body: Mapping[str, Any] | None, accept: str) -> Any:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json", "Accept": accept}
        if self._session:
            headers["X-Looper-Session"] = self._session
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            response = self._opener.open(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:  # a non-2xx answer; it reads like a response
            response = exc
        except (OSError, http.client.HTTPException) as exc:
            raise self._unreachable(exc) from None
        status = response.status
        if 300 <= status < 400:
            response.close()
            raise Unreachable(f"Looper at {self.url} answered a redirect ({status}); loopercli does not follow "
                              f"redirects, so the token never leaves {self.url}")
        if status == 401:
            response.close()
            raise Unreachable("Looper refused the token (revoked?): create a new one in Settings → API clients "
                              "and run loopercli login")
        return response

    def _json(self, response: Any) -> dict[str, Any]:
        try:
            payload = json.loads(response.read())
        except (OSError, http.client.HTTPException) as exc:
            raise self._unreachable(exc) from None
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise Unreachable(f"Looper at {self.url} answered HTTP {response.status} without a JSON object; "
                              "is that a Looper?")
        return payload

    def _events(self, response: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        event, data = "message", []
        try:
            with contextlib.closing(response):
                for raw in response:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if data:
                            yield event, _event_data("\n".join(data))
                        event, data = "message", []
                    elif not line.startswith(":"):
                        name, _, value = line.partition(":")
                        value = value[1:] if value.startswith(" ") else value
                        if name == "event":
                            event = value
                        elif name == "data":
                            data.append(value)
                if data:
                    yield event, _event_data("\n".join(data))
        except (OSError, http.client.HTTPException) as exc:
            raise self._unreachable(exc) from None

    def _unreachable(self, exc: Exception) -> Unreachable:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, TimeoutError):
            reason = "timed out"
        return Unreachable(f"cannot reach Looper at {self.url} ({reason}); is looper serve running?")


def _event_data(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {"text": text}
    return value if isinstance(value, dict) else {"data": value}


# --- the answers ---


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))


def _cell(value: Any, named: bool) -> str:
    text = "" if named and value is None else str(value)
    return _clip(text.replace("\n", " "), CELL_CHARS)


def _table(rows: list[dict[str, Any]], columns: Sequence[str] | None = None, indent: str = "  ") -> list[str]:
    """``rows`` as aligned lines; without ``columns``, the scalar keys of the first row (at most ``TABLE_COLUMNS``)."""
    named = columns is not None
    if columns is None:
        columns = [key for key, value in rows[0].items() if _is_scalar(value)][:TABLE_COLUMNS]
    if not columns:
        return []
    cells = [[_cell(row.get(column, ""), named) for column in columns] for row in rows]
    widths = [max(len(column), *(len(row[i]) for row in cells)) for i, column in enumerate(columns)]
    lines = ["  ".join(text.ljust(width) for text, width in zip(row, widths)).rstrip() for row in (columns, *cells)]
    return [indent + line for line in lines]


def _ok_blocks(answer: Mapping[str, Any]) -> list[str]:
    result = answer.get("result")
    blocks: list[str] = []
    if isinstance(result, dict):
        lines = [f"{key}: {value}" for key, value in result.items() if _is_scalar(value)]
        for key, value in result.items():
            if _is_scalar(value):
                continue
            if key == "plan" and "import_id" in result:
                # Keep every import change, missing credential and migration note visible.
                lines.append(f"{key}: " + json.dumps(value, ensure_ascii=False, indent=2))
                continue
            table = _table(value) if isinstance(value, list) and value and all(
                isinstance(item, dict) for item in value) else []
            if table:
                lines += [f"{key}:", *table]
                if key == "runs":
                    lines += [f"  {item['run_id']} log: " + json.dumps(item["log"], ensure_ascii=False)
                              for item in value if item.get("run_id") and isinstance(item.get("log"), dict)]
            else:
                lines.append(f"{key}: " + _clip(json.dumps(value, ensure_ascii=False), LINE_CHARS))
        if lines:
            blocks.append("\n".join(lines))
    else:
        blocks.append(json.dumps(result, ensure_ascii=False, indent=2))
    notes = answer.get("notes")
    blocks += [str(note) for note in notes] if isinstance(notes, list) else []
    return blocks


def render(answer: Mapping[str, Any], session: str | None = None) -> None:
    """Print an answer for a person: ``ok`` and ``needs_confirmation`` on stdout, ``error`` on stderr."""
    status = answer.get("status")
    if status == "ok":
        blocks = _ok_blocks(answer)
        if blocks:
            print("\n\n".join(blocks))
    elif status == "needs_confirmation":
        # A confirmation belongs to its session: the printed command must name it, or copying it fails.
        flag = f"--session {shlex.quote(session)} " if session else ""
        print(f"{answer.get('impact', '')}\n"
              f"Confirm with: loopercli {flag}confirm {answer.get('confirmation_id')}\n"
              f"(expires in {answer.get('expires_in_seconds')} s; same client and session)")
    else:
        error = answer.get("error")
        error = error if isinstance(error, dict) else {}
        _print_error(f"{error.get('code', 'unknown')}: {error.get('message', json.dumps(answer, ensure_ascii=False))}")


def finish(status: int, answer: Mapping[str, Any], as_json: bool, session: str | None = None) -> int:
    """Print the answer and give the exit code it decides."""
    if as_json:
        print(json.dumps(answer, ensure_ascii=False, indent=2))
    else:
        render(answer if "status" in answer else {
            "status": "error", "error": {"code": "unavailable", "message": f"HTTP {status}"}}, session)
    kind = answer.get("status")
    if kind == "ok":
        return EXIT_OK
    return EXIT_CONFIRM if kind == "needs_confirmation" else EXIT_ERROR


# --- the commands ---


@dataclass
class Context:
    client_file: Path
    session: str | None
    as_json: bool
    _api: Api | None = field(default=None, repr=False)
    operator: bool = False

    def api(self) -> Api:
        if self._api is None:
            url, token = read_client(self.client_file)
            self._api = Api(url, token, self.session)
        return self._api


def _print_error(message: str) -> None:
    # An argument the user mistook for an option's value may be a token; do not echo it.
    print("error: " + re.sub(rf"{TOKEN_PREFIX}[A-Za-z0-9_-]+", f"{TOKEN_PREFIX}…", message), file=sys.stderr)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # type: ignore[override]
        # An argument the user mistook for an option's value may be a token; do not echo it.
        if self.prog.startswith(("loopercli auth", "loopercli clients")):
            raise UsageError("invalid credential arguments; tokens and passwords are read privately, never as arguments",
                             self.format_usage())
        raise UsageError(re.sub(rf"{TOKEN_PREFIX}[A-Za-z0-9_-]+", f"{TOKEN_PREFIX}…", message), self.format_usage())


@dataclass(frozen=True)
class HandCommand:
    name: str
    help: str
    add: Callable[[Parser], Any]
    run: Callable[[argparse.Namespace, Context], int]


def _read_token() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("API client token: ").strip()
    return sys.stdin.readline().strip()


def _login_arguments(parser: Parser) -> None:
    parser.add_argument("--url", default=DEFAULT_URL, help="the address of the dashboard (default: %(default)s)")


def _login(args: argparse.Namespace, ctx: Context) -> int:
    try:
        url = check_url(args.url)
    except ClientError as exc:
        raise UsageError(str(exc)) from None
    token = _read_token()
    if not token.startswith(TOKEN_PREFIX):
        raise UsageError(f"an API client token starts with {TOKEN_PREFIX}: create one in Settings → API clients; "
                         "it is read from stdin (or a prompt), never from an argument")
    status, answer = Api(url, token, ctx.session).call("GET", f"{API_PREFIX}/whoami")
    result = answer.get("result") if isinstance(answer.get("result"), dict) else {}
    if status != 200 or answer.get("status") != "ok":
        error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
        raise Unreachable(f"Looper at {url} did not accept the token: {error.get('message') or f'HTTP {status}'}")
    client = result.get("client")
    if result.get("principal") != "client" or not isinstance(client, dict):
        raise Unreachable(f"Looper at {url} does not know that token as an API client's")
    write_client(ctx.client_file, url, token)
    print(f'signed in as API client "{client.get("name")}"; wrote {ctx.client_file}')
    return EXIT_OK


def _confirm_arguments(parser: Parser) -> None:
    parser.add_argument("confirmation_id", metavar="ID", help="the id a call answered needs_confirmation with")


def _confirm(args: argparse.Namespace, ctx: Context) -> int:
    status, answer = ctx.api().call("POST", f"{API_PREFIX}/confirm", {"confirmation_id": args.confirmation_id})
    return finish(status, answer, ctx.as_json, ctx.session)


def _online_operation(ctx: Context, name: str) -> Generated:
    """Resolve even hand-written convenience commands through the running service's OpenAPI document."""
    head, _, tail = name.replace("_", "-").partition(".")
    for command, sub, _, _, operation in generated_operations(_fetch_document(ctx)):
        if (command, sub) == (head, tail):
            return operation
    raise UsageError(f"this Looper version has no {name} operation; upgrade the service")


def _operator_arguments(parser: Parser) -> None:
    parser.add_argument("--url", default=DEFAULT_URL, help="the running service's loopback address")
    parser.add_argument("--operator-token-file", default="-", metavar="PATH",
                        help="operator token file (0600); default '-' reads one line from stdin")


def _auth_arguments(parser: Parser) -> None:
    commands = parser.add_subparsers(dest="auth_command", required=True)
    for name in ("set-password", "clear-password"):
        sub = commands.add_parser(name)
        _operator_arguments(sub)
        if name == "set-password":
            sub.add_argument("--user", default="admin")


def _use_operator(args: argparse.Namespace, ctx: Context) -> None:
    url = check_url(args.url)
    if args.operator_token_file == "-":
        token = (getpass.getpass("Operator token: ") if sys.stdin.isatty() else sys.stdin.readline()).strip()
    else:
        try:
            token = _read_private(Path(args.operator_token_file)).decode().strip()
        except UnicodeError:
            raise ClientError("the operator token file is not UTF-8") from None
    if not token or token.startswith(TOKEN_PREFIX) or any(char.isspace() for char in token):
        raise UsageError("an operator token is required; an API client token cannot manage credentials")
    # This Context lives only for this invocation; never write operator credentials to client.json.
    ctx._api = Api(url, token, ctx.session)
    ctx.operator = True


def _auth(args: argparse.Namespace, ctx: Context) -> int:
    _use_operator(args, ctx)
    body = {}
    if args.auth_command == "set-password":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("New password: ")
                if getpass.getpass("Repeat it: ") != password:
                    raise UsageError("the passwords differ; nothing changed")
        except (getpass.GetPassWarning, EOFError):
            raise UsageError("password input needs a terminal with echo disabled; nothing changed") from None
        body = {"user": args.user, "password": password}
    op = _online_operation(ctx, "auth." + args.auth_command.replace("-", "_"))
    status, answer = ctx.api().call(op.method, op.path, body)
    return finish(status, answer, ctx.as_json, ctx.session)


def _clients_arguments(parser: Parser) -> None:
    commands = parser.add_subparsers(dest="clients_command", required=True)
    for action, field_name in (("list", None), ("create", "name"), ("revoke", "client_id")):
        sub = commands.add_parser(action)
        _operator_arguments(sub)
        sub.add_argument("--body", metavar="PATH", default=argparse.SUPPRESS,
                         help="a JSON object (file, or '-' for stdin) used as the base; options override its keys")
        if field_name:
            _add_property(sub, field_name, {"type": "string"})


def _clients(args: argparse.Namespace, ctx: Context) -> int:
    _use_operator(args, ctx)
    args._generated = _online_operation(ctx, "clients." + args.clients_command)
    return _run_generated(args, ctx)


def _doctor_arguments(parser: Parser) -> None:
    pass


def _doctor(args: argparse.Namespace, ctx: Context) -> int:
    op = _online_operation(ctx, "system.check")
    status, answer = ctx.api().call(op.method, op.path, {})
    code = finish(status, answer, ctx.as_json, ctx.session)
    if code == EXIT_OK and any(check.get("status") == "fail"
                               for check in answer.get("result", {}).get("checks", [])):
        return EXIT_ERROR
    return code


def _config_arguments(parser: Parser) -> Any:
    commands = parser.add_subparsers(dest="config_command", required=True)
    export = commands.add_parser(
        "export", help="Export configuration as JSON to a file or stdout")
    export.add_argument("file", help="destination JSON file ('-' writes stdout)")
    export.add_argument("--include-projects", action="store_true")
    export.add_argument("--include-secrets", action="store_true")
    _operator_arguments(export)
    import_ = commands.add_parser(
        "import", help="Preview configuration; request confirmation to apply, or cancel")
    import_.add_argument("file", nargs="?", help="configuration JSON file ('-' reads stdin); preview only")
    _operator_arguments(import_)
    actions = import_.add_mutually_exclusive_group()
    actions.add_argument("--apply", metavar="IMPORT_ID", help="request confirmation to apply a staged import")
    actions.add_argument("--cancel", metavar="IMPORT_ID", help="cancel a staged import")
    return commands  # OpenAPI may add subcommands while export/import keep their existing parsers.


def _config(args: argparse.Namespace, ctx: Context) -> int:
    if args.config_command == "export":
        name = "config.export"
        body = {"include_projects": args.include_projects, "include_secrets": args.include_secrets}
    elif args.apply or args.cancel:
        if args.file:
            raise UsageError("use either FILE to preview, --apply IMPORT_ID or --cancel IMPORT_ID")
        name = "config.import_apply" if args.apply else "config.import_cancel"
        body = {"import_id": args.apply or args.cancel}
    else:
        if not args.file:
            raise UsageError("config import needs FILE to preview, --apply IMPORT_ID or --cancel IMPORT_ID")
        try:
            document = json.loads(_read_text(args.file, "config import"))
        except ValueError:
            raise UsageError("the configuration file must contain a JSON object") from None
        if not isinstance(document, dict):
            raise UsageError("the configuration file must contain a JSON object")
        name, body = "config.import_preview", {"document": document}
    op = _online_operation(ctx, name)
    status, answer = ctx.api().call(op.method, op.path, body)
    if args.config_command != "export" or answer.get("status") != "ok":
        return finish(status, answer, ctx.as_json, ctx.session)
    raw = json.dumps(answer["result"]["document"], ensure_ascii=False, indent=2) + "\n"
    if args.file == "-":
        print(raw, end="")
        return EXIT_OK
    destination = Path(args.file)
    # Exports may contain secrets. Exclusive creation avoids overwriting files or following symlinks.
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise UsageError(f"cannot write the export file: {exc.strerror}") from None
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except OSError:
            os.close(fd)
            raise
        with handle:
            handle.write(raw)
    except (OSError, UnicodeError) as exc:
        destination.unlink(missing_ok=True)
        raise UsageError(f"cannot write the export file: {getattr(exc, 'strerror', None) or exc}") from None
    return finish(status, {"status": "ok", "result": {"path": str(destination)}}, ctx.as_json, ctx.session)


# --- chat ---


@dataclass
class Turn:
    """What one message to the Manager came to."""

    conversation_id: str | None
    message_id: str | None = None
    reply: str | None = None
    outcome: str = "incomplete"
    code: int = EXIT_OK


def _chat_arguments(parser: Parser) -> None:
    parser.add_argument("message", metavar="MESSAGE", nargs="?", help="what to say ('-' reads stdin)")
    parser.add_argument("--file", metavar="PATH", help="say the content of a file ('-' reads stdin)")
    parser.add_argument("--conversation", metavar="ID", help="continue this conversation (it is printed on stderr)")
    parser.add_argument("--manager", metavar="ID", help="talk to this Manager")
    parser.add_argument("--language", choices=("zh", "en"), help="the language of the Manager's reply")
    parser.add_argument("--read", action="store_true", help="print the messages of --conversation instead of sending")
    parser.add_argument("--after", metavar="MESSAGE_ID", help="with --read: only the messages after this one")
    parser.add_argument("--list", action="store_true", help="list your conversations")
    parser.add_argument("--follow", action="store_true",
                        help="after sending or --read, keep printing new messages until Ctrl-C")


def _chat_text(args: argparse.Namespace) -> str | None:
    """The text to send; ``None`` when the user types it in a prompt."""
    if args.file is not None:
        text = _read_text(args.file, "--file")
    elif args.message == "-":
        text = _read_text("-", "message")
    elif args.message is not None:
        text = args.message
    elif sys.stdin.isatty():
        return None
    else:
        text = _read_text("-", "message")
    if not text.strip():
        raise UsageError("the message is empty")
    if len(text) > MAX_MESSAGE_CHARS:
        raise UsageError(f"the message has {len(text)} characters; at most {MAX_MESSAGE_CHARS}: "
                         "summarise it and point to the files")
    return text


def _answer_error(answer: Mapping[str, Any], status: int) -> CliError:
    error = answer.get("error")
    error = error if isinstance(error, dict) else {}
    return CliError(f"{error.get('code', 'unavailable')}: {error.get('message') or f'HTTP {status}'}")


def _send(args: argparse.Namespace, ctx: Context, text: str, conversation_id: str | None) -> Turn:
    """Send ``text`` and print the Manager's reply as it streams in."""
    body: dict[str, Any] = {"text": text}
    for key, value in (("conversation_id", conversation_id), ("manager_id", args.manager),
                       ("language", args.language)):
        if value:
            body[key] = value
    turn = Turn(conversation_id)
    printed = ""
    quiet = ctx.as_json
    events = ctx.api().stream(f"{API_PREFIX}/chat/send", body)
    try:
        for event, data in events:
            if event == "answer":  # the server answered an error instead of a stream
                turn.outcome, turn.code = "error", finish(0, data, ctx.as_json, ctx.session)
                return turn
            if event == "accepted":
                turn.conversation_id = data.get("conversation_id") or turn.conversation_id
                turn.message_id = data.get("message_id")
                print(f"conversation: {turn.conversation_id}", file=sys.stderr, flush=True)
            elif event == "delta":
                chunk = str(data.get("text", ""))
                printed += chunk
                if not quiet:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
            elif event == "status":
                if not quiet:
                    print(f"[{data.get('label', '')}]", file=sys.stderr, flush=True)
            elif event == "final":
                turn.reply = str(data.get("text", ""))
                turn.outcome = str(data.get("outcome") or "answered")
                turn.code = EXIT_ERROR if turn.outcome == "failed" else EXIT_OK
                if not quiet:
                    # Looper may append its own blocks after the Manager's words (a confirmation request).
                    rest = turn.reply[len(printed):] if turn.reply.startswith(printed) else "\n" + turn.reply
                    print(rest)
                return turn
            elif event == "timeout":
                turn.outcome = "timeout"
                if not quiet:
                    if printed:
                        print()
                    print(data.get("message", "the reply is still being written"), file=sys.stderr)
                return turn
    except KeyboardInterrupt as exc:
        exc.turn = turn
        raise
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()
    if not quiet and printed:
        print()
    _print_error("the stream ended without a reply; read it later with: "
                 f"loopercli chat --conversation {turn.conversation_id} --read")
    turn.code = EXIT_ERROR
    return turn


def _print_turn(turn: Turn) -> None:
    print(json.dumps({"conversation_id": turn.conversation_id, "message_id": turn.message_id, "reply": turn.reply,
                      "outcome": turn.outcome}, ensure_ascii=False, indent=2))


class _Messages:
    """Prints messages as ``--read`` shows them, a blank line between."""

    def __init__(self, as_json: bool) -> None:
        self.as_json, self.shown = as_json, 0

    def show(self, message: Mapping[str, Any]) -> None:
        if self.as_json:
            print(json.dumps(message, ensure_ascii=False))
        else:
            if self.shown:
                print()
            print(f"{message.get('role')} · {message.get('created_at')}")
            print(message.get("content", ""))
        self.shown += 1
        sys.stdout.flush()


def _pages(ctx: Context, conversation_id: str, after: str | None) -> Iterator[Mapping[str, Any]]:
    """The pages of a conversation after a cursor, following the server's cursor while it has more."""
    while True:
        body = {"conversation_id": conversation_id, **({"after": after} if after else {})}
        status, answer = ctx.api().call("POST", f"{API_PREFIX}/chat/messages", body)
        page = answer.get("result")
        if answer.get("status") != "ok" or not isinstance(page, dict):
            raise _answer_error(answer, status)
        yield page
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor or cursor == after:
            return
        after = cursor


def _chat_read(args: argparse.Namespace, ctx: Context) -> int:
    messages = _Messages(ctx.as_json)
    collected: list[Any] = []
    manager_id, cursor = None, args.after
    for page in _pages(ctx, args.conversation, args.after):
        manager_id = page.get("manager_id", manager_id)
        cursor = page.get("next_cursor") or cursor
        for message in page.get("messages") or []:
            if ctx.as_json:
                collected.append(message)
            else:
                messages.show(message)
    if ctx.as_json:
        print(json.dumps({"conversation_id": args.conversation, "manager_id": manager_id, "messages": collected,
                          "next_cursor": cursor}, ensure_ascii=False, indent=2))
    if args.follow:
        return _follow(ctx, args.conversation, cursor, None, _Messages(ctx.as_json))
    return EXIT_OK


def _follow(ctx: Context, conversation_id: str, cursor: str | None, skip: str | None, messages: _Messages) -> int:
    """Print the new messages of a conversation every few seconds until Ctrl-C.

    ``skip`` is a reply already printed from a stream: the first assistant message with that content is not shown again.
    """
    try:
        while True:
            time.sleep(FOLLOW_SECONDS)
            for page in _pages(ctx, conversation_id, cursor):
                for message in page.get("messages") or []:
                    if skip is not None and message.get("role") == "assistant" and message.get("content") == skip:
                        skip = None
                        continue
                    messages.show(message)
                cursor = page.get("next_cursor") or cursor
    except KeyboardInterrupt:
        return EXIT_OK


def _chat_list(ctx: Context) -> int:
    status, answer = ctx.api().call("POST", f"{API_PREFIX}/chat/conversations", {})
    if ctx.as_json or answer.get("status") != "ok":
        return finish(status, answer, ctx.as_json, ctx.session)
    result = answer.get("result")
    rows = result.get("conversations") if isinstance(result, dict) else None
    if rows:
        print("\n".join(_table(rows, ("conversation_id", "manager_id", "last_message_at", "last_content"), "")))
    else:
        print("no conversations")
    return EXIT_OK


def _chat_interactive(args: argparse.Namespace, ctx: Context) -> int:
    conversation_id = args.conversation
    while True:
        print("> ", end="", file=sys.stderr, flush=True)
        try:
            line = input().strip()
        except EOFError:
            print(file=sys.stderr)
            return EXIT_OK
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return EXIT_OK
        if not line:
            continue
        if len(line) > MAX_MESSAGE_CHARS:
            _print_error(f"the message has {len(line)} characters; at most {MAX_MESSAGE_CHARS}: "
                         "summarise it and point to the files")
            continue
        try:
            turn = _send(args, ctx, line, conversation_id)
        except KeyboardInterrupt as exc:
            turn = getattr(exc, "turn", None)
            if turn is not None:
                conversation_id = turn.conversation_id or conversation_id
            print(file=sys.stderr)
            where = f" --conversation {conversation_id} --read" if conversation_id else ""
            print(f"(stopped reading; the reply is still saved: loopercli chat{where})", file=sys.stderr)
            continue
        conversation_id = turn.conversation_id or conversation_id
        if ctx.as_json:
            _print_turn(turn)


def _chat(args: argparse.Namespace, ctx: Context) -> int:
    if args.list and args.read:
        raise UsageError("--list and --read exclude each other")
    if args.list:
        return _chat_list(ctx)
    if args.read:
        if not args.conversation:
            raise UsageError("--read needs --conversation ID")
        return _chat_read(args, ctx)
    if args.after:
        raise UsageError("--after goes with --read")
    text = _chat_text(args)
    if text is None:
        if args.follow:
            raise UsageError("--follow needs a message or --read")
        return _chat_interactive(args, ctx)
    turn = _send(args, ctx, text, args.conversation)
    if ctx.as_json and turn.outcome != "error":
        _print_turn(turn)
    if args.follow and turn.code == EXIT_OK and turn.conversation_id:
        return _follow(ctx, turn.conversation_id, turn.message_id, turn.reply, _Messages(ctx.as_json))
    return turn.code


# --- wait ---


def _wait_arguments(parser: Parser) -> None:
    parser.add_argument("task_id", metavar="TASK")
    parser.add_argument("--for", dest="until", choices=("any", "end", "gate"), default="any",
                        help="what to wait for: a gate that needs you, the task's end, or whichever comes first")
    parser.add_argument("--timeout", type=int, default=0, metavar="SECONDS",
                        help="give up after this long (default: never)")
    parser.add_argument("--interval", type=int, default=WAIT_INTERVAL_SECONDS, metavar="SECONDS",
                        help="between two looks at the task (default %(default)s, at least "
                             f"{WAIT_MIN_INTERVAL_SECONDS})")


_WAIT_GROUPS = {"gate": ("needs_you",), "end": ("attention", "completed", "cancelled"),
                "any": ("needs_you", "attention", "completed", "cancelled")}


def _wait(args: argparse.Namespace, ctx: Context) -> int:
    interval = max(args.interval, WAIT_MIN_INTERVAL_SECONDS)
    started = time.monotonic()
    while True:
        status, answer = ctx.api().call("POST", f"{API_PREFIX}/ops/tasks/get", {"task_id": args.task_id})
        result = answer.get("result")
        if answer.get("status") != "ok" or not isinstance(result, dict):
            return finish(status, answer, ctx.as_json, ctx.session)
        if result.get("group") in _WAIT_GROUPS[args.until]:
            if ctx.as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return EXIT_OK
            print(f"task {args.task_id} {result.get('status')} ({result.get('group')})")
            if result.get("group") == "needs_you":
                for gate in result.get("gates") or []:
                    if isinstance(gate, dict) and gate.get("waiting_for") == "user":
                        print(f"gate {gate.get('gate')}: {gate.get('topic')}\n"
                              f"  loopercli gates messages --task-id {args.task_id} --gate {gate.get('gate')}")
            elif result.get("result"):
                print(f"result: {result['result']}")
            return EXIT_OK
        if args.timeout and time.monotonic() - started >= args.timeout:
            if ctx.as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"still {result.get('status')} after {args.timeout} s", file=sys.stderr)
            return EXIT_WAIT_TIMEOUT
        remaining = args.timeout - (time.monotonic() - started) if args.timeout else interval
        time.sleep(min(interval, max(remaining, 0)))


# --- skill ---


def _skill_directory() -> Path:
    directory = Path(str(importlib.resources.files("looper") / "skill" / "looper-skill"))
    if not (directory / "SKILL.md").is_file():
        raise CliError(f"the packaged skill is missing from {directory}")
    return directory


def _skill_arguments(parser: Parser) -> None:
    operations = parser.add_subparsers(dest="skill_operation", required=True, metavar="OPERATION")
    operations.add_parser("path", help="print the directory of the packaged skill")
    operations.add_parser("show", help="print the skill's SKILL.md")


def _skill(args: argparse.Namespace, ctx: Context) -> int:
    directory = _skill_directory()
    if args.skill_operation == "path":
        print(directory)
    else:
        sys.stdout.write((directory / "SKILL.md").read_text(encoding="utf-8"))
    return EXIT_OK


HAND_COMMANDS: dict[str, HandCommand] = {command.name: command for command in (
    HandCommand("auth", "Manage the password using a transient operator token", _auth_arguments, _auth),
    HandCommand("clients", "Manage API clients using a transient operator token", _clients_arguments, _clients),
    HandCommand("doctor", "Run the service's self checks, including pg_dump", _doctor_arguments, _doctor),
    HandCommand("config", "Export or preview and apply configuration through the running service",
                _config_arguments, _config),
    HandCommand("login", "Save an API client's token (read from stdin or a prompt) after checking it with Looper",
                _login_arguments, _login),
    HandCommand("confirm", "Run the call that answered needs_confirmation", _confirm_arguments, _confirm),
    HandCommand("chat", "Talk to the Manager, read a conversation, or list your conversations", _chat_arguments,
                _chat),
    HandCommand("wait", "Wait until a task needs you, ends, or the timeout passes (exit 6)", _wait_arguments, _wait),
    HandCommand("skill", "Print the directory (path) or the text (show) of the skill packaged with Looper",
                _skill_arguments, _skill),
)}


@dataclass(frozen=True)
class Generated:
    """A command made from an operation of the document."""

    method: str
    path: str
    properties: Mapping[str, Mapping[str, Any]]
    has_body: bool
    response_media_type: str = "application/json"
    operator_only: bool = False


def generated_operations(document: Mapping[str, Any]) -> list[tuple[str, str, str, str, Generated]]:
    """``(command, sub-command, summary, description, operation)`` for each operation without a hand-written command."""
    servers = document.get("servers")
    prefix = servers[0].get("url", API_PREFIX) if isinstance(servers, list) and servers else API_PREFIX
    found = []
    for path, item in (document.get("paths") or {}).items():
        for method, operation in item.items():
            if not isinstance(operation, dict) or operation.get("x-looper-cli-hand") or "operationId" not in operation:
                continue
            head, _, tail = str(operation["operationId"]).partition(".")
            schema = (((operation.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
                      ).get("schema")
            properties = schema.get("properties") if isinstance(schema, dict) else None
            media_types = [media for status, response in (operation.get("responses") or {}).items()
                           if str(status).startswith("2") for media in response.get("content", {})]
            media_type = "text/event-stream" if "text/event-stream" in media_types else next(
                iter(media_types), "application/json")
            found.append((head.replace("_", "-"), tail.replace("_", "-"), operation.get("summary") or "",
                          operation.get("description") or "",
                          Generated(method.upper(), prefix + path, properties or {}, schema is not None, media_type,
                                    operation.get("x-looper-operator-only") is True)))
    return found


def _escape(text: str) -> str:
    return text.replace("%", "%%")


def _json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON ({exc})") from None


def _add_property(parser: Parser, name: str, schema: Mapping[str, Any]) -> None:
    flag = "--" + name.replace("_", "-")
    help_text = _escape(str(schema.get("description") or ""))
    kind = schema.get("type")
    common: dict[str, Any] = {"dest": name, "default": argparse.SUPPRESS, "help": help_text or None}
    if kind == "string":
        group = parser.add_mutually_exclusive_group()
        choices = schema.get("enum") if isinstance(schema.get("enum"), list) else None
        group.add_argument(flag, choices=choices, **common)
        group.add_argument(f"{flag}-file", dest=f"{name}__file", metavar="PATH", default=argparse.SUPPRESS,
                           help=f"read {flag} from a file ('-' reads stdin)")
    elif kind == "integer":
        parser.add_argument(flag, type=int, **common)
    elif kind == "number":
        parser.add_argument(flag, type=float, **common)
    elif kind == "boolean":
        parser.add_argument(flag, action=argparse.BooleanOptionalAction, **common)
    else:
        common["help"] = (help_text + " " if help_text else "") + "(JSON)"
        parser.add_argument(flag, type=_json_value, metavar="JSON", **common)


def _add_operation(parser: Parser, operation: Generated) -> None:
    parser.set_defaults(_generated=operation)
    if operation.operator_only:
        if "url" in operation.properties:
            parser.add_argument("--server-url", dest="_operator_url", default=DEFAULT_URL,
                                help="the running service's loopback address")
            parser.add_argument("--operator-token-file", default="-", metavar="PATH",
                                help="operator token file (0600); default '-' reads one line from stdin")
        else:
            _operator_arguments(parser)
    if operation.method == "GET" or not operation.has_body:
        return
    parser.add_argument("--body", metavar="PATH", default=argparse.SUPPRESS,
                        help="a JSON object (file, or '-' for stdin) used as the base; options override its keys")
    for name, schema in operation.properties.items():
        if name not in ("body", "help"):
            _add_property(parser, name, schema if isinstance(schema, Mapping) else {})


def _read_text(source: str, what: str) -> str:
    try:
        if source == "-":
            return sys.stdin.read()
        with open(source, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"{what}: cannot read {source}: {getattr(exc, 'strerror', None) or exc}") from None


def request_body(args: argparse.Namespace, operation: Generated) -> dict[str, Any]:
    """The JSON body: ``--body`` as the base, the options on top of it."""
    values = vars(args)
    file_options = {name: values[f"{name}__file"] for name in operation.properties if f"{name}__file" in values}
    sources = [*file_options.values(), *([values["body"]] if "body" in values else [])]
    if sources.count("-") > 1:
        raise UsageError("only one option may read stdin ('-')")
    body: dict[str, Any] = {}
    if "body" in values:
        try:
            base = json.loads(_read_text(values["body"], "--body"))
        except ValueError as exc:
            raise UsageError(f"--body: not valid JSON ({exc})") from None
        if not isinstance(base, dict):
            raise UsageError("--body: must be a JSON object")
        body.update(base)
    for name in operation.properties:
        if name in values:
            body[name] = values[name]
        elif name in file_options:
            body[name] = _read_text(file_options[name], "--" + name.replace("_", "-") + "-file")
    return body


def _run_generated(args: argparse.Namespace, ctx: Context) -> int:
    operation: Generated = args._generated
    if operation.operator_only and not ctx.operator:
        raise UsageError("an operator token is required; use --url and --operator-token-file")
    body = request_body(args, operation) if operation.method == "POST" else None
    if operation.response_media_type == "text/event-stream":
        with contextlib.ExitStack() as cleanup:
            events = ctx.api().stream(operation.path, body or {})
            if close := getattr(events, "close", None):
                cleanup.callback(close)
            for event, data in events:
                if event == "answer":
                    return finish(0, data, ctx.as_json, ctx.session)
                if ctx.as_json:
                    print(json.dumps({"event": event, "data": data}, ensure_ascii=False), flush=True)
                else:
                    print(f"{event}: {json.dumps(data, ensure_ascii=False)}", flush=True)
        return EXIT_OK
    status, answer = ctx.api().call(operation.method, operation.path, body)
    return finish(status, answer, ctx.as_json, ctx.session)


def _run_task_alias(args: argparse.Namespace, ctx: Context) -> int:
    """``start``, ``stop`` and ``status TASK``: the generated operation with the task's id."""
    operation: Generated = args._generated
    status, answer = ctx.api().call(operation.method, operation.path, {"task_id": args.task})
    return finish(status, answer, ctx.as_json, ctx.session)


def _run_status(args: argparse.Namespace, ctx: Context) -> int:
    if args.task:
        return _run_task_alias(args, ctx)
    overview: Generated | None = args._overview
    if overview is None:
        raise UsageError("status needs a TASK: Looper's API description has no overview")
    status, answer = ctx.api().call(overview.method, overview.path, {})
    return finish(status, answer, ctx.as_json, ctx.session)


PIPELINE_COLUMNS = ("step", "kind", "status", "depends_on", "role", "instruction")
GATE_COLUMNS = ("gate", "state", "waiting_for", "topic")
WAIT_COLUMNS = ("step", "state", "outcome", "topic", "check", "next_check_at")


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _render_pipeline(result: Mapping[str, Any]) -> None:
    blocks = ["\n".join(f"{key}: {value}" for key, value in result.items() if _is_scalar(value))]
    steps = [{**step, "depends_on": ",".join(map(str, step.get("depends_on") or []))}
             for step in _rows(result.get("steps"))]
    for title, rows, columns in (("steps", steps, PIPELINE_COLUMNS),
                                 ("gates", _rows(result.get("gates")), GATE_COLUMNS),
                                 ("waits", _rows(result.get("waits")), WAIT_COLUMNS)):
        if rows:
            blocks.append("\n".join([f"{title}:", *_table(rows, columns)]))
        elif title == "steps":
            blocks.append("steps: none")
    print("\n\n".join(block for block in blocks if block))


def _run_pipeline(args: argparse.Namespace, ctx: Context) -> int:
    operation: Generated = args._generated
    status, answer = ctx.api().call(operation.method, operation.path, {"task_id": args.task})
    result = answer.get("result")
    if ctx.as_json or answer.get("status") != "ok" or not isinstance(result, dict):
        return finish(status, answer, ctx.as_json, ctx.session)
    _render_pipeline(result)
    return EXIT_OK


def _add_aliases(commands: Any, found: Mapping[tuple[str, str], Generated], summaries: Mapping[tuple[str, str], str],
                 taken: set[str]) -> None:
    """The short names of the commands used most; each needs its operation in the document."""

    def alias(name: str, key: tuple[str, str], help_text: str) -> Parser | None:
        if name in taken or key not in found:
            return None
        taken.add(name)
        return commands.add_parser(name, help=_escape(help_text), description=summaries.get(key) or help_text)

    for name, key, help_text in (("create", ("task-create", ""), "Create a task (the options of task-create)"),
                                 ("list", ("tasks", "list"), "List tasks (the options of tasks list)")):
        if (sub := alias(name, key, help_text)) is not None:
            _add_operation(sub, found[key])
            sub.set_defaults(_run=_run_generated)
    for name, key, help_text, run in (
            ("start", ("task-control", "start"), "Start a ready task", _run_task_alias),
            ("stop", ("task-control", "stop"), "Stop a running task", _run_task_alias),
            ("pipeline", ("task-pipeline", ""), "A task's steps, gates and waits", _run_pipeline)):
        if (sub := alias(name, key, help_text)) is not None:
            sub.add_argument("task", metavar="TASK", help="the task's id")
            sub.set_defaults(_run=run, _generated=found[key])
    if (sub := alias("status", ("tasks", "get"), "One task's state, or the overview without a TASK")) is not None:
        sub.add_argument("task", metavar="TASK", nargs="?", help="the task's id (without it: the overview)")
        sub.set_defaults(_run=_run_status, _generated=found[("tasks", "get")], _overview=found.get(("overview", "")))


def build_parser(document: Mapping[str, Any] | None) -> Parser:
    """The parser of every command: the hand-written ones, and those the ``document`` (when there is one) generates."""
    parser = Parser(prog="loopercli", description="Operate the Looper on this machine from the command line.",
                    epilog="Global options, in any position: --client-file PATH, --session ID ($LOOPER_SESSION), "
                           "--json, --version. Exit codes: 0 ok, 1 error, 2 usage, 3 needs confirmation "
                           "(run loopercli confirm ID), 4 Looper unreachable or the client file unusable, "
                           "6 wait timed out.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    hand_groups: dict[str, Any] = {}
    for hand in HAND_COMMANDS.values():
        sub = commands.add_parser(hand.name, help=_escape(hand.help), description=hand.help)
        children = hand.add(sub)
        if children is not None:
            hand_groups[hand.name] = children
        sub.set_defaults(_run=hand.run)
    parents: dict[str, Any] = {}
    found: dict[tuple[str, str], Generated] = {}
    summaries: dict[tuple[str, str], str] = {}
    for head, tail, summary, description, operation in generated_operations(document or {"paths": {}}):
        if head in HAND_COMMANDS:
            children = hand_groups.get(head)
            if children is not None and tail and tail not in children.choices:
                sub = children.add_parser(tail, help=_escape(summary), description=description)
                _add_operation(sub, operation)
                if head == "config" and not operation.operator_only:
                    _operator_arguments(sub)
                sub.set_defaults(_run=_run_generated)
            continue
        found[head, tail], summaries[head, tail] = operation, summary
        if not tail:
            sub = commands.add_parser(head, help=_escape(summary), description=description)
            _add_operation(sub, operation)
            sub.set_defaults(_run=_run_generated)
            continue
        if head not in parents:
            group = commands.add_parser(head, help=f"{head} operations (see: loopercli {head} --help)")
            parents[head] = group.add_subparsers(dest=f"{head}__sub", required=True, metavar="OPERATION")
        sub = parents[head].add_parser(tail, help=_escape(summary), description=description)
        _add_operation(sub, operation)
        sub.set_defaults(_run=_run_generated)
    _add_aliases(commands, found, summaries, set(HAND_COMMANDS) | set(commands.choices))
    return parser


def _global_parser() -> argparse.ArgumentParser:
    parser = Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--client-file")
    parser.add_argument("--session", default=os.environ.get("LOOPER_SESSION") or None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="store_true")
    return parser


def _fetch_document(ctx: Context) -> dict[str, Any]:
    status, document = ctx.api().call("GET", f"{API_PREFIX}/openapi.json")
    if status != 200 or not isinstance(document.get("paths"), dict):
        raise Unreachable(f"Looper at {ctx.api().url} did not serve its API description (HTTP {status})")
    return document


def _needs_document(rest: list[str]) -> bool:
    hand = HAND_COMMANDS.get(rest[0]) if rest else None
    if hand is None:
        return True
    children = hand.add(Parser(add_help=False))
    if children is None or len(rest) < 2:
        return False
    child = rest[1]
    # Group help discovers API children; known hand-written children and other flags stay offline.
    return child in ("-h", "--help") or (not child.startswith("-") and child not in children.choices)


def _run(opts: argparse.Namespace, rest: list[str]) -> int:
    ctx = Context(client_file(opts.client_file), opts.session, opts.json)
    document = None
    needs_document = _needs_document(rest)
    if needs_document or (rest and rest[0] == "config"):
        # Discover generated operator commands with a transient token, before a client file is needed.
        # A bare --url may be an operation's request field, so only the credential flag selects this mode.
        if any(part == "--operator-token-file" or part.startswith("--operator-token-file=") for part in rest):
            bootstrap = Parser(add_help=False, allow_abbrev=False)
            _operator_arguments(bootstrap)
            bootstrap.add_argument("--server-url", dest="_operator_url")
            operator_args, _ = bootstrap.parse_known_args(rest)
            if operator_args._operator_url is not None:
                operator_args.url = operator_args._operator_url
            _use_operator(operator_args, ctx)
    if needs_document:
        try:
            document = _fetch_document(ctx)
        except CliError as exc:
            hands = build_parser(None)
            if "-h" in rest or "--help" in rest:
                if rest and rest[0] in HAND_COMMANDS:
                    with contextlib.suppress(SystemExit):  # argparse exits after printing the group's help.
                        hands.parse_args([rest[0], "--help"])
                else:
                    hands.print_help()
                print(f"\n(the commands made from Looper's API are not shown: {exc})")
                return EXIT_OK
            hands.print_help(sys.stderr)
            raise
    try:
        args = build_parser(document).parse_args(rest)
    except UsageError:
        if rest and rest[0] in ("auth", "clients"):
            raise UsageError("invalid credential arguments; tokens and passwords are read privately, never as arguments") from None
        raise
    return args._run(args, ctx)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        opts, rest = _global_parser().parse_known_args(list(sys.argv[1:] if argv is None else argv))
        if opts.version:
            print(f"loopercli {__version__}")
            return EXIT_OK
        return _run(opts, rest)
    except CliError as exc:
        if isinstance(exc, UsageError) and exc.usage:
            print(exc.usage, end="", file=sys.stderr)
        _print_error(str(exc))
        return exc.code
    except SystemExit as exc:  # argparse's --help
        return exc.code if isinstance(exc.code, int) else EXIT_OK
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
