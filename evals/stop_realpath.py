"""Real-path check of the Manager's stop (design chat-stop-v2.zh-CN.md 8.4): drive a throwaway ``looper serve``.

Run by hand (it needs Claude Code or OMP with a DeepSeek key, and a disposable PostgreSQL server)::

    python -m evals.stop_realpath --runtime claude|omp [--repeat 3] [--out FILE] [--pg-url URL]

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
    moment: str  # "send" | "activity" | "delta"
    prompt: str


_PIDS = ("Run the shell commands below one after another with your Bash tool, and before anything else inside "
         "each command run `echo $$ >> {file}`. ")
_SLEEP = _PIDS + "Command 1: `sleep 120`. After it finishes say done."
_SETSID = _PIDS + "Command 1: `setsid sleep 120 &` then `wait`. After it finishes say done."
_TEXT = "Count from 1 to 300, one number per line, without any tool."
_TOOL_THEN_TEXT = _PIDS + "Command 1: `sleep 1`. Then write a long poem of 60 lines."

_COMMON = (
    Cell("a", "stop right after send", "send", _SLEEP),
    Cell("b", "stop at the first tool activity", "activity", _SLEEP),
    Cell("c", "stop at the first text delta", "delta", _TEXT),
    Cell("d", "stop at the first tool activity, a setsid child", "activity", _SETSID),
)
_CLAUDE_ONLY = (Cell("e", "stop after the tool, in the text", "delta", _TOOL_THEN_TEXT),)


def cells_for(runtime: str) -> tuple[Cell, ...]:
    return _COMMON + (_CLAUDE_ONLY if runtime == "claude" else ())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.stop_realpath", description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", required=True, choices=("claude", "omp"))
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--pg-url", default=os.environ.get("LOOPER_EVAL_DATABASE_URL")
                        or os.environ.get("LOOPER_TEST_DATABASE_URL"))
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    return args


def _ask_os_for_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pick_port() -> int:
    """A free port, never the operator's 8767."""
    while True:
        port = _ask_os_for_port()
        if port != RESERVED_PORT:
            return port


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


@dataclass
class CellResult:
    cell: str
    rep: int
    failures: list[str] = field(default_factory=list)


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
                  meta_keys: list[list[str]], tmp: str) -> str:
    seen = sorted({key for keys in meta_keys for key in keys})
    lines = [f"# Stop real-path report: {runtime}", "",
             f"- Claude Code version: {claude_version or 'unknown'}",
             f"- tools/call calls traced: {len(meta_keys)}; `_meta` carried: "
             + (", ".join(f"`{k}`" for k in seen) if seen else "no"),
             "", "| cell | rep | result | why |", "| --- | --- | --- | --- |"]
    for r in results:
        status = "pass" if not r.failures else "FAIL"
        lines.append(f"| {r.cell} | {r.rep} | {status} | {'; '.join(r.failures) or '-'} |")
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

    def __init__(self, url: str) -> None:
        self.events: list[tuple[float, str, dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._response = urllib.request.urlopen(urllib.request.Request(url), timeout=300)
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


def run_cell(api: Api, database_url: str, cell: Cell, rep: int, work: Path) -> CellResult:
    session = f"stop-{uuid.uuid4().hex[:10]}"
    pid_file = work / f"pids-{cell.key}-{rep}.txt"
    prompt = cell.prompt.format(file=pid_file)
    records = api.get("/api/llm-records", agent_id="manager", limit=1)["records"]
    since = max((r["id"] for r in records), default=0)
    message_id = f"m-{uuid.uuid4().hex[:10]}"
    sent = api.post("/api/chat/send", {"text": prompt, "session_id": session, "message_id": message_id})
    stream = _Stream(api.base_url + sent["events_url"])
    try:
        if not _wait(lambda: _moment_reached(cell.moment, stream.snapshot()), 120):
            return CellResult(cell.key, rep, [f"the moment {cell.moment!r} never came"])
        started_at = time.time()
        stopped_at = time.monotonic()
        api.post("/api/chat/stop", {"message_id": message_id})
        final = _wait(lambda: next((p for _, k, p in stream.snapshot() if k == "answer.final"), None), FINAL_WITHIN_S)
        after = None
        if final is not None:
            after = next(t for t, k, _ in stream.snapshot() if k == "answer.final") - stopped_at
    finally:
        stream.close()
    time.sleep(GONE_AFTER_S)
    pids = _read_pids(pid_file)
    record = _latest_record(api, since)
    actions = _db_rows(database_url, "SELECT status, error_code FROM manager_actions "
                       "WHERE created_at >= to_timestamp($1) AND message_id = $2 ORDER BY id", started_at, message_id)
    # needs_confirmation rows no later call of the session answered: a confirmation left open
    confirmations = sum(1 for a in actions if a["status"] == "needs_confirmation")
    before = (api.get("/api/manager/session") or {}).get("session_id")
    nxt = f"stop-next-{uuid.uuid4().hex[:8]}"
    api.post("/api/chat/send", {"text": "Say hi in one word.", "session_id": session, "message_id": nxt})
    reply = _wait(lambda: next((m for m in api.get("/api/chat/messages", session_id=session)["messages"]
                                if m.get("message_id") == f"{nxt}:reply"), None), 180)
    after_id = (api.get("/api/manager/session") or {}).get("session_id")
    obs = Observation(
        final=final, final_after_s=after, pids=pids, alive=[p for p in pids if _alive(p)], record=record,
        actions=actions, confirmations=confirmations, next_text=str((reply or {}).get("content") or ""),
        session_before=before, session_after=after_id, expect_pids=cell.moment != "delta" or cell.key == "e")
    if cell.key == "c":
        obs.expect_pids = False
    return CellResult(cell.key, rep, verdict(obs))


def _set_profile(api: Api, runtime: str) -> None:
    if runtime == "claude":
        chosen = runner.register_agent(api, {
            "runtime": {"type": "claude-code"}, "provider": {"provider_id": "prov-claude-subscription"},
            "model": {"name": "sonnet", "protocol": "anthropic-messages"}, "agent": {"name": "stop-real/claude"}})
    else:
        chosen = runner.register_agent(api, {
            "runtime": {"type": "omp"},
            "provider": {"name": "stop-real-deepseek", "vendor_type": "deepseek", "api_key_env": DEEPSEEK_KEY_ENV,
                         "endpoints": [{"protocol": "anthropic-messages",
                                        "base_url": "https://api.deepseek.com/anthropic"}]},
            "model": {"name": "deepseek-chat", "protocol": "anthropic-messages"},
            "agent": {"name": "stop-real/omp"}})
    api.put("/api/agent-settings/roles/manager", {"profile_id": chosen["profile_id"]})


def _kill_tree(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        process.kill()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.runtime == "omp" and not os.environ.get(DEEPSEEK_KEY_ENV):
        print(f"error: the OMP profile needs ${DEEPSEEK_KEY_ENV}", file=sys.stderr)
        return 2
    if not args.pg_url:
        print("error: give --pg-url (or set LOOPER_EVAL_DATABASE_URL)", file=sys.stderr)
        return 2
    results: list[CellResult] = []
    with tempfile.TemporaryDirectory(prefix="stop-realpath-") as tmp_name, runner.fresh_database(args.pg_url) as (
            database_url, _offset):
        tmp = Path(tmp_name)
        trace = tmp / "mcp-trace.jsonl"
        os.environ["LOOPER_MCP_TRACE"] = str(trace)
        runner.free_port = pick_port  # the server's port comes from here: never 8767
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        work = tmp / "work"
        work.mkdir()
        version = claude_version()
        # Every process the server starts is in its own group so that `finally` ends the tree.
        original_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: original_popen(*a, **{**k, "start_new_session": True})  # type: ignore
        try:
            with runner.running_server(database_url=database_url, runtime_root=tmp / "runtime", bin_dir=bin_dir,
                                       log_path=tmp / "server.log", home=tmp / "home") as api:
                api.timeout = 60.0
                api.put("/api/chat-settings", {"stop_enabled": True})
                _set_profile(api, args.runtime)
                for rep in range(1, args.repeat + 1):
                    for cell in cells_for(args.runtime):
                        try:
                            results.append(run_cell(api, database_url, cell, rep, work))
                        except EvalError as exc:
                            results.append(CellResult(cell.key, rep, [f"the script failed: {exc}"]))
        finally:
            subprocess.Popen = original_popen  # type: ignore
            for pid in _read_all_pids(work):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        report = render_report(args.runtime, results, claude_version=version, meta_keys=read_trace(trace),
                               tmp=tmp_name)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    print(report)
    return 0 if all(not r.failures for r in results) else 1


def _read_all_pids(work: Path) -> list[int]:
    return [p for f in work.glob("pids-*.txt") for p in _read_pids(f)]


if __name__ == "__main__":
    sys.exit(main())
