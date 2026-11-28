"""Does the sandbox really refuse every path on its read deny list, on this machine, under the operator's own HOME?

The real CLI runs as a task Manager's direct session (``operator_resident_command``, the pinned sandbox settings of
``task_manager_sandbox``) against a local scripted Messages server, so no model is called and nothing is billed. The
script is one read-only Bash command per denied path that exists here; each prints ``DENIED``, ``ALLOWED`` or
``OTHER`` and never a file name or a byte of content. The server keeps those markers and nothing else of a request.

The hard verdict on the boundary is only the markers answered inside the sandbox (``DENIED``, ``ALLOWED``, ``OTHER``,
``BLOCKED_BY_HOOK``) and the positive and negative controls; the session must also follow the script and send no other
credential. A watched directory's modification time and number of entries are an auxiliary signal, seen from outside:
a change in a ``CREDENTIAL`` entry is an ``ALARM``; a change in an ``AGENT_STATE`` entry, which running agent
sessions write at any time, is only listed under ``changed_during_probe``.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import threading
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from looper.agents import task_manager_safety, task_manager_sandbox
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.task_manager_sandbox import DenyClass

PROBE_KEY = "sk-probe-not-real"
TIMEOUT_SEC = 420.0
HOOK_ERROR_PREFIX = "PreToolUse:"


class Mark(StrEnum):
    DENIED = "DENIED"
    ALLOWED = "ALLOWED"
    OTHER = "OTHER"
    BLOCKED_BY_HOOK = "BLOCKED_BY_HOOK"  # the command never ran: a hook (or a permission rule) refused it


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"  # an entry was not denied, or a control answered wrongly
    INVALID = "INVALID"  # the probe says nothing: a command was blocked, or the session did not follow the script
    ALARM = "ALARM"  # every answer was right, but a denied credential directory changed while the probe ran
    ABORTED = "ABORTED"  # the CLI's requests were not the probe's: stopped at once, nothing else recorded


class Refusal(StrEnum):
    AUTHORIZATION = "an Authorization header reached the probe server"
    WRONG_KEY = "a request carried another API key, or a model request carried none"


class Kind(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"


class Unverifiable(StrEnum):
    """Why a present entry is not verified: outside the sandbox too it cannot be read, so a refusal inside proves
    nothing about the sandbox. No command is run for it."""

    NO_METADATA = "its metadata cannot be read outside the sandbox"
    NOT_LISTABLE = "it cannot be listed outside the sandbox"
    NOT_READABLE = "it has no read permission outside the sandbox"


# --- The server -------------------------------------------------------------------------------------------------


class MarkerServer:
    """A Messages endpoint on 127.0.0.1 that asks for the scripted commands one at a time. Of a request it keeps the
    marker of each answered command; bodies and headers are dropped. A request of any method with an
    ``Authorization`` header or another API key, or a model request without the probe's key, is answered 401 without
    reading its body, and sets ``refused``."""

    def __init__(self, script: list[tuple[str, str]]) -> None:
        self.script = list(script)
        self.marks: dict[str, Mark] = {}
        self.requests = 0  # model requests that offered Bash: the session's own turns
        self.refusal: Refusal | None = None
        self.refused = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> str:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def reply(self, status: int, body: str, content_type: str = "application/json") -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def parse_request(self) -> bool:
                # Every request is checked here, before it is dispatched by method (an unsupported one included).
                return super().parse_request() and self.admitted()

            def admitted(self) -> bool:
                keys = self.headers.get_all("x-api-key") or []
                model_request = self.command == "POST" and self.path.startswith("/v1/messages")
                refusal = (Refusal.AUTHORIZATION if self.headers.get_all("Authorization") else
                           Refusal.WRONG_KEY if any(key != PROBE_KEY for key in keys) or (model_request and not keys)
                           else None)
                with owner._lock:
                    owner.refusal = owner.refusal or refusal
                    if owner.refusal is None:
                        return True
                owner.refused.set()
                self.close_connection = True
                self.reply(401, "{}")
                return False

            def do_GET(self) -> None:
                self.reply(404, "{}")

            do_HEAD = do_GET

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if path == "/v1/messages/count_tokens":
                    self.reply(200, json.dumps({"input_tokens": 1}))
                elif path != "/v1/messages":
                    self.reply(404, "{}")
                else:
                    self.answer(body)

            def answer(self, body: dict[str, Any]) -> None:
                turn = owner._next(body)
                message = {"id": "msg_probe", "type": "message", "role": "assistant",
                           "model": body.get("model", "probe"), "content": [], "stop_reason": None,
                           "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}
                tool = turn["type"] == "tool_use"
                stop = "tool_use" if tool else "end_turn"
                if not body.get("stream"):
                    self.reply(200, json.dumps({**message, "content": [turn], "stop_reason": stop}))
                    return
                start = {**turn, "input": {}} if tool else {"type": "text", "text": ""}
                delta = ({"type": "input_json_delta", "partial_json": json.dumps(turn["input"])} if tool
                         else {"type": "text_delta", "text": turn["text"]})
                events = [
                    {"type": "message_start", "message": message},
                    {"type": "content_block_start", "index": 0, "content_block": start},
                    {"type": "content_block_delta", "index": 0, "delta": delta},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                     "usage": {"output_tokens": 1}},
                    {"type": "message_stop"}]
                self.reply(200, "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events),
                           "text/event-stream")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _next(self, body: dict[str, Any]) -> dict[str, Any]:
        """Record the markers ``body`` carries, and pick the first command it has no result for."""
        if not any(tool.get("name") == "Bash" for tool in body.get("tools") or ()):
            return {"type": "text", "text": "ok"}  # a side request of the CLI, not the session's turn
        ids = {ident for ident, _ in self.script}
        answered = set()
        with self._lock:
            self.requests += 1
            for message in body.get("messages") or ():
                content = message.get("content")
                for block in content if isinstance(content, list) else ():
                    if block.get("type") == "tool_result" and block.get("tool_use_id") in ids:
                        answered.add(block["tool_use_id"])
                        self.marks[block["tool_use_id"]] = read_mark(block)
        for ident, text in self.script:
            if ident not in answered:
                return {"type": "tool_use", "id": ident, "name": "Bash", "input": {"command": text}}
        return {"type": "text", "text": "done"}


def read_mark(block: dict[str, Any]) -> Mark:
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    text = str(content or "").strip()
    if block.get("is_error"):  # every command exits 0, so an error result means it was not run as written
        return Mark.BLOCKED_BY_HOOK if text.startswith(HOOK_ERROR_PREFIX) else Mark.OTHER
    return Mark(text) if text in (Mark.DENIED, Mark.ALLOWED, Mark.OTHER) else Mark.OTHER


# --- The commands and the outside view --------------------------------------------------------------------------


def command(path: Path | str, kind: Kind) -> str:
    """A read-only command that prints one marker. A directory is listed with the names discarded; a file is opened
    and no block of it is read (``dd count=0``; BSD ``head`` refuses a zero count)."""
    quoted = shlex.quote(str(path))
    attempt = (f"ls -- {quoted} 2>&1 >/dev/null" if kind is Kind.DIRECTORY
               else f"dd if={quoted} of=/dev/null count=0 status=none 2>&1 >/dev/null")
    return (f'o=$({attempt}); case "$o" in *"Operation not permitted"*) echo DENIED;; "") echo ALLOWED;; '
            "*) echo OTHER;; esac")


@dataclass(frozen=True)
class Entry:
    """A denied path as the probe sees it from outside the sandbox: metadata only."""

    index: int
    path: str
    label: str
    kind: Kind | None  # None: absent on this machine
    deny_class: DenyClass = DenyClass.CREDENTIAL
    unverifiable: Unverifiable | None = None
    state: tuple[int, int] | None = None  # a directory's (mtime in ns, number of entries)


def directory_state(path: str) -> tuple[int, int] | None:
    try:
        with os.scandir(path) as entries:
            count = sum(1 for _ in entries)
        return os.stat(path).st_mtime_ns, count
    except OSError:
        return None


def look(index: int, path: str, label: str, deny_class: DenyClass = DenyClass.CREDENTIAL) -> Entry:
    entry = partial(Entry, index, path, label, deny_class=deny_class)
    try:
        found = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return entry(None)
    except OSError:
        return entry(Kind.FILE, unverifiable=Unverifiable.NO_METADATA)
    if stat.S_ISDIR(found.st_mode):
        state = directory_state(path)
        return entry(Kind.DIRECTORY, unverifiable=None if state else Unverifiable.NOT_LISTABLE, state=state)
    return entry(Kind.FILE, unverifiable=None if os.access(path, os.R_OK) else Unverifiable.NOT_READABLE)


def labeller(home: str, runtime_root: Path) -> Callable[[int, str], str]:
    """How the report names a path: under HOME as ``~/…``; never an absolute path of this machine."""
    home, runtime = os.path.realpath(home), os.path.realpath(runtime_root)

    def label(index: int, path: str) -> str:
        if path == runtime:
            return "<the probe's runtime root>"
        if path.startswith(home + os.sep):
            return "~/" + path[len(home) + 1:]
        return f"<outside HOME, entry {index}>/{os.path.basename(path)}"

    return label


# --- The run ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CliRun:
    exit_code: int | None
    denied: frozenset[str]  # tool_use ids the CLI's result lists under permission_denials


def drive_cli(command: list[str], *, cwd: Path, env: dict[str, str], server: MarkerServer) -> CliRun:
    """One user message to the CLI, until it exits, the server refuses a request or the time is up. Of its output
    only the ids of refused tool calls are kept; the rest (replies, hook output, memory) is discarded."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True)
    denied: set[str] = set()

    def read() -> None:
        for line in process.stdout:
            try:
                event = json.loads(line)
                if event.get("type") == "result":
                    denied.update(str(item.get("tool_use_id")) for item in event.get("permission_denials") or ())
            except (ValueError, AttributeError):
                continue

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": "Run the probe."}}) + "\n")
        process.stdin.close()
        waited = 0.0
        while process.poll() is None and not server.refused.wait(0.1) and waited < TIMEOUT_SEC:
            waited += 0.1
    except BrokenPipeError:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)  # the CLI and whatever it left running
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=30)
        reader.join(timeout=10)
    return CliRun(process.returncode, frozenset(denied))


CONTROLS = (("allowed_directory", Kind.DIRECTORY, Mark.ALLOWED), ("allowed_file", Kind.FILE, Mark.ALLOWED),
            ("denied_directory", Kind.DIRECTORY, Mark.DENIED), ("denied_file", Kind.FILE, Mark.DENIED))


def run_probe(claude_bin: str, *, root: Path, drive: Callable[..., CliRun] = drive_cli) -> dict[str, Any]:
    """Run the probe from the current environment (the caller's isolated operator-login environment) under ``root``,
    an empty directory of its own. The environment, the command and the deny list are built by the functions a live
    direct session uses; the two negative controls are the only additions to the list."""
    if (problem := task_manager_sandbox.platform_problem()) is not None:
        return {"status": Status.INVALID.value, "reason": problem}
    root = Path(root).resolve()
    task, runtime = root / "task", root / "runtime"
    controls = {"allowed_directory": task / "probe-allowed", "denied_directory": root / "probe-denied-dir",
                "denied_file": root / "probe-denied-file"}
    controls["allowed_file"] = controls["allowed_directory"] / "file"
    for directory in (task, runtime, controls["allowed_directory"], controls["denied_directory"]):
        directory.mkdir(parents=True, exist_ok=True)
    for file in (controls["allowed_file"], controls["denied_directory"] / "file", controls["denied_file"]):
        file.write_text("probe\n")
    subprocess.run(["git", "init", "-q"], cwd=task, check=True, capture_output=True, timeout=30)

    launch = Launch(AgentSettings(profile=builtin_resolved_profile()))
    classes = task_manager_sandbox.read_deny_classes(launch.runtime.environment(launch), runtime)
    live = tuple(classes)  # the list a live session is launched with (``read_deny_paths``)
    added = tuple(os.path.realpath(controls[name]) for name in ("denied_directory", "denied_file"))
    prompt = root / "prompt.md"
    prompt.write_text("")
    argv = CLAUDE_CODE.operator_resident_command(claude_bin, cwd=task, prompt_file=prompt, launch=launch,
                                                 settings_dir=root / "settings", read_deny=(*live, *added))
    # The probe's key must be the only credential the CLI is handed: a token variable would outrank it.
    env = task_manager_safety.environment(launch, "deny-read-probe", drop=(
        *task_manager_sandbox.dropped_env(launch), "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"))
    home = env.get("HOME") or str(Path.home())
    label = labeller(home, runtime)
    entries = [look(index, path, label(index, path), classes[path]) for index, path in enumerate(live)]
    tested = [entry for entry in entries if entry.kind is not None and entry.unverifiable is None]
    script = [(f"toolu_probe_{entry.index}", command(entry.path, entry.kind)) for entry in tested]
    script += [(f"toolu_probe_{name}", command(controls[name], kind)) for name, kind, _ in CONTROLS]

    server = MarkerServer(script)
    url = server.start()
    try:
        env.update(ANTHROPIC_BASE_URL=url, ANTHROPIC_API_KEY=PROBE_KEY, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        done = drive(argv, cwd=task, env=env, server=server)
    finally:
        server.stop()
    if server.refusal is not None:  # nothing but the fact: no counts, no markers, nothing of the request
        return {"status": Status.ABORTED.value, "reason": server.refusal.value}
    if not server.requests:
        return {"status": Status.ABORTED.value,
                "reason": f"no request reached the probe server (the CLI exited {done.exit_code})"}

    marks = {ident: Mark.BLOCKED_BY_HOOK if ident in done.denied else server.marks.get(ident, Mark.OTHER)
             for ident, _ in script}
    failed = [{"index": entry.index, "path": entry.label, "mark": marks[f"toolu_probe_{entry.index}"].value}
              for entry in tested if marks[f"toolu_probe_{entry.index}"] is not Mark.DENIED]
    answers = {name: marks[f"toolu_probe_{name}"] for name, _, _ in CONTROLS}
    alarms, changed_during_probe = [], []
    for entry in entries:
        if entry.state is None:
            continue
        now = directory_state(entry.path)
        changed = (["gone or unreadable"] if now is None else
                   [what for what, before, after in zip(("mtime", "count"), entry.state, now) if before != after])
        if changed:  # a directory that is gone or unreadable now is an alarm whatever its class
            expected = now is not None and entry.deny_class is DenyClass.AGENT_STATE
            (changed_during_probe if expected else alarms).append(
                {"index": entry.index, "path": entry.label, "changed": changed})
    counts = {mark.value: sum(value is mark for value in marks.values()) for mark in Mark}
    verified = sum(marks[f"toolu_probe_{entry.index}"] is Mark.DENIED for entry in tested)
    absent = [entry.label for entry in entries if entry.kind is None]
    unverifiable = [{"index": entry.index, "path": entry.label, "reason": entry.unverifiable.value}
                    for entry in entries if entry.unverifiable is not None]
    expected = len(script) + 1
    reason = None
    if counts[Mark.BLOCKED_BY_HOOK.value]:
        status, reason = Status.INVALID, "a command was blocked before it ran (a PreToolUse hook or a permission rule)"
    elif server.requests != expected:
        status, reason = Status.INVALID, f"{server.requests} model requests reached the server, expected {expected}"
    elif failed or any(answers[name] is not want for name, _, want in CONTROLS) or not verified:
        status = Status.FAIL
        reason = "an entry was not denied, a control answered wrongly, or nothing was verified"
    elif alarms:
        status, reason = Status.ALARM, "a denied credential directory changed while the probe ran"
    else:  # every present entry is DENIED, or listed as unverifiable with its reason
        status = Status.PASS
        if unverifiable:
            reason = "passed with present entries that were not verified: " + "; ".join(
                f"{item['path']} ({item['reason']})" for item in unverifiable)
    summary = f"{verified} verified denied, {len(absent)} absent on this machine (not verified)"
    if unverifiable:
        summary += f", {len(unverifiable)} unreadable outside the sandbox too (not verified)"
    return {"status": status.value, "reason": reason, "summary": summary, "entries": len(entries),
            "verified_denied": verified, "absent": absent, "unverifiable": unverifiable, "failed": failed,
            "alarms": alarms, "changed_during_probe": changed_during_probe,
            "controls": {name: mark.value for name, mark in answers.items()}, "markers": counts,
            "requests": server.requests, "cli_exit_code": done.exit_code}
