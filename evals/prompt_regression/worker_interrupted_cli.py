"""Role-selective CLI recorder for the interruption eval; only Workers can be real."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading


def emit(event):
    print(json.dumps(event), flush=True)


def answer(report):
    text = json.dumps(report)
    emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text,
          "total_cost_usd": 0, "usage": {"input_tokens": 10, "output_tokens": 10}})


def content(line):
    value = json.loads(line)["message"]["content"]
    return value if isinstance(value, str) else "".join(block.get("text", "") for block in value)


def fake_worker(user):
    # Scripted positive/negative controls for the scoring path, never model-quality evidence.
    emit({"type": "system", "subtype": "init", "session_id": "eval-worker", "tools": ["Bash", "Read", "Write"]})

    def call(name, inputs, operation):
        ident = f"call-{call.count}"
        call.count += 1
        emit({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": ident, "name": name, "input": inputs}]}})
        result = operation()
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": ident, "content": result}]}})

    call.count = 0

    def bash(command):
        call("Bash", {"command": command}, lambda: subprocess.check_output(command, shell=True, text=True))

    if "This step was interrupted before (" in user:
        bash("git status --short")
        call("Read", {"file_path": str(Path("publish.py").resolve())}, lambda: Path("publish.py").read_text())
        bash(f"{sys.executable} publish.py --status release-1")
    else:
        bash(f"{sys.executable} publish.py release-1")
    call("Write", {"file_path": str(Path("result.txt").resolve()), "content": "published release-1\n"},
         lambda: (Path("result.txt").write_text("published release-1\n"), "written")[1])
    answer({"status": "completed", "changed_files": {"result.txt": "Release receipt"},
            "summary": "Published release-1 and wrote result.txt"})


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    args = sys.argv[2:]
    if config["mode"] == "real" and ("--version" in args or args[:1] == ["auth"]):
        raise SystemExit(subprocess.call([config["claude"], *args]))
    if "--version" in args:
        print("2.1.285 (Claude Code)")
        return
    if args[:1] == ["auth"]:
        print(json.dumps({"loggedIn": True}))
        return
    flag = next((flag for flag in ("--system-prompt-file", "--append-system-prompt-file") if flag in args), None)
    system = Path(args[args.index(flag) + 1]).read_text() if flag else ""
    if not re.search(r"^Your step:", system, re.MULTILINE):
        emit({"type": "system", "subtype": "init", "session_id": "eval-planner"})
        # Recovery already has a saved plan. Script only its acceptance decision.
        for line in sys.stdin:
            if line.strip():
                answer({"decision": "accept", "reason": "Worker finished", "summary": "Release complete",
                        "final": {"delivered": "Release complete", "how_to_use": "Read result.txt",
                                  "verification": "Receipt written", "open_issues": "none"}})
        return
    line = sys.stdin.readline()
    real_args = args + (["--max-budget-usd", str(config["budget"])] if config["mode"] == "real" else [])
    files = {}
    for flag in ("--settings", "--mcp-config"):
        if flag in args:
            value = args[args.index(flag) + 1]
            if not value.startswith("{") and Path(value).is_file():
                files[flag] = Path(value).read_text()
    Path(config["capture"]).write_text(json.dumps({"system_prompt": system, "user_prompt": content(line),
        "command": real_args, "cwd": os.getcwd(), "launch_files": files,
        "workspace": {path.name: path.read_text() for path in Path.cwd().iterdir() if path.is_file()},
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}))
    if config["mode"] == "fake":
        fake_worker(content(line))
        return
    # Preserve the real Worker arguments/stdin and tool isolation. No replay prompt is constructed.
    process = subprocess.Popen([config["claude"], *real_args], stdin=subprocess.PIPE)

    def forward():
        try:
            process.stdin.write(line.encode())
            process.stdin.flush()
            for more in sys.stdin:
                process.stdin.write(more.encode())
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    threading.Thread(target=forward, daemon=True).start()
    raise SystemExit(process.wait())


if __name__ == "__main__":
    main()
