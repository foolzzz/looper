"""Real-path check of the Manager's stop (design chat-stop-v2.zh-CN.md 8.4): drive a throwaway ``looper serve``.

Run by hand (it needs Claude Code or OMP with a DeepSeek key, and a disposable PostgreSQL server)::

    python -m evals.stop_realpath --runtime claude|omp [--repeat 3] [--out FILE] [--pg-url URL]
        [--ports 8791-8799] [--deepseek-model deepseek-flash] [--key-from-zsh]

Every cell sends a message through ``POST /api/chat/send``, follows its events, stops at the cell's moment through
``POST /api/chat/stop`` and checks what is left. The server has a database, runtime root and port of its own; it
never touches ``~/.looper`` and is killed with its process tree in ``finally``. The report is Markdown, home and
temp paths redacted.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.prompt_regression import runner
from evals.prompt_regression.runner import Api, EvalError

RESERVED_PORT = 8767
FINAL_WITHIN_S = 15.0
GONE_AFTER_S = 5.0
DEEPSEEK_KEY_ENV = "DEEPSEEK_API_KEY"


@dataclass(frozen=True)
class Cell:
    key: str
    title: str
    moment: str  # "send" | "activity" | "delta" | "thinking"
    prompt: str
    running: int = 0  # at the moment, wait for this many pids: the stop comes while the command runs
    after_s: float = 0.0  # stop this long after the moment
    detached: bool = False  # its command leaves a sleeper detached (setsid, its parent gone): see ``best_effort``


_PIDS = ("Run the shell commands below one after another with your Bash tool, and before anything else inside "
         "each command run `echo $$ >> {file}`. ")
_SLEEP = _PIDS + "Command 1: `sleep 120`. After it finishes say done."
# A sleeper in a session of its own (``os.setsid``: macOS has no setsid command) whose parent exits at once, as #339
# reproduced; it writes its own pid too, so a survivor is seen, and again to ``<file>.detached``, so it is told apart
# from the shell. Python forks it itself: OMP runs commands in its brush-core shell, where the sleeper of a
# backgrounded subshell (``(… &)``, kept as cell dx) never ran.
_SETSID_PY = ("import os, time\nif os.fork() == 0:\n    os.setsid()\n"
              "    for name in (\"{file}\", \"{file}.detached\"):\n"
              "        f = open(name, \"a\")\n        f.write(\"%d\\n\" % os.getpid())\n        f.close()\n"
              "    time.sleep(120)\n")
_SETSID = _PIDS + ("Command 1: `python3 {script}; sleep 60` (the script forks a sleeper and exits at once). "
                   "After it finishes say done.")
_SETSID_SUBSHELL = _PIDS + ("Command 1: `(python3 -c 'import os, time; os.setsid(); f = open(\"{file}\", \"a\"); "
                            "f.write(\"%d\\n\" % os.getpid()); f.close(); time.sleep(120)' &); sleep 60`. "
                            "After it finishes say done.")
_TEXT = "Count from 1 to 300, one number per line, without any tool."
_TOOL_THEN_TEXT = _PIDS + "Command 1: `sleep 1`. Then write a long poem of 60 lines."

_COMMON = (
    Cell("a", "stop right after send", "send", _SLEEP),
    Cell("b", "stop at the first tool activity", "activity", _SLEEP, running=1),
    Cell("c", "stop at the first text delta", "delta", _TEXT),
    Cell("d", "stop at the first tool activity, a setsid child", "activity", _SETSID, running=2, detached=True),
)
_CLAUDE_ONLY = (Cell("e", "stop after the tool, in the text", "delta", _TOOL_THEN_TEXT),)
# By hand only (--cells dx): cell d as first written, a subshell in the background.
_DIAGNOSTIC = (Cell("dx", "cell d with a backgrounded subshell", "activity", _SETSID_SUBSHELL, running=2,
                    detached=True),)
# OMP gives a message's text only at its end (``OmpStream`` drops ``message_update``): its first delta is the whole
# message, so cell c's stop finds a turn that is usually over. This one stops while the model is still writing.
_OMP_ONLY = (Cell("f", "stop while the text is written, before it is shown", "thinking", _TEXT, after_s=1.5),)


def best_effort(cell: Cell, platform: str = sys.platform) -> bool:
    """Whether a detached sleeper the stop left running is recorded rather than failed: on macOS, ending a daemon
    whose ancestors are gone is best effort (design chat-stop-v2.zh-CN.md 6.6 and 10); on Linux it is ended."""
    return cell.detached and platform == "darwin"


def cells_for(runtime: str) -> tuple[Cell, ...]:
    return _COMMON + (_CLAUDE_ONLY if runtime == "claude" else _OMP_ONLY)


def chosen_cells(runtime: str, keys: str | None) -> tuple[Cell, ...]:
    """The runtime's cells, or those of ``keys`` (comma-separated; the diagnostic ones too)."""
    if not keys:
        return cells_for(runtime)
    wanted = keys.split(",")
    return tuple(c for c in cells_for(runtime) + _DIAGNOSTIC if c.key in wanted)


# Not stopped: one write call of Looper's own tools in an ordinary turn, so the turn binding's match (6.3) is seen
# on the real path. With Stop on, the write runs only when it matches the turn's streamed tool_use.
_PROBE = ("Call the looper_providers tool exactly once, with the arguments "
          '{{"action": "register", "name": "{name}", "vendor_type": "deepseek"}}. '
          "Do not use any other tool. Then reply with the single word done.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.stop_realpath", description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", required=True, choices=("claude", "omp"))
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--pg-url", default=os.environ.get("LOOPER_EVAL_DATABASE_URL")
                        or os.environ.get("LOOPER_TEST_DATABASE_URL"))
    parser.add_argument("--ports", default=None, help="the server's port from this range (FIRST-LAST), never 8767")
    parser.add_argument("--deepseek-model", default="deepseek-flash")
    parser.add_argument("--cells", default=None, help="only these cells (comma-separated keys; probe included)")
    parser.add_argument("--keep-log", type=Path, default=None, help="copy the server's log here at the end")
    parser.add_argument("--key-from-zsh", action="store_true",
                        help=f"read ${DEEPSEEK_KEY_ENV} from an interactive zsh, for the server's environment alone")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.ports is not None:
        try:
            first, last = (int(part) for part in args.ports.split("-", 1))
        except ValueError:
            parser.error("--ports is FIRST-LAST")
        args.ports = range(first, last + 1)
    return args


def _ask_os_for_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pick_port(ports: range | None = None) -> int:
    """A free port (of ``ports``, when given), never the operator's 8767."""
    if ports is not None:
        for port in ports:
            if port == RESERVED_PORT:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
            return port
        raise EvalError(f"no free port in {ports.start}-{ports.stop - 1}")
    while True:
        port = _ask_os_for_port()
        if port != RESERVED_PORT:
            return port


def key_from_zsh() -> str:
    """``$DEEPSEEK_API_KEY`` as the operator's interactive zsh sets it; never printed."""
    try:
        listed = subprocess.run(["zsh", "-ic", f'printf %s "${DEEPSEEK_KEY_ENV}"'], capture_output=True, text=True,
                                timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return listed.stdout.strip()


# --- Verdict -------------------------------------------------------------------------------------------


@dataclass
class Observation:
    final: dict[str, Any] | None  # the answer.final payload
    final_after_s: float | None  # seconds from the stop request to it
    pids: list[int]  # what the commands wrote
    alive: list[int]  # of them, still alive 5 s after the final
    record: dict[str, Any] | None  # the turn's llm record
    actions: list[dict[str, Any]]  # manager actions recorded after the stop request
    confirmations: int  # confirmations left open
    next_text: str  # the next turn's answer
    session_before: str | None
    session_after: str | None
    expect_pids: bool = True
    expect_record: bool = True  # a stop before the model started leaves none (design 7)


@dataclass
class CellResult:
    cell: str
    rep: int
    failures: list[str] = field(default_factory=list)
    timeline: str = ""  # seconds after the send: the moment, the first delta, the stop, the final


def verdict(obs: Observation) -> list[str]:
    """Why the cell failed (empty when it passed)."""
    why: list[str] = []
    if obs.final is None:
        why.append("no answer.final")
    else:
        if obs.final.get("outcome") != "stopped":
            why.append(f"answer.final outcome is {obs.final.get('outcome')!r}, not 'stopped'")
        if obs.final_after_s is None or obs.final_after_s > FINAL_WITHIN_S:
            why.append(f"answer.final came after {obs.final_after_s} s, over {FINAL_WITHIN_S:g} s")
    if obs.expect_pids and not obs.pids:
        why.append("no pid was written: the model ran no command")
    if obs.alive:
        why.append(f"pids still alive {GONE_AFTER_S:g} s after: {sorted(obs.alive)}")
    record = obs.record or {}
    if obs.expect_record:
        if record.get("status") != "interrupted":
            why.append(f"llm record is {record.get('status')!r}, not interrupted")
        stop = (record.get("metadata") or {}).get("user_stop") or record.get("user_stop")
        if not isinstance(stop, dict) or "phase" not in stop or "method" not in stop:
            why.append("llm record has no user_stop with phase and method")
    for action in obs.actions:
        if "stopped" not in (action.get("status"), action.get("error_code")):
            why.append(f"a manager action after the stop is {action.get('status')!r}, not stopped")
            break
    if obs.confirmations:
        why.append(f"{obs.confirmations} confirmation left open")
    if not obs.next_text.strip():
        why.append("the next turn gave no answer")
    if obs.session_before != obs.session_after:
        why.append(f"session id changed: {obs.session_before} -> {obs.session_after}")
    return why


# --- Report --------------------------------------------------------------------------------------------


def redact(text: str, *, tmp: str) -> str:
    text = text.replace(tmp, "<tmp>")
    return text.replace(str(Path.home()), "~")


def read_trace(path: Path) -> list[list[str]]:
    """The ``_meta`` keys of each ``tools/call`` the bridge traced (a call without ``_meta`` is an empty list)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    calls = []
    for line in lines:
        try:
            keys = json.loads(line).get("meta_keys")
        except (ValueError, AttributeError):
            continue
        calls.append([str(k) for k in keys] if isinstance(keys, list) else [])
    return calls


def render_report(runtime: str, results: list[CellResult], *, claude_version: str | None,
                  meta_keys: list[list[str]], tmp: str, binding: dict[str, Any] | None = None,
                  log_counts: dict[str, int] | None = None) -> str:
    seen = sorted({key for keys in meta_keys for key in keys})
    lines = [f"# Stop real-path report: {runtime}", "",
             f"- Claude Code version: {claude_version or 'unknown'}",
             f"- tools/call calls traced: {len(meta_keys)}; `_meta` carried: "
             + (", ".join(f"`{k}`" for k in seen) if seen else "no")]
    if binding is not None:
        matches = ", ".join(f"{m['by']}/{m['when']}: {m['count']}" for m in binding.get("matches") or []) or "none"
        by = ", ".join(f"{m['reason']}/{m['runtime']}/{m['tool']}: {m['count']}" for m in binding.get("by") or [])
        lines += [f"- turn binding (GET /api/status): enforced {binding.get('enforced')}, mismatches "
                  f"{binding.get('mismatches')}{f' ({by})' if by else ''}",
                  f"- matched calls (by how / when the tool_use was read): {matches}"]
    if log_counts is not None:
        lines.append("- server log: " + ", ".join(f"`{k}` {v}" for k, v in log_counts.items()))
    lines += ["", "| cell | rep | result | why | timeline (s after send) |", "| --- | --- | --- | --- | --- |"]
    for r in results:
        status = "pass" if not r.failures else "FAIL"
        lines.append(f"| {r.cell} | {r.rep} | {status} | {'; '.join(r.failures) or '-'} | {r.timeline or '-'} |")
    return redact("\n".join(lines) + "\n", tmp=tmp)


# --- The real run --------------------------------------------------------------------------------------


def claude_version() -> str | None:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.strip() or None


class _Stream:
    """The SSE events of one message, read on a thread."""

    def __init__(self, url: str, token: str | None = None) -> None:
        self.events: list[tuple[float, str, dict[str, Any]]] = []
        self._lock = threading.Lock()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        kind = "message"
        try:
            for raw in self._response:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        payload = json.loads(line[5:])
                    except ValueError:
                        continue
                    with self._lock:
                        self.events.append((time.monotonic(), kind, payload))
        except (OSError, ValueError):
            pass

    def snapshot(self) -> list[tuple[float, str, dict[str, Any]]]:
        with self._lock:
            return list(self.events)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._response.close()


def _wait(predicate: Callable[[], Any], timeout: float, step: float = 0.1) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def _moment_reached(moment: str, events: list[tuple[float, str, dict[str, Any]]]) -> bool:
    if moment == "send":
        return True
    for _, kind, payload in events:
        if moment == "activity" and kind == "agent.phase" and payload.get("activity") == "tool":
            return True
        if moment == "delta" and kind == "answer.delta":
            return True
        if moment == "thinking" and kind == "agent.phase" and payload.get("activity") == "thinking":
            return True
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pids(path: Path) -> list[int]:
    try:
        return [int(x) for x in path.read_text().split() if x.isdigit()]
    except OSError:
        return []


def _db_rows(database_url: str, sql: str, *args: Any) -> list[dict[str, Any]]:
    import asyncio

    import asyncpg

    async def go() -> list[dict[str, Any]]:
        conn = await asyncpg.connect(database_url)
        try:
            return [dict(r) for r in await conn.fetch(sql, *args)]
        finally:
            await conn.close()

    return asyncio.run(go())


def _latest_record(api: Api, since: int) -> dict[str, Any] | None:
    for brief in api.get("/api/llm-records", agent_id="manager", limit=20)["records"]:
        if brief["id"] > since:
            return api.get(f"/api/llm-records/{brief['id']}")["record"]
    return None


def _record_events(api: Api, since: int) -> list[dict[str, Any]]:
    brief = next((r for r in api.get("/api/llm-records", agent_id="manager", limit=20)["records"] if r["id"] > since),
                 None)
    if brief is None:
        return []
    return [e.get("event") or {} for e in api.get(f"/api/llm-records/{brief['id']}/events", limit=1000)["events"]]


def tool_calls(events: list[dict[str, Any]], limit: int = 300) -> str:
    """What the model ran (each tool_use's input) and what came back, short: why a command wrote no pid."""
    parts: list[str] = []
    for event in events:
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                parts.append(f"{block.get('name')} {json.dumps(block.get('input'), ensure_ascii=False)[:limit]}")
            elif block.get("type") == "tool_result":
                parts.append(f"-> {str(block.get('content'))[:limit]}")
    return " ".join(parts).replace("|", "\\|").replace("\n", " ") or "no tool call"


def whole_before_stop(events: list[tuple[float, str, dict[str, Any]]], stopped_at: float) -> bool:
    """Whether the reply's text was all shown before the stop, and it then ended answered: the turn was over (the
    stop's 409 ``not_running`` is right)."""
    final = next((p for _, k, p in events if k == "answer.final"), None)
    shown = "".join(str(p.get("text") or "") for t, k, p in events if k == "answer.delta" and t <= stopped_at)
    return (final is not None and final.get("outcome") == "answered"
            and bool(shown.strip()) and shown.strip() == str(final.get("text") or "").strip())


def run_cell(api: Api, database_url: str, cell: Cell, rep: int, work: Path, clock_offset: float = 0.0) -> CellResult:
    """``clock_offset``: the database's clock minus this one's (the actions after the stop are read by time)."""
    session = f"stop-{uuid.uuid4().hex[:10]}"
    pid_file = work / f"pids-{cell.key}-{rep}.txt"
    script = work / f"sleeper-{cell.key}-{rep}.py"
    script.write_text(_SETSID_PY.format(file=pid_file), encoding="utf-8")
    prompt = cell.prompt.format(file=pid_file, script=script)
    records = api.get("/api/llm-records", agent_id="manager", limit=1)["records"]
    since = max((r["id"] for r in records), default=0)
    message_id = f"m-{uuid.uuid4().hex[:10]}"
    sent_at = time.monotonic()
    sent = api.post("/api/chat/send", {"text": prompt, "session_id": session, "message_id": message_id})
    stream = _Stream(api.base_url + sent["events_url"], api.token)
    marks: dict[str, float] = {}

    def timeline() -> str:
        events = stream.snapshot()
        for name, kind in (("first delta", "answer.delta"), ("final", "answer.final")):
            if name not in marks and (at := next((t for t, k, _ in events if k == kind), None)) is not None:
                marks[name] = at
        return ", ".join(f"{name} {at - sent_at:.1f}" for name, at in sorted(marks.items(), key=lambda m: m[1]))

    try:
        if not _wait(lambda: _moment_reached(cell.moment, stream.snapshot()), 120):
            return CellResult(cell.key, rep, [f"the moment {cell.moment!r} never came"], timeline())
        marks["moment"] = time.monotonic()
        if cell.after_s:
            time.sleep(cell.after_s)
        if cell.running and not _wait(lambda: len(_read_pids(pid_file)) >= cell.running, 30):
            with contextlib.suppress(EvalError):  # end the turn before the next cell
                api.post("/api/chat/stop", {"message_id": message_id})
            _wait(lambda: any(k == "answer.final" for _, k, _ in stream.snapshot()), FINAL_WITHIN_S)
            return CellResult(cell.key, rep, [
                f"the command did not write {cell.running} pid(s) within 30 s (wrote {len(_read_pids(pid_file))}); "
                f"the model ran: {tool_calls(_record_events(api, since))}"], timeline())
        started_at = time.time()
        stopped_at = marks["stop"] = time.monotonic()
        try:
            api.post("/api/chat/stop", {"message_id": message_id})
        except EvalError as exc:
            _wait(lambda: any(k == "answer.final" for _, k, _ in stream.snapshot()), FINAL_WITHIN_S)
            if "not_running" in str(exc) and whole_before_stop(stream.snapshot(), stopped_at):
                return CellResult(cell.key, rep, [], timeline() + "; the reply was whole before the stop: "
                                  "409 not_running (the turn had ended)")
            return CellResult(cell.key, rep, [f"the stop was refused: {exc}"], timeline())
        final = _wait(lambda: next((p for _, k, p in stream.snapshot() if k == "answer.final"), None), FINAL_WITHIN_S)
        after = None
        if final is not None:
            after = next(t for t, k, _ in stream.snapshot() if k == "answer.final") - stopped_at
        line = timeline()
    finally:
        stream.close()
    time.sleep(GONE_AFTER_S)
    pids = _read_pids(pid_file)
    alive = [p for p in pids if _alive(p)]
    if best_effort(cell):
        detached = set(_read_pids(pid_file.with_name(pid_file.name + ".detached")))
        if left := [p for p in alive if p in detached]:
            line += f"; macOS best effort: the detached sleeper {left} was left running (not a pass criterion)"
            for pid in left:  # (it would sleep on through the next cells)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        alive = [p for p in alive if p not in detached]
    record = _latest_record(api, since)
    actions = _db_rows(database_url, "SELECT status, error_code FROM manager_actions "
                       "WHERE created_at >= to_timestamp($1) AND chat = $2 ORDER BY id", started_at + clock_offset,
                       session)
    # needs_confirmation rows no later call of the session answered: a confirmation left open
    confirmations = sum(1 for a in actions if a["status"] == "needs_confirmation")
    before = (api.get("/api/manager/session") or {}).get("session_id")
    nxt = f"stop-next-{uuid.uuid4().hex[:8]}"
    api.post("/api/chat/send", {"text": "Say hi in one word.", "session_id": session, "message_id": nxt})
    reply = _wait(lambda: next((m for m in api.get("/api/chat/messages", session_id=session)["messages"]
                                if m.get("message_id") == f"{nxt}:reply"), None), 180)
    after_id = (api.get("/api/manager/session") or {}).get("session_id")
    obs = Observation(
        final=final, final_after_s=after, pids=pids, alive=alive, record=record,
        actions=actions, confirmations=confirmations, next_text=str((reply or {}).get("content") or ""),
        session_before=before, session_after=after_id, expect_pids=cell.moment != "delta" or cell.key == "e")
    if cell.key in ("c", "f"):
        obs.expect_pids = False
    if cell.moment == "send":  # stopped before the model saw it (7): no command, and no run record unless it started
        obs.expect_pids = False
        obs.expect_record = record is not None
    stop = ((record or {}).get("metadata") or {}).get("user_stop") or (record or {}).get("user_stop")
    if isinstance(stop, dict):
        line += f"; user_stop phase {stop.get('phase')}, method {stop.get('method')}"
    elif record is None:
        line += "; no run record"
    return CellResult(cell.key, rep, verdict(obs), line)


def run_probe(api: Api, database_url: str, rep: int) -> CellResult:
    """An ordinary turn with one write call of Looper's tools: it runs (matched), and the turn answers."""
    session = f"probe-{uuid.uuid4().hex[:10]}"
    message_id = f"m-{uuid.uuid4().hex[:10]}"
    api.post("/api/chat/send", {"text": _PROBE.format(name=f"stop-probe-{rep}-{uuid.uuid4().hex[:6]}"),
                                "session_id": session, "message_id": message_id})
    reply = _wait(lambda: next((m for m in api.get("/api/chat/messages", session_id=session)["messages"]
                                if m.get("message_id") == f"{message_id}:reply"), None), 240)
    actions = _db_rows(database_url, "SELECT tool, risk, status, error_code FROM manager_actions "
                       "WHERE chat = $1 ORDER BY id", session)
    why: list[str] = []
    if reply is None:
        why.append("no reply")
    writes = [a for a in actions if a["risk"] != "read"]
    if not writes:
        why.append("the model made no write call")
    for action in writes:
        if action["status"] != "ok":
            why.append(f"{action['tool']} was {action['status']} ({action['error_code']})")
    return CellResult("probe", rep, why, ", ".join(f"{a['tool']} {a['status']}" for a in actions) or "no action")


def _set_profile(api: Api, runtime: str, *, deepseek_model: str = "deepseek-flash") -> None:
    if runtime == "claude":
        chosen = runner.register_agent(api, {
            "runtime": {"type": "claude-code"}, "provider": {"provider_id": "prov-claude-subscription"},
            "model": {"name": "sonnet", "protocol": "anthropic-messages"}, "agent": {"name": "stop-real/claude"}})
    else:
        chosen = runner.register_agent(api, {
            "runtime": {"type": "omp"},
            "provider": {"name": "stop-real-deepseek", "vendor_type": "deepseek", "api_key_env": DEEPSEEK_KEY_ENV,
                         "endpoints": [{"protocol": "openai-chat", "base_url": "https://api.deepseek.com"}]},
            "model": {"name": deepseek_model, "protocol": "openai-chat"},
            "agent": {"name": "stop-real/omp"}})
    api.put("/api/agent-settings/roles/manager", {"profile_id": chosen["profile_id"]})


def _kill_tree(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        process.kill()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server_env: dict[str, str] = {}
    if args.runtime == "omp":
        key = key_from_zsh() if args.key_from_zsh else os.environ.get(DEEPSEEK_KEY_ENV, "")
        if not key:
            print(f"error: the OMP profile needs ${DEEPSEEK_KEY_ENV}", file=sys.stderr)
            return 2
        server_env[DEEPSEEK_KEY_ENV] = key  # the server's environment alone
    if not args.pg_url:
        print("error: give --pg-url (or set LOOPER_EVAL_DATABASE_URL)", file=sys.stderr)
        return 2
    results: list[CellResult] = []
    binding: dict[str, Any] | None = None
    with tempfile.TemporaryDirectory(prefix="stop-realpath-") as tmp_name, runner.fresh_database(args.pg_url) as (
            database_url, clock_offset):
        tmp = Path(tmp_name)
        trace = tmp / "mcp-trace.jsonl"
        os.environ["LOOPER_MCP_TRACE"] = str(trace)
        runner.free_port = lambda: pick_port(args.ports)  # the server's port comes from here: never 8767
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        work = tmp / "work"
        work.mkdir()
        version = claude_version()
        home = tmp / "home"
        home.mkdir()
        keychains = Path.home() / "Library" / "Keychains"
        if sys.platform == "darwin" and keychains.is_dir():
            # Claude Code's login is in the macOS keychain, found under $HOME: the throwaway home links to the
            # keychains alone, so Claude Code's own files (.claude, .claude.json) stay in the throwaway home.
            (home / "Library").mkdir()
            (home / "Library" / "Keychains").symlink_to(keychains)
        # Every process the server starts is in its own group so that `finally` ends the tree.
        original_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: original_popen(*a, **{**k, "start_new_session": True})  # type: ignore
        try:
            with runner.running_server(database_url=database_url, runtime_root=tmp / "runtime", bin_dir=bin_dir,
                                       log_path=tmp / "server.log", home=home, env=server_env) as api:
                api.timeout = 60.0
                api.put("/api/chat-settings", {"stop_enabled": True})
                _set_profile(api, args.runtime, deepseek_model=args.deepseek_model)
                for rep in range(1, args.repeat + 1):
                    try:
                        results.append(run_probe(api, database_url, rep))
                    except Exception as exc:  # (an API error too: the cell failed, the run goes on)
                        results.append(CellResult("probe", rep, [f"the script failed: {exc}"]))
                    for cell in chosen_cells(args.runtime, args.cells):
                        try:
                            results.append(run_cell(api, database_url, cell, rep, work, clock_offset))
                        except Exception as exc:  # (an API error too: the cell failed, the run goes on)
                            results.append(CellResult(cell.key, rep, [f"the script failed: {exc}"]))
                binding = api.get("/api/status").get("turn_binding")
        finally:
            subprocess.Popen = original_popen  # type: ignore
            for pid in _read_all_pids(work):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        try:
            log = (tmp / "server.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            log = ""
        counts = {needle: log.count(needle) for needle in ("stop lost", "turn binding mismatch")}
        if args.keep_log is not None:
            args.keep_log.write_text(log, encoding="utf-8")
        report = render_report(args.runtime, results, claude_version=version, meta_keys=read_trace(trace),
                               tmp=tmp_name, binding=binding, log_counts=counts)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    print(report)
    return 0 if all(not r.failures for r in results) else 1


def _read_all_pids(work: Path) -> list[int]:
    return [p for f in work.glob("pids-*.txt") for p in _read_pids(f)]


if __name__ == "__main__":
    sys.exit(main())
