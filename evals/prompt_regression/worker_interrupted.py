"""One fake-only prompt regression: a new Worker run of an interrupted attempt.

Uses the real attempt records, Worker launch and recorder with a local CLI stand-in.
It checks prompt delivery, not whether a model follows the recovery instruction.
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from looper.db import connection, llm_records, step_attempts, system_settings

from .runner import fresh_database, init_repository

EXPECTED_LINE = (
    "This step was interrupted before (restart not adopted); the workspace may hold partial changes from that run. "
    "Check git status and the files you will touch before continuing, and do not repeat external side effects "
    "(pushes, comments, messages) without checking whether they already happened."
)


async def run_case(root: Path, db, *, mode: str = "fake") -> dict:
    if mode != "fake":
        raise ValueError("worker_interrupted supports fake mode only")
    work = root / "work"
    work.mkdir()
    (work / "partial.txt").write_text("original\n")
    init_repository(work)
    (work / "partial.txt").write_text("unfinished edit\n")
    capture = root / "received.json"
    executable = root / "fake-claude"
    report = {"type": "result", "subtype": "success", "is_error": False,
              "result": json.dumps({"status": "completed", "changed_files": {}, "summary": "checked workspace"})}
    executable.write_text(
        f"#!{sys.executable}\nimport json, pathlib, sys\n"
        "if '--version' in sys.argv:\n    print('2.1.285 (Claude Code)'); sys.exit(0)\n"
        f"pathlib.Path({str(capture)!r}).write_text(sys.stdin.readline())\n"
        f"print({json.dumps(report)!r}, flush=True)\nsys.stdin.read()\n"
    )
    executable.chmod(0o755)
    await db.execute("INSERT INTO projects(id,name,work_dir) VALUES ('recovery-project','Recovery fixture',$1)", str(work))
    await db.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('recovery-task','Finish the edit','recovery-project')")
    await db.execute("INSERT INTO task_steps(id,task_id,step_index,instruction) "
                     "VALUES ('recovery-step','recovery-task',0,'Inspect the partial edit')")
    attempt = await step_attempts.open_attempt("recovery-task", "recovery-step", "edit", 1, "first", pool=db)
    old = await llm_records.start_record(agent_id="worker-before", task_id="recovery-task", pool=db)
    await step_attempts.worker_started(attempt, old, None, pool=db)
    await llm_records.end_record_interrupted(old, "not_adopted", pool=db)
    await system_settings.put_settings({"worker.interrupted_hint.enabled": "true"}, pool=db)
    pool = WorkerPool(config=WorkerConfig(claude_bin=str(executable)), runtime_root=root / "runtime")
    item = MessageEnvelope.create(source=planner_address("recovery-task"), target=worker_address("worker-after"),
        message_type=MessageType.WORK_ITEM, payload={"parent_task_id": "recovery-task", "task_id": "recovery-step",
            "step_key": "edit", "instruction": "Inspect the partial edit", "attempt": 1, "attempt_id": attempt,
            "context": {"work_dir": str(work)}})
    result = await pool._execute(item)
    received = json.loads(capture.read_text())["message"]["content"]
    if isinstance(received, list):
        received = "".join(part.get("text", "") for part in received)
    current = await db.fetchrow("SELECT id,attempt_id,input_text FROM llm_records WHERE agent_id='worker-after'")
    checks = {
        "completed": result.get("status") == "completed",
        "new_run_same_attempt": current is not None and current["id"] != old and current["attempt_id"] == attempt,
        "prompt_delivered_once": received == "Start your step (the Task section of your instructions).\n" + EXPECTED_LINE,
        "prompt_recorded": current is not None and current["input_text"] == received,
        "workspace_kept": (work / "partial.txt").read_text() == "unfinished edit\n",
    }
    return {"case": "worker_interrupted", "mode": "fake", "outcome": "pass" if all(checks.values()) else "fail",
            "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["fake"], default="fake")
    parser.add_argument("--pg-url", required=True, help="Disposable PostgreSQL server admin URL")
    args = parser.parse_args()

    async def run(root: Path, url: str) -> dict:
        db = await connection.init_pool(url)
        try:
            await connection.ensure_schema(db)
            return await run_case(root, db, mode=args.mode)
        finally:
            await connection.close_pool()

    with fresh_database(args.pg_url) as (url, _), tempfile.TemporaryDirectory(prefix="looper-worker-eval-") as temp:
        result = asyncio.run(run(Path(temp), url))
    print(json.dumps(result))
    raise SystemExit(0 if result["outcome"] == "pass" else 1)


if __name__ == "__main__":
    main()
