"""A completed or failed task closes out its vault through the real Planner tools.

Uses a stand-in Claude and a disposable database only when
``LOOPER_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from evals.prompt_regression.runner import fresh_database, running_server
from tests.db_support import DSN
from tests.test_restart_resume_db import _log, _wait

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

RULES = "Keep one topic per note. Search before adding a near-duplicate."

_FAKE_CLAUDE = r'''
import json, os, re, sys, urllib.request
STATE = sys.argv[1]
args = sys.argv[2:]
if args[:1] in (["--version"], ["auth"]):
    sys.exit(0)
prompt = ""
for flag in ("--append-system-prompt-file", "--system-prompt-file"):
    if flag in args:
        prompt = open(args[args.index(flag) + 1], encoding="utf-8").read()
        break

def log(name, value):
    with open(os.path.join(STATE, name), "a") as f:
        f.write(json.dumps(value) + "\n")

def text_of(line):
    content = json.loads(line)["message"]["content"]
    return content if isinstance(content, str) else "".join(b.get("text", "") for b in content)

def answer(session, reply):
    text = "Done.\n\n```json\n" + json.dumps(reply) + "\n```"
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session, "model": "fake"}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                      "session_id": session, "total_cost_usd": 0.001}), flush=True)

def write_note(path):
    config = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["looper"]["env"]
    body = {"path": path, "content": "# Build\n\nRun make gen first.\n", "expected_sha256": None,
            "reason": "keep the build quirk"}
    req = urllib.request.Request(config["LOOPER_AGENT_URL"].rstrip("/") + "/api/agent/v1/ops/looper_vault_write",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + config["LOOPER_AGENT_TOKEN"]})
    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.load(response)
    log("writes.jsonl", {"path": path, "answer": result})

if re.search(r"^Your step: ", prompt, re.MULTILINE):
    text_of(sys.stdin.readline())
    answer("fake-worker", {"status": os.environ["FAKE_STEP_STATUS"], "changed_files": {}, "deleted_files": [],
                           "summary": "build needs make gen first"})
else:
    for line in sys.stdin:
        if not line.strip():
            continue
        text = text_of(line)
        log("planner_turns.jsonl", text)
        if text.startswith("[PLAN]"):
            reply = {"steps": [{"id": "build", "instruction": "build the package", "acceptance_criteria": ["it builds"],
                                "depends_on": [], "review": False}]}
        elif text.startswith("[VAULT]"):
            write_note("codex-mem/lessons.md")
            write_note("mine/diary.md")
            reply = {"vault": "wrote codex-mem/lessons.md"}
        elif os.environ["FAKE_STEP_STATUS"] == "failed":
            reply = {"decision": "fail", "reason": "the build failed"}
        else:
            reply = {"decision": "accept", "reason": "meets the criteria", "summary": "package built",
                     "final": {"delivered": "package built", "how_to_use": "run it", "verification": "build passed",
                               "open_issues": "none"}}
        answer("fake-planner", reply)
'''


def _install(root: Path, step_status: str) -> tuple[Path, Path]:
    state, bin_dir = root / "state", root / "bin"
    state.mkdir()
    bin_dir.mkdir()
    script = root / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE, encoding="utf-8")
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\n"
                      f"export FAKE_STEP_STATUS={shlex.quote(step_status)}\n"
                      f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "
                      f'{shlex.quote(str(state))} "$@"\n', encoding="utf-8")
    claude.chmod(0o755)
    return bin_dir, state


def _git(vault: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", "-C", str(vault), "-c", "commit.gpgsign=false", *args],
                          env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=True, timeout=30).stdout.strip()


@pytest.mark.parametrize("mode,step_status", [("on", "completed"), ("off", "completed"), ("on", "failed")])
def test_task_vault_closeout_is_once_and_preserves_its_result(tmp_path: Path, mode: str, step_status: str) -> None:
    root = tmp_path.resolve()
    bin_dir, state = _install(root, step_status)
    runtime, work, home, vault = (root / name for name in ("runtime", "work", "home", "vault"))
    for path in (runtime, work, home, vault):
        path.mkdir()
    (vault / "codex-mem").mkdir()
    (vault / "mine").mkdir()
    (vault / "codex-mem/AGENTS.md").write_text(RULES, encoding="utf-8")
    _git(vault, "init", "-q")
    _git(vault, "config", "user.name", "Vault Test")
    _git(vault, "config", "user.email", "vault-test@localhost")
    _git(vault, "add", "--", "codex-mem/AGENTS.md")
    _git(vault, "commit", "-qm", "initial rules")
    initial = _git(vault, "rev-parse", "HEAD")

    with fresh_database(str(DSN)) as (database_url, _):
        with running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                            log_path=root / "serve.log") as api:
            api.put("/api/knowledge-vault", {"path": str(vault), "writable": ["codex-mem"],
                                             "rules_file": "codex-mem/AGENTS.md"})
            project = api.post("/api/projects", {"name": "build", "work_dir": str(work), "vault": mode,
                                                 "distill_lessons": "off", "agent_room": "off",
                                                 "operator_notes": "off", "task_workspaces": "in_place"})
            task_id = api.post("/api/tasks/create", {"requirement": "build the package",
                                                       "project_id": project["project_id"]})["task_id"]
            api.post("/api/v1/ops/task_control/start", {"task_id": task_id})
            done = _wait(api, task_id, lambda detail: detail["status"] in ("completed", "failed", "stopped"))
            activity = [row["summary"] for row in api.post("/api/v1/tasks/activity", {"task_id": task_id, "since": 0, "limit": 500})["result"]["activity"]]

    assert done["status"] == step_status, (done.get("error"), (root / "serve.log").read_text()[-3000:])
    turns = [text for text in _log(state, "planner_turns.jsonl") if text.startswith("[VAULT]")]
    assert not (vault / "mine/diary.md").exists()
    if mode == "off":
        assert turns == [] and _git(vault, "rev-parse", "HEAD") == initial
        assert not (vault / "codex-mem/lessons.md").exists()
        return
    assert len(turns) == 1 and RULES in turns[0]
    ended = "completed." if step_status == "completed" else "failed: "
    assert turns[0].startswith(f"[VAULT] The task {ended}")
    assert _git(vault, "rev-list", "--count", f"{initial}..HEAD") == "1"
    assert _git(vault, "log", "-1", "--format=%s") == f"looper: {task_id} planner: keep the build quirk"
    assert _git(vault, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "codex-mem/lessons.md"
    writes = {row["path"]: row["answer"] for row in _log(state, "writes.jsonl")}
    assert writes["codex-mem/lessons.md"]["status"] == "ok"
    assert writes["mine/diary.md"]["error"]["code"] == "forbidden"
    assert any(line.startswith("Vault: planner wrote codex-mem/lessons.md") for line in activity)
    assert "Vault closeout: wrote codex-mem/lessons.md" in activity
