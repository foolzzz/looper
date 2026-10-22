"""Operator credentials stay outside the real agent launch inputs (disposable PostgreSQL only)."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression import dialogs, runner

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")

_CAPTURE = r'''from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import FrameType

capture, inner = map(Path, sys.argv[1:3])
args = sys.argv[3:]
capture.mkdir(parents=True, exist_ok=True)
token = inner.parent.parent / "runtime" / "operator-token"
if token.is_file():
    try:
        with (capture / "token").open("x", encoding="utf-8") as handle:
            handle.write(token.read_text(encoding="utf-8"))
    except FileExistsError:
        pass

files: dict[str, str] = {}
flags = {"--append-system-prompt-file", "--system-prompt-file", "--mcp-config", "--settings", "--plugin-dir"}
for index, arg in enumerate(args):
    flag, equals, value = arg.partition("=")
    if flag not in flags:
        continue
    if not equals:
        value = args[index + 1]
    path = Path(value)
    if flag == "--settings":
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
    paths = sorted(path.rglob("*")) if flag == "--plugin-dir" else [path]
    for named in paths:
        if not named.is_file():
            continue
        try:
            text = named.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "\0" not in text:
            files[str(named)] = text

record = {"argv": [str(inner.with_name("claude")), *args], "env": dict(os.environ),
          "cwd": os.getcwd(), "stdin": "", "files": files, "finished": False}
target = capture / (uuid.uuid4().hex + ".json")
target.write_text(json.dumps(record), encoding="utf-8")
child = subprocess.Popen([str(inner), *args], stdin=subprocess.PIPE)

def forward(number: int, _frame: FrameType | None) -> None:
    if child.poll() is None:
        child.send_signal(number)

signal.signal(signal.SIGTERM, forward)
signal.signal(signal.SIGINT, forward)
chunks: list[bytes] = []

def relay() -> None:
    try:
        while data := os.read(sys.stdin.fileno(), 65536):
            chunks.append(data)
            child.stdin.write(data)
            child.stdin.flush()
    except BrokenPipeError:
        pass
    finally:
        try:
            child.stdin.close()
        except BrokenPipeError:
            pass

threading.Thread(target=relay, daemon=True).start()
status = child.wait()
record.update(stdin=b"".join(chunks).decode("utf-8", errors="replace"), finished=True)
temporary = target.with_suffix(".tmp")
temporary.write_text(json.dumps(record), encoding="utf-8")
temporary.replace(target)
sys.exit(status if status >= 0 else 128 - status)
'''


def _records(captures: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(captures.glob("*/*.json"))]


def _roles(records: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    for record in records:
        prompt = "\n".join(record["files"].values())
        for role, pattern in {
            "Manager": r"^You are the Manager Agent",
            "Planner": r"^You are the Planner of one Looper task",
            "Worker": r"^Your step:",
            "Reviewer": r"^The step under review:",
        }.items():
            if re.search(pattern, prompt, re.MULTILINE):
                found.add(role)
    return found


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_operator_token_is_absent_from_every_agent_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(runner.tempfile, "tempdir", str(tmp_path))
    captures = tmp_path / "captures"
    original = runner.install_claude
    installed: list[Path] = []

    def install(bin_dir: Path, **kwargs: Any) -> Path:
        script = original(bin_dir, **kwargs)
        inner = script.with_name("claude-inner")
        script.rename(inner)
        capture = captures / str(len(installed))
        installed.append(capture)
        program = bin_dir / "capture.py"
        program.write_text(_CAPTURE, encoding="utf-8")
        command = [sys.executable, str(program), str(capture), str(inner)]
        script.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(arg) for arg in command) + ' "$@"\n',
                          encoding="utf-8")
        script.chmod(0o755)
        return script

    monkeypatch.setattr(runner, "install_claude", install)
    monkeypatch.setattr(dialogs, "install_claude", install)
    result, _ = runner.run(mode="fake", tasks=runner.select_tasks(["reviewed_step"]), admin_url=str(DSN),
                           out_dir=tmp_path / "tasks", user_gates="off", task_timeout=180)
    assert result["aborted"] is None, result["aborted"]
    assert [row["outcome"] for row in result["tasks"]] == ["pass"], result["tasks"]
    if "Manager" not in _roles(_records(captures)):
        result, _ = dialogs.run(mode="fake", dialogs=dialogs.select(["ask_project"]), admin_url=str(DSN),
                                out_dir=tmp_path / "dialogs", timeout=180)
        assert result["aborted"] is None, result["aborted"]
        assert [row["outcome"] for row in result["dialogs"]] == ["pass"], result["dialogs"]

    records = _records(captures)
    assert _roles(records) == {"Manager", "Planner", "Worker", "Reviewer"}
    tokens = [(capture / "token").read_text(encoding="utf-8").strip() for capture in installed]
    assert tokens and all(tokens)
    for record in records:
        assert record["finished"], "the capture process did not finish recording a launch"
        assert not any("OPERATOR_TOKEN" in name.upper() for name in record["env"])
        encoded = json.dumps(record)
        assert all(token not in encoded for token in tokens), "an agent launch contained the operator credential"
    for role in ("Manager", "Planner", "Worker", "Reviewer"):
        assert any(_roles([record]) == {role} and record["stdin"] and record["files"] for record in records), role
