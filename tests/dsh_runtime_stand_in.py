"""Headless DSH stand-in for disposable serve tests; never contacts a model provider.

It consumes the real adapter's private overlay and MCP environment references,
persists an opaque owner-local session, and runs the real Looper stdio bridge.
It does not implement DSH's execution guard; the official registry tests cover it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def install(root: Path) -> tuple[Path, Path]:
    from fake_claude import fake_claude

    bin_dir, state = root / "bin", root / "stand-in-state"
    bin_dir.mkdir()
    state.mkdir()
    # Smoke exposes the selected executable through another symlink. Unlike
    # fake_claude's argv[0] + '.py' driver, this entry works from either path.
    dsh = bin_dir / "dsh"
    dsh.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                   "from tests.dsh_runtime_stand_in import main\n"
                   f"main(Path({str(state)!r}))\n", encoding="utf-8")
    dsh.chmod(0o755)
    # Startup may check Claude, but must never run the operator's binary/login.
    fake_claude(bin_dir / "claude", "import sys\n"
                "print('2.1.285 (Claude Code)')\n"
                "sys.exit(0 if sys.argv[1:] in (['--version'], ['auth', 'status', '--json']) else 9)\n")
    return bin_dir, state


def read_calls(state: Path) -> list[dict]:
    # One atomic file per process avoids interleaved appends by independent owners.
    return sorted((json.loads(path.read_text()) for path in state.glob("call-*.json")),
                  key=lambda row: (row["home"], row["turn"]))


def _emit(kind: str, **fields) -> None:
    print(json.dumps({"type": kind, **fields}), flush=True)


def _fenced(value: dict) -> str:
    """The Planner parses only a reply that ends with one ```json block."""
    return "```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"


def _platform_call(server: dict, tool: str) -> dict:
    env = {**os.environ, **{key: os.environ[ref] for key, ref in server["envRefs"].items()}}
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "disposable-dsh"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": tool, "arguments": {}}},
    ]
    done = subprocess.run([server["command"], *server["args"]],
                          input="".join(json.dumps(row) + "\n" for row in requests), text=True,
                          capture_output=True, env=env, timeout=15, check=True)
    replies = {row["id"]: row for line in done.stdout.splitlines() if (row := json.loads(line)).get("id")}
    published = {row["name"] for row in replies[2]["result"]["tools"]}
    assert set(server["requiredTools"]) <= published
    assert tool in published
    answer = json.loads(replies[3]["result"]["content"][0]["text"])
    assert answer["status"] == "ok", answer
    return answer


def main(state: Path) -> None:
    args = sys.argv[1:]
    if args == ["--version"]:
        print("0.2.1-rc.2")
        return
    if args == ["--help"] or "--help" in args:
        print("--profile --patch --json --session-id")
        return
    assert args[:2] == ["--profile", "headless"] and args[-1] == "-"
    prompt = sys.stdin.read()  # EOF is part of the real headless protocol.
    patch = Path(args[args.index("--patch") + 1])
    bootstrap = patch.with_name("looper-bootstrap.mjs").read_text()
    options = json.loads(bootstrap.split("await createBootstrap(", 1)[1].rsplit(");", 1)[0])
    server = next(row for row in options["mcp"] if row["serverName"] == "looper")
    required = set(server["requiredTools"])
    role = ("manager" if "looper_overview" in required else "planner" if "looper_task_profiles" in required
            else "reviewer" if prompt.startswith("Review the step") else "worker")
    home = Path(os.environ["DSH_HOME"])
    store = home / "opaque-stand-in-session.json"
    saved = json.loads(store.read_text()) if store.exists() else None
    resumed = "--session-id" in args
    if resumed and (not saved or saved["session"] != args[args.index("--session-id") + 1]
                    or saved["cwd"] != os.getcwd()):
        _emit("error", message="unknown session or cwd; turn was not executed")
        raise SystemExit(7)
    saved = saved if resumed else {"session": str(uuid.uuid4()), "cwd": os.getcwd(), "turn": 0}
    saved["turn"] += 1
    store.write_text(json.dumps(saved))
    _emit("session", sessionId=saved["session"], cwd=os.getcwd())
    _emit("status", phase="turn_start", turn=saved["turn"])
    _emit("status", phase="step_start", turn=saved["turn"], step=1)
    _emit("thinking", text=f"Executing disposable {role} turn")
    _emit("text", text="Checking the disposable task")

    tool, tool_status = None, None
    if role in ("manager", "planner"):
        tool = "looper_overview" if role == "manager" else "looper_task_profiles"
        _emit("tool_call", callId="platform-read", tool=f"mcp__looper__{tool}", input={})
        answer = _platform_call(server, tool)
        tool_status = answer["status"]
        _emit("tool_result", callId="platform-read", status="completed", result=json.dumps(answer))
    if role == "manager":
        reply = "Disposable platform status read successfully."
    elif role == "planner":
        if prompt.startswith("[PLAN]"):
            reply = {"steps": [{"id": "delivery", "instruction": "Write delivery.txt with the requested contents",
                                "acceptance_criteria": ["delivery.txt contains delivered by disposable DSH"],
                                "depends_on": [], "review": True}]}
        elif prompt.startswith("[DECISION]"):
            if "has its Worker's result." in prompt.splitlines()[0]:
                reply = {"decision": "review", "reason": "Check the file independently", "summary": "Written"}
            else:
                reply = {"decision": "accept", "reason": "The independent review passed", "summary": "Delivered",
                         "final": {"delivered": "delivery.txt", "how_to_use": "Read delivery.txt",
                                   "verification": "The Reviewer read the file", "open_issues": "none"}}
        else:
            reply = {"reviews": []}
    elif role == "worker":
        assert not options["readOnly"]
        _emit("tool_call", callId="write-delivery", tool="write", input={"path": "delivery.txt"})
        payload = b"delivered by disposable DSH\n"
        Path("delivery.txt").write_bytes(payload)
        _emit("tool_result", callId="write-delivery", status="completed", result="wrote delivery.txt")
        reply = {"status": "completed", "summary": "Wrote delivery.txt", "deleted_files": [],
                 "changed_files": {"delivery.txt": hashlib.sha256(payload).hexdigest()}}
    else:
        _emit("tool_call", callId="read-delivery", tool="read", input={"path": "delivery.txt"})
        contents = Path("delivery.txt").read_text()
        passed = contents == "delivered by disposable DSH\n"
        _emit("tool_result", callId="read-delivery", status="completed", result=contents)
        reply = {"passed": passed, "summary": "Reviewer read delivery.txt", "findings": [] if passed else ["bad file"]}
    if role == "planner" and isinstance(reply, dict):
        reply = _fenced(reply)

    record = {"role": role, "home": str(home), "session": saved["session"], "turn": saved["turn"],
              "resumed": resumed, "read_only": options["readOnly"], "tool": tool, "tool_status": tool_status}
    (state / f"call-{uuid.uuid4().hex}.json").write_text(json.dumps(record))
    _emit("status", phase="step_end", turn=saved["turn"], step=1,
          usage={"inputTokens": 10, "outputTokens": 4, "cacheReadTokens": 7, "cacheWriteTokens": 2})
    _emit("status", phase="turn_end", reason={"kind": "completed"})
    _emit("final", text=reply if isinstance(reply, str) else json.dumps(reply))
