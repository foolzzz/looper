"""A scripted stand-in for the ``claude`` CLI, for the eval's fake mode.

Usage (through the shim the runner puts on PATH as ``claude``):
    python fake_claude.py <scenarios.json> <claude arguments...>

It speaks Claude Code's stream-json like the real CLI and plays the role the
agent system started it in:

- the Planner (a resident process; its task section has neither of the
  markers below) answers each user turn by its kind: [PLAN] with the task's scripted plan; [DECISION] with
  what a careful Planner decides from the allowed decisions (accept, else
  review a completed step that needs one, else retry a failed one, else fail),
  with a progress note, and an accept with the task's final summary;
  [CHAT] (a change to the task) with the script's ``change_replan`` steps
  when it gives them, else by forwarding the new supplements to the running
  step, if any; [SUPERVISE] with no review and a progress note. With
  ``planner_calls`` (``{"PLAN": [[tool, arguments], ...]}``), it first makes
  that turn kind's calls over its ``looper`` MCP server;
- a Worker (its task section says "Your step:") writes the files its step's
  scripted attempt lists and reports that attempt's status, after the
  attempt's ``seconds`` when it gives them; with ``wait_for_input`` it first
  waits for a forwarded user turn and then writes the files given there;
- a Reviewer ("The step under review:") passes the step, unless its system
  prompt holds one of the script's ``failing_reviews`` (a review role's
  marker): then it fails it;
- the Manager (its system prompt has Looper's tools contract) starts the
  ``looper`` MCP server of its ``--mcp-config`` and, for each user message,
  makes the calls ``scenarios.json``'s ``manager`` lists for that message's
  text (re-read every turn), replying with each call's tool and status. In the
  calls, ``<task id>`` is the last task id an answer gave and
  ``<confirmation>`` the confirmation id the note before the message names.

To show that the eval notices misbehaviour, a script may also make the
Planner write files with the Write tool during [PLAN] (``planner_writes``),
break its first [PLAN] reply (``break_first_plan``), or give a Worker attempt
a final message without the report (``raw`` in the attempt).

The task is found by the process's working directory in ``scenarios.json``
(``{work_dir: script}``). Every result reports the session's running cost.
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import time
from typing import Any

PLANNER_TURN_COST = 0.001
MANAGER_TURN_COST = 0.001
WORKER_RUN_COST = 0.002
REVIEWER_RUN_COST = 0.001


class _Stdin:
    """Lines from fd 0 with a timeout; Python's buffered stdin would hide a waiting line from select."""

    def __init__(self) -> None:
        self._buffer = b""
        self._eof = False

    def readline(self, timeout: float | None = None) -> str | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self._buffer and not self._eof:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([0], [], [], left)
            if not ready:
                return None
            chunk = os.read(0, 65536)
            if not chunk:
                self._eof = True
            self._buffer += chunk
        if not self._buffer:
            return None
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line.decode("utf-8", errors="replace")


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _user_text(line: str) -> str:
    content = json.loads(line).get("message", {}).get("content", "")
    if isinstance(content, list):
        return "".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return str(content)


def _answer(session_id: str, text: str, cost: float, *, writes: dict[str, str] | None = None) -> None:
    _emit({"type": "system", "subtype": "init", "session_id": session_id, "model": "fake"})
    for name, content in (writes or {}).items():
        path = os.path.join(os.getcwd(), name)
        _emit({"type": "assistant", "session_id": session_id, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"toolu_{len(name)}", "name": "Write",
             "input": {"file_path": path, "content": content}}]}})
        _write(os.getcwd(), {name: content})
        time.sleep(0.5)  # well before the Worker that follows, as a real turn would be
    _emit({"type": "assistant", "session_id": session_id,
           "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    _emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session_id,
           "total_cost_usd": round(cost, 6), "usage": {"input_tokens": 10, "output_tokens": 10}})


def _fenced(value: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"


PROGRESS = {"done": "the steps accepted so far", "now": "the next step", "next": "the rest of the plan",
            "risks": "none", "estimate": "a minute"}
# Kept only on the accept that completes the task.
FINAL = {"delivered": "the task's files", "how_to_use": "read them", "verification": "each step was checked",
         "open_issues": "none"}


def _decide(message: str) -> dict[str, Any]:
    allowed_line = re.search(r"^Allowed decisions now: (.*)\.$", message, re.MULTILINE)
    allowed = {item.split(" ")[0] for item in (allowed_line.group(1).split(", ") if allowed_line else [])}
    if "accept" in allowed:
        return {"decision": "accept", "reason": "meets the acceptance criteria", "summary": "The step is done.",
                "final": FINAL}
    if "review" in allowed and "needs a passing review" in message:
        return {"decision": "review", "reason": "the step asks for an independent review"}
    if "retry" in allowed:
        return {"decision": "retry", "reason": "the step failed", "feedback": "Run the step again."}
    return {"decision": "fail", "reason": "the step cannot be done"}


def _chat(script: dict[str, Any], message: str) -> dict[str, Any]:
    if script.get("change_replan"):
        return {"reply": "Replanning the steps not started.", "action": "replan", "steps": script["change_replan"]}
    running = re.search(r"^Running now: (\S+) \(", message, re.MULTILINE)
    added = message.split("(new supplements, oldest first; every message the user sends you is one):\n", 1)
    text = added[-1].split("\n\n", 1)[0].removeprefix("- ").strip()
    if running:
        return {"reply": "Passing it on to the running step.", "action": "forward", "step": running.group(1),
                "text": text}
    return {"reply": "Noted.", "action": "none"}


def _mcp(args: list[str]) -> Any:
    """A call of a tool on the ``looper`` MCP server of the ``--mcp-config`` in ``args``: ``rpc(method, params)``."""
    config = json.load(open(args[args.index("--mcp-config") + 1], encoding="utf-8"))["mcpServers"]["looper"]
    server = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              env={**os.environ, **config.get("env", {})}, text=True)
    ids = iter(range(1, 1_000_000))

    def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert server.stdin is not None and server.stdout is not None
        server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params}) + "\n")
        server.stdin.flush()
        return json.loads(server.stdout.readline())

    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake"}})
    return rpc


def _planner(script: dict[str, Any], args: list[str], stdin: _Stdin) -> None:
    cost = 0.0
    broken = 0
    calls = script.get("planner_calls") or {}
    rpc = _mcp(args) if calls else None
    while (line := stdin.readline()) is not None:
        if not line.strip():
            continue
        message = _user_text(line)
        writes = None
        kind = re.match(r"\[(\w+)\]", message)
        for name, arguments in calls.get(kind.group(1) if kind else "", []):
            rpc("tools/call", {"name": name, "arguments": arguments})
        if message.startswith("[PLAN]") and broken < int(script.get("break_first_plan", 0)):
            broken += 1
            cost += PLANNER_TURN_COST
            _answer("fake-planner", "Here is the plan: s1 creates the file.", cost)
            continue
        if message.startswith("[PLAN]"):
            reply = {"steps": script["plan"]}
            writes = script.get("planner_writes")
        elif message.startswith("[DECISION]"):
            reply = {**_decide(message), "progress": PROGRESS}
        elif message.startswith("[CHAT]"):
            reply = _chat(script, message)
        else:
            reply = {"reviews": [], "progress": PROGRESS}
        cost += PLANNER_TURN_COST
        _answer("fake-planner", "Done.\n\n" + _fenced(reply), cost, writes=writes)


def _write(work_dir: str, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = os.path.join(work_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)


def _worker(script: dict[str, Any], system_prompt: str, stdin: _Stdin) -> None:
    stdin.readline()  # the start cue
    step = re.search(r"^Your step: (\S+)$", system_prompt, re.MULTILINE)
    label = step.group(1) if step else ""
    retried = re.search(r"being retried \(attempt (\d+)\)", system_prompt)
    attempt_no = int(retried.group(1)) if retried else 1
    attempts = script.get("attempts", {}).get(label) or [{"status": "completed", "files": {}}]
    attempt = attempts[min(attempt_no, len(attempts)) - 1]
    files = dict(attempt.get("files") or {})
    wait = (script.get("wait_for_input") or {}).get(label)
    if wait and attempt_no == 1 and stdin.readline(timeout=float(wait.get("seconds", 10))) is not None:
        files = dict(wait.get("files") or {})
    time.sleep(float(attempt.get("seconds", 0)))
    work_dir = os.getcwd()
    _write(work_dir, files)
    report = {"status": attempt.get("status", "completed"),
              "changed_files": {name: "written" for name in files},
              "deleted_files": [], "summary": attempt.get("summary") or f"step {label} attempt {attempt_no}"}
    _answer("fake-worker", attempt.get("raw") or "Done.\n" + json.dumps(report), WORKER_RUN_COST)


def _reviewer(script: dict[str, Any], system_prompt: str, stdin: _Stdin) -> None:
    stdin.readline()  # the Worker's report
    if any(marker in system_prompt for marker in script.get("failing_reviews") or []):
        verdict = {"passed": False, "findings": ["the greeting is not what this role asks for"],
                   "summary": "Not to this role's liking."}
    else:
        verdict = {"passed": True, "findings": [], "summary": "Meets the criteria."}
    _answer("fake-reviewer", json.dumps(verdict), REVIEWER_RUN_COST)


def _main_checkout() -> str:
    """The task directory of a step that runs in a worktree of its own: the repository's main checkout."""
    done = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True,
                          text=True, check=False)
    return os.path.realpath(os.path.dirname(done.stdout.strip())) if done.returncode == 0 else ""


def _manager(scenarios_path: str, args: list[str], stdin: _Stdin) -> None:
    rpc = _mcp(args)
    cost, last_task = 0.0, ""
    while (line := stdin.readline()) is not None:
        if not line.strip():
            continue
        content = _user_text(line)
        channel = re.search(r"^Channel: .*\n\n", content, re.MULTILINE)
        message = content[channel.end():] if channel else content
        named = re.search(r"confirmation_id (c-[0-9a-f]+)", content)
        with open(scenarios_path, encoding="utf-8") as handle:
            calls = (json.load(handle).get("manager") or {}).get(message.strip(), [])
        answers = []
        for name, arguments in calls:
            text = json.dumps(arguments).replace("<confirmation>", named.group(1) if named else "")
            result = rpc("tools/call", {"name": name, "arguments": json.loads(text.replace("<task id>", last_task))})
            answer = json.loads(result["result"]["content"][0]["text"])
            created = answer.get("result") if isinstance(answer.get("result"), dict) else {}
            last_task = created.get("task_id") or last_task
            answers.append(answer)
        cost += MANAGER_TURN_COST
        done = "；".join(f"{name}：{answer.get('status')}" for (name, _), answer in zip(calls, answers))
        _answer("fake-manager", f"已处理（{done}）。" if answers else "放在哪个项目、哪个目录？", cost)


def main(argv: list[str]) -> int:
    scenarios_path, args = argv[0], argv[1:]
    with open(scenarios_path, encoding="utf-8") as handle:
        scenarios = json.load(handle)
    script = scenarios.get(os.path.realpath(os.getcwd())) or scenarios.get(_main_checkout(), {})
    system_prompt = ""
    if "--append-system-prompt-file" in args:
        with open(args[args.index("--append-system-prompt-file") + 1], encoding="utf-8") as handle:
            system_prompt = handle.read()
    stdin = _Stdin()
    # The role is in the task section: a Reviewer's names the step under review, a Worker's its step; the
    # Manager's prompt ends with Looper's tools contract.
    if "## Looper's tools (required)" in system_prompt:
        _manager(scenarios_path, args, stdin)
    elif re.search(r"^The step under review:", system_prompt, re.MULTILINE):
        _reviewer(script, system_prompt, stdin)
    elif re.search(r"^Your step:", system_prompt, re.MULTILINE):
        _worker(script, system_prompt, stdin)
    else:
        _planner(script, args, stdin)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
