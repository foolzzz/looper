"""The ``claude`` the task Manager eval puts on PATH: it records every launch, then runs a stand-in or real Claude Code.

``python -m evals.prompt_regression.task_manager_cli CONFIG.json <claude arguments...>``

Both modes first write what Looper launched it with (arguments, the system prompt file, the settings and MCP files,
the working directory, the names of the environment variables) to ``capture_dir``. Real mode then replaces itself with
the real executable, unchanged: no prompt is built here. Fake mode plays a scripted model through the same stream-json
protocol, runs the scripted tool calls for real (a shell, file edits, the ``looper`` MCP server), and so produces
records of the shape a real run produces. The script is the case's, never a model's claim.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .fake_claude import _Stdin, _emit, _mcp, _user_text

VERSION = "2.1.332"  # looper.agents.task_manager_safety.VERIFIED_CLI_VERSION; the eval refuses a real CLI that differs
TOKEN = re.compile(r"taskmgr\.[A-Za-z0-9_\-]+")
BUILTIN_TOOLS = ("Task", "Bash", "Edit", "Glob", "Grep", "NotebookEdit", "Read", "TodoWrite", "Write")
SENTINEL_ENV = ("LOOPER_TASK_MANAGER",)


def redact(text: str) -> str:
    return TOKEN.sub("taskmgr.<token>", text)


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def _file_or_inline(value: str | None) -> Any:
    if value is None:
        return None
    text = value if value.lstrip().startswith("{") else Path(value).read_text(encoding="utf-8")
    try:
        return json.loads(redact(text))
    except json.JSONDecodeError:
        return redact(text)


def capture(config: dict[str, Any], args: list[str]) -> Path:
    prompt_flag = next((f for f in ("--append-system-prompt-file", "--system-prompt-file") if f in args), None)
    record = {
        "args": [redact(a) for a in args],
        "cwd": os.getcwd(),
        "system_prompt": Path(_flag(args, prompt_flag)).read_text(encoding="utf-8") if prompt_flag else "",
        "settings": _file_or_inline(_flag(args, "--settings")),
        "mcp_config": _file_or_inline(_flag(args, "--mcp-config")),
        "resume": _flag(args, "--resume"),
        "model": _flag(args, "--model"),
        "env_names": sorted(os.environ),
        "env_values": {name: os.environ.get(name) for name in SENTINEL_ENV},
        "looper_edit_on_path": shutil.which("looper-edit") is not None,
        "mode": config["mode"],
        "tools_list": None,
    }
    directory = Path(config["capture_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"launch-{time.time_ns()}-{os.getpid()}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def apply_arm(config: dict[str, Any], args: list[str]) -> list[str]:
    """The launch the case's arm asks for. ``shipped`` is Looper's own, unchanged. ``bash_only`` drops the fixed
    instructions' edit line from the appended prompt (a copy in ``capture_dir``) and every PATH entry holding
    ``looper-edit``; a prompt without that line exactly once, or a ``looper-edit`` still found, stops the launch."""
    if config.get("arm", "shipped") == "shipped":
        return args
    from looper.agents.task_manager_brief import EDIT_LINE

    flag = "--append-system-prompt-file"
    text = Path(_flag(args, flag) or "/nonexistent").read_text(encoding="utf-8") if flag in args else ""
    if text.count(EDIT_LINE + "\n") != 1:
        raise SystemExit("bash_only arm: the launch's prompt does not hold the edit line exactly once")
    copy = Path(config["capture_dir"]) / f"prompt-bash-only-{time.time_ns()}.md"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(text.replace(EDIT_LINE + "\n", "", 1), encoding="utf-8")
    os.environ["PATH"] = os.pathsep.join(entry for entry in os.environ.get("PATH", "").split(os.pathsep)
                                         if not os.path.exists(os.path.join(entry, "looper-edit")))
    if shutil.which("looper-edit"):
        raise SystemExit("bash_only arm: looper-edit is still on PATH")
    changed = list(args)
    changed[changed.index(flag) + 1] = str(copy)
    return changed


def _fill(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in mapping.items():
            value = value.replace(f"<{key}>", replacement)
        return value
    if isinstance(value, list):
        return [_fill(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, mapping) for key, item in value.items()}
    return value


class Scripted:
    """One resident fake process: init and a result per user turn, tool calls made for real in between."""

    def __init__(self, config: dict[str, Any], args: list[str], launch_file: Path) -> None:
        self.config, self.args, self.launch_file = config, args, launch_file
        self.stdin = _Stdin()
        self.sessions = Path(config["capture_dir"]) / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.disallowed = set((_flag(args, "--disallowedTools") or "").split(","))
        self.model = _flag(args, "--model") or "fake"
        resume = _flag(args, "--resume")
        if resume and not (self.sessions / resume).exists():
            print(f"No conversation found with session ID: {resume}", file=sys.stderr)
            raise SystemExit(1)
        self.session = resume or str(uuid.uuid4())
        self.memory_file = self.sessions / self.session
        self.memory_file.touch()
        self.cost = 0.0
        self.rpc = _mcp(args) if _flag(args, "--mcp-config") else None
        self.mcp_tools: list[dict[str, Any]] = []
        if self.rpc:
            self.mcp_tools = self.rpc("tools/list", {})["result"]["tools"]
            record = json.loads(launch_file.read_text())
            record["tools_list"] = self.mcp_tools
            launch_file.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        self.mapping = {"TASK": os.environ.get("LOOPER_TASK_MANAGER", ""), "WORK": os.getcwd(),
                        **config.get("placeholders", {})}
        self.calls = 0

    # -- protocol ---------------------------------------------------------------------------------------------

    def tool_names(self, plan: dict[str, Any] | None = None) -> list[str]:
        builtin = [name for name in BUILTIN_TOOLS if name not in self.disallowed]
        names = [*builtin, *(f"mcp__looper__{tool['name']}" for tool in self.mcp_tools)]
        plan = plan or {}
        return [n for n in names if n not in plan.get("tools_drop", [])] + list(plan.get("tools_add", []))

    def result(self, text: str, *, subtype: str = "success") -> None:
        _emit({"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": text,
               "session_id": self.session, "total_cost_usd": round(self.cost, 6),
               "usage": {"input_tokens": 10, "output_tokens": 5}})

    def tool(self, name: str, arguments: dict[str, Any]) -> bool:
        """Emit the call, run it, emit its result; True when the user's interrupt ended the turn on the way."""
        self.calls += 1
        ident = f"toolu_{self.calls:04d}"
        _emit({"type": "assistant", "session_id": self.session, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": ident, "name": name, "input": arguments}]}})
        waiting = (re.search(r"select\(undef,\s*undef,\s*undef,\s*(\d+)\)", str(arguments.get("command", "")))
                   if name == "Bash" else None)
        if waiting and self.sleep(float(waiting.group(1))):
            return True
        content, is_error = ("", False) if waiting else self.run(name, arguments)
        _emit({"type": "user", "session_id": self.session, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ident, "content": content, **({"is_error": True} if is_error else {})}]}})
        return False

    def run(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name in self.disallowed:
            return f"No such tool available: {name}", True
        if name == "Bash":
            done = subprocess.run(arguments["command"], shell=True, capture_output=True, text=True, timeout=120)
            output = done.stdout + done.stderr
            return (f"Exit code {done.returncode}\n{output}" if done.returncode else output), bool(done.returncode)
        if name == "Read":
            path = Path(arguments["file_path"])
            return (path.read_text() if path.exists() else f"File does not exist: {path}"), not path.exists()
        if name == "Write":
            path = Path(arguments["file_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(arguments["content"])
            return f"File created successfully at: {path}", False
        if name == "Edit":
            path = Path(arguments["file_path"])
            text = path.read_text()
            if arguments["old_string"] not in text:
                return "String to replace not found in file.", True
            path.write_text(text.replace(arguments["old_string"], arguments["new_string"], 1))
            return f"The file {path} has been updated.", False
        if name.startswith("mcp__looper__") and self.rpc:
            answer = self.rpc("tools/call", {"name": name.removeprefix("mcp__looper__"), "arguments": arguments})
            result = answer.get("result") or {}
            text = "".join(block.get("text", "") for block in result.get("content", []))
            return text or json.dumps(answer.get("error", answer)), bool(result.get("isError") or "error" in answer)
        return f"No such tool available: {name}", True

    def sleep(self, seconds: float) -> bool:
        """Wait ``seconds`` while reading stdin; an interrupt ends the turn, as Claude Code does."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            line = self.stdin.readline(timeout=0.2)
            if line is None:
                continue
            data = json.loads(line) if line.strip() else {}
            if data.get("type") == "control_request":
                _emit({"type": "control_response", "response": {"subtype": "success",
                                                                  "request_id": data["request_id"]}})
                self.result("", subtype="error_during_execution")
                return True
        return False

    def turn(self, content: str) -> None:
        # The last of the matching steps' phrases to appear is the question being asked now: a restarted session's
        # input holds the earlier turns' text before it.
        matching = [s for s in self.config["script"] if _fill(s["match"], self.mapping) in content]
        step = max(matching, key=lambda s: content.rfind(_fill(s["match"], self.mapping)), default=None)
        plan = step[self.config["behavior"]] if step else None
        _emit({"type": "system", "subtype": "init", "session_id": self.session, "model": self.model,
               "tools": self.tool_names(plan), "cwd": os.getcwd()})
        if step is None:
            self.cost += .001
            return self.finish("No scripted answer for this message.")
        if "remember" in plan:
            self.memory_file.write_text(plan["remember"])
        for name, arguments in plan.get("calls", []):
            if self.tool(name, _fill(arguments, self.mapping)):
                return
        reply = plan.get("reply", "")
        memory = self.memory_file.read_text()
        if "recall" in plan:
            reply = plan["recall"].format(memory=memory) if memory else plan.get("unknown", "I do not know.")
        if "requires_input" in plan and _fill(plan["requires_input"], self.mapping) not in content:
            reply = plan.get("unknown", "I do not have that.")
        self.cost += .002
        self.finish(_fill(reply, self.mapping))

    def finish(self, text: str) -> None:
        _emit({"type": "stream_event", "session_id": self.session,
               "event": {"delta": {"type": "text_delta", "text": text}}})
        _emit({"type": "assistant", "session_id": self.session,
               "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
        self.result(text)

    def loop(self) -> None:
        while (line := self.stdin.readline()) is not None:
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("type") == "control_request":
                _emit({"type": "control_response", "response": {"subtype": "success", "request_id": data["request_id"]}})
                self.result("", subtype="error_during_execution")
                continue
            self.turn(_user_text(line))


def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text())
    args = sys.argv[2:]
    real = config["mode"] == "real"
    if "--version" in args or args[:1] == ["auth"]:
        if real:
            os.execv(config["claude"], [config["claude"], *args])
        print(f"{VERSION} (Claude Code)" if "--version" in args else json.dumps({"loggedIn": True}))
        return
    args = apply_arm(config, args)
    launch_file = capture(config, args)
    if real:
        os.execv(config["claude"], [config["claude"], *args])  # Looper's arguments (the arm's prompt copy), stdin, stdout
    Scripted(config, args, launch_file).loop()


if __name__ == "__main__":
    main()
