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
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
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
_MAX_CLIENT_FILE_BYTES = 64 * 1024
# Non-blocking, so a FIFO in the file's place can't hang the read before fstat refuses it.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_CONFIRM, EXIT_UNREACHABLE = 0, 1, 2, 3, 4


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


def read_client(path: Path) -> tuple[str, str]:
    """The ``(url, token)`` of the client file; it must be the user's own regular file with mode 0600."""
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


def _table(rows: list[dict[str, Any]]) -> list[str]:
    columns = [key for key, value in rows[0].items() if _is_scalar(value)][:TABLE_COLUMNS]
    if not columns:
        return []
    cells = [[_clip(str(row.get(column, "")).replace("\n", " "), CELL_CHARS) for column in columns] for row in rows]
    widths = [max(len(column), *(len(row[i]) for row in cells)) for i, column in enumerate(columns)]
    lines = ["  ".join(text.ljust(width) for text, width in zip(row, widths)).rstrip() for row in (columns, *cells)]
    return ["  " + line for line in lines]


def _ok_blocks(answer: Mapping[str, Any]) -> list[str]:
    result = answer.get("result")
    blocks: list[str] = []
    if isinstance(result, dict):
        lines = [f"{key}: {value}" for key, value in result.items() if _is_scalar(value)]
        for key, value in result.items():
            if _is_scalar(value):
                continue
            table = _table(value) if isinstance(value, list) and value and all(
                isinstance(item, dict) for item in value) else []
            if table:
                lines += [f"{key}:", *table]
            else:
                lines.append(f"{key}: " + _clip(json.dumps(value, ensure_ascii=False), LINE_CHARS))
        if lines:
            blocks.append("\n".join(lines))
    else:
        blocks.append(json.dumps(result, ensure_ascii=False, indent=2))
    notes = answer.get("notes")
    blocks += [str(note) for note in notes] if isinstance(notes, list) else []
    return blocks


def render(answer: Mapping[str, Any]) -> None:
    """Print an answer for a person: ``ok`` and ``needs_confirmation`` on stdout, ``error`` on stderr."""
    status = answer.get("status")
    if status == "ok":
        blocks = _ok_blocks(answer)
        if blocks:
            print("\n\n".join(blocks))
    elif status == "needs_confirmation":
        print(f"{answer.get('impact', '')}\n"
              f"Confirm with: loopercli confirm {answer.get('confirmation_id')}\n"
              f"(expires in {answer.get('expires_in_seconds')} s; same client and session)")
    else:
        error = answer.get("error")
        error = error if isinstance(error, dict) else {}
        print(f"error: {error.get('code', 'unknown')}: {error.get('message', json.dumps(answer, ensure_ascii=False))}",
              file=sys.stderr)


def finish(status: int, answer: Mapping[str, Any], as_json: bool) -> int:
    """Print the answer and give the exit code it decides."""
    if as_json:
        print(json.dumps(answer, ensure_ascii=False, indent=2))
    else:
        render(answer if "status" in answer else {
            "status": "error", "error": {"code": "unavailable", "message": f"HTTP {status}"}})
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

    def api(self) -> Api:
        if self._api is None:
            url, token = read_client(self.client_file)
            self._api = Api(url, token, self.session)
        return self._api


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # type: ignore[override]
        # An argument the user mistook for an option's value may be a token; do not echo it.
        raise UsageError(re.sub(rf"{TOKEN_PREFIX}\w+", f"{TOKEN_PREFIX}…", message), self.format_usage())


@dataclass(frozen=True)
class HandCommand:
    name: str
    help: str
    add: Callable[[Parser], None]
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
    return finish(status, answer, ctx.as_json)


HAND_COMMANDS: dict[str, HandCommand] = {command.name: command for command in (
    HandCommand("login", "Save an API client's token (read from stdin or a prompt) after checking it with Looper",
                _login_arguments, _login),
    HandCommand("confirm", "Run the call that answered needs_confirmation", _confirm_arguments, _confirm),
)}


@dataclass(frozen=True)
class Generated:
    """A command made from an operation of the document."""

    method: str
    path: str
    properties: Mapping[str, Mapping[str, Any]]
    has_body: bool


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
            found.append((head.replace("_", "-"), tail.replace("_", "-"), operation.get("summary") or "",
                          operation.get("description") or "",
                          Generated(method.upper(), prefix + path, properties or {}, schema is not None)))
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
    body = request_body(args, operation) if operation.method == "POST" else None
    status, answer = ctx.api().call(operation.method, operation.path, body)
    return finish(status, answer, ctx.as_json)


def build_parser(document: Mapping[str, Any] | None) -> Parser:
    """The parser of every command: the hand-written ones, and those the ``document`` (when there is one) generates."""
    parser = Parser(prog="loopercli", description="Operate the Looper on this machine from the command line.",
                    epilog="Global options, in any position: --client-file PATH, --session ID ($LOOPER_SESSION), "
                           "--json, --version. Exit codes: 0 ok, 1 error, 2 usage, 3 needs confirmation "
                           "(run loopercli confirm ID), 4 Looper unreachable or the client file unusable.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for hand in HAND_COMMANDS.values():
        sub = commands.add_parser(hand.name, help=_escape(hand.help), description=hand.help)
        hand.add(sub)
        sub.set_defaults(_run=hand.run)
    parents: dict[str, Any] = {}
    for head, tail, summary, description, operation in generated_operations(document or {"paths": {}}):
        if head in HAND_COMMANDS:
            continue
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
    return parser


def _global_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
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


def _run(opts: argparse.Namespace, rest: list[str]) -> int:
    ctx = Context(client_file(opts.client_file), opts.session, opts.json)
    document = None
    if not (rest and rest[0] in HAND_COMMANDS):
        try:
            document = _fetch_document(ctx)
        except CliError as exc:
            hands = build_parser(None)
            if "-h" in rest or "--help" in rest:
                hands.print_help()
                print(f"\n(the commands made from Looper's API are not shown: {exc})")
                return EXIT_OK
            hands.print_help(sys.stderr)
            raise
    args = build_parser(document).parse_args(rest)
    return args._run(args, ctx)


def main(argv: Sequence[str] | None = None) -> int:
    opts, rest = _global_parser().parse_known_args(list(sys.argv[1:] if argv is None else argv))
    if opts.version:
        print(f"loopercli {__version__}")
        return EXIT_OK
    try:
        return _run(opts, rest)
    except CliError as exc:
        if isinstance(exc, UsageError) and exc.usage:
            print(exc.usage, end="", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except SystemExit as exc:  # argparse's --help
        return exc.code if isinstance(exc.code, int) else EXIT_OK
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
