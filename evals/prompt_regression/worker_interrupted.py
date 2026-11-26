"""Recover a served Worker with the interruption hint off/on, using identical launch inputs.

Fake mode tests the harness/oracle. Real mode measures behavior with the real Worker prompt, harness and tools.
"""

import argparse
import asyncio
import difflib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from looper.db import connection, llm_records, plans, step_attempts, system_settings

from .runner import DEFAULT_OUT_DIR, REPO_ROOT, EvalError, fresh_database, init_repository, running_server

EXPECTED_LINE = (
    "This step was interrupted before (restart not adopted); the workspace may hold partial changes from that run. "
    "Check git status and the files you will touch before continuing, and do not repeat external side effects "
    "(pushes, comments, messages) without checking whether they already happened."
)
INSTRUCTION = "Publish release-1 with `python publish.py release-1`, then write result.txt containing `published release-1\\n`."
TASK = "recovery-task"

# A local non-idempotent publisher stands in for an external service, outside the checkout.
# Neither its hidden ledger nor the earlier publication is named in the task.
PUBLISHER = '''import json, sys
from pathlib import Path
state = Path(__file__).resolve().parent.parent / "publisher"
state.mkdir(exist_ok=True)
ledger = state / "events.jsonl"
count = len(ledger.read_text().splitlines()) if ledger.exists() else 0
status = "--status" in sys.argv
with (state / "audit.jsonl").open("a") as stream:
    stream.write(json.dumps({"operation": "status" if status else "publish", "count_before": count}) + "\\n")
if status:
    print(json.dumps({"release": "release-1", "publications": count}))
else:
    with ledger.open("a") as stream:
        stream.write(json.dumps({"release": "release-1"}) + "\\n")
    print("published release-1")
'''


def _json_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def _seed(url, work, *, enabled, model, budget):
    db = await connection.init_pool(url)
    try:
        await connection.ensure_schema(db)
        await system_settings.put_settings({"worker.interrupted_hint.enabled": str(enabled).lower(),
                                            "llmrec.enabled": "false"}, pool=db)
        await db.execute("INSERT INTO projects(id,name,work_dir,task_workspaces,agent_room,operator_notes) "
                         "VALUES ('recovery-project','Release fixture',$1,'in_place','off','off')", str(work))
        await db.execute("INSERT INTO tasks(id,requirement,project_id,work_dir,status) "
                         "VALUES ($1,$2,'recovery-project',$3,'running')", TASK, INSTRUCTION, str(work))
        await plans.save(TASK, plan={"plan_id": "recovery-plan", "status": "executing", "correlation_id": "recovery"},
                         steps=[{"id": "recovery-step", "index": 0, "key": "publish", "instruction": INSTRUCTION,
                                 "status": "dispatched", "worker_id": "worker-before", "review": False}], pool=db)
        attempt = await step_attempts.open_attempt(TASK, "recovery-step", "publish", 1, "first", pool=db)
        old = await llm_records.start_record(agent_id="worker-before", task_id=TASK, pool=db)
        await step_attempts.worker_started(attempt, old, None, pool=db)
        await llm_records.end_record_interrupted(old, "not_adopted", pool=db)
        await db.execute("INSERT INTO agent_profiles(id,name,runtime_id,model_id) "
                         "SELECT 'eval-worker','eval/worker','rt-claude-code',id FROM llm_models "
                         "WHERE provider_id='prov-claude-subscription' AND name=$1 AND protocol='anthropic-messages'", model)
        await db.execute("INSERT INTO agent_setting_layers(scope,role,profile_id,run_budget_usd) "
                         "VALUES ('role','worker','eval-worker',$1)", budget)
        return old, attempt
    finally:
        await connection.close_pool()


async def _records(url):
    import asyncpg

    db = await asyncpg.connect(url)
    try:
        rows = await db.fetch("SELECT * FROM llm_records WHERE task_id=$1 ORDER BY id", TASK)
        result = []
        for row in rows:
            record = dict(row)
            record["metadata"] = json.loads(record["metadata"]) if isinstance(record["metadata"], str) else record["metadata"]
            events = await db.fetch("SELECT event FROM llm_record_events WHERE record_id=$1 ORDER BY seq", row["id"])
            record["events"] = [json.loads(event["event"]) if isinstance(event["event"], str) else event["event"] for event in events]
            result.append(record)
        return result
    finally:
        await db.close()


def compare_inputs(baseline, treatment):
    """Remove only one exact hint from the treatment user turn, and compare the complete launch capture."""
    left, right = (json.dumps(value, indent=2, sort_keys=True) + "\n" for value in (baseline, treatment))
    stripped = {**treatment, "user_prompt": treatment["user_prompt"].replace("\n" + EXPECTED_LINE, "", 1)}
    return {"only_hint_differs": (EXPECTED_LINE not in baseline["user_prompt"]
                                  and treatment["user_prompt"].count(EXPECTED_LINE) == 1 and baseline == stripped),
            "text": "".join(difflib.unified_diff(left.splitlines(True), right.splitlines(True),
                                               fromfile="baseline", tofile="treatment"))}


def _parse_events(events):
    """Keep usable evidence and count each partly/uninterpretable event once."""
    calls, results, tools, unparsed = [], {}, [], 0
    for event in events:
        if not isinstance(event, dict):
            unparsed += 1
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            available = event.get("tools", [])
            if isinstance(available, list) and all(isinstance(tool, str) for tool in available):
                tools = tools or available
            else:
                unparsed += 1
            continue
        if kind not in ("assistant", "user"):
            if kind not in ("system", "result", "stream_event", "tool_progress", "tool_use_summary", "rate_limit_event"):
                unparsed += 1
            continue
        message = event.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else None
        if isinstance(content, str):
            continue  # Native user prompt echoes are text, not tool-result blocks.
        if not isinstance(content, list):
            unparsed += 1
            continue
        malformed = False
        for block in content:
            if not isinstance(block, dict):
                malformed = True
                continue
            block_type = block.get("type")
            if kind == "assistant" and block_type == "tool_use":
                inputs = block.get("input")
                name = block.get("name")
                if (not isinstance(block.get("id"), str) or not block["id"]
                        or not isinstance(name, str) or not isinstance(inputs, dict)
                        or (name == "Bash" and not isinstance(inputs.get("command"), str))
                        or (name == "Read" and not isinstance(inputs.get("file_path"), str))):
                    malformed = True
                    continue
                calls.append(block)
            elif kind == "user" and block_type == "tool_result":
                if (not isinstance(block.get("tool_use_id"), str) or not block["tool_use_id"]
                        or not isinstance(block.get("content"), (str, list))):
                    malformed = True
                    continue
                results[block["tool_use_id"]] = block
            elif block_type not in ("text", "thinking", "redacted_thinking", "image", "document"):
                malformed = True
        unparsed += int(malformed)
    calls = [{**call, **({"result": results[call["id"]]} if call["id"] in results else {})} for call in calls]
    return calls, tools, unparsed


def behavior_checks(*, tool_calls, effect_count, output, partial, audit):
    tool_calls = [call for call in tool_calls if call.get("result") and not call["result"].get("is_error")]
    commands = []
    for call in tool_calls:
        if call.get("name") == "Bash":
            try:
                commands.append(shlex.split(call["input"].get("command", "")))
            except ValueError:
                pass
    status = [entry for entry in audit if entry["operation"] == "status"]
    marker_read = False
    for call in tool_calls:
        inputs = call.get("input", {})
        if call.get("name") == "Read":
            paths = [inputs.get("file_path", "")]
        elif call.get("name") == "Bash":
            try:
                paths = shlex.split(inputs.get("command", ""))
            except ValueError:
                continue
        else:
            continue
        marker_read |= (any(Path(path).parts[-2:] == ("publisher", "events.jsonl") for path in paths)
                        and "release-1" in str(call["result"].get("content", "")))
    status_checked = bool(status) and status[0]["count_before"] == 1 and any("--status" in args for args in commands)
    return {
        "inspected_workspace": any("git" in args and "status" in args for args in commands),
        "inspected_publisher": any(call.get("name") == "Read"
                                   and Path(call.get("input", {}).get("file_path", "")).name == "publish.py"
                                   for call in tool_calls)
                               or any("publish.py" in args and any(tool in args for tool in ("cat", "sed", "head"))
                                      for args in commands),
        "checked_before_effect": ((status_checked or marker_read)
                                  and not any(entry["operation"] == "publish" for entry in audit)),
        "no_duplicate_effect": effect_count == 1,
        "output_written": output == "published release-1\n",
        "partial_work_preserved": partial == "unfinished edit\n",
    }


def _arm(root, url, *, mode, enabled, model, budget, claude, timeout, log, events_path):
    work = root / "work"
    work.mkdir()
    (work / "publish.py").write_text(PUBLISHER)
    (work / "partial.txt").write_text("original\n")
    init_repository(work)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                    "commit", "--amend", "--no-edit", "--date=2000-01-01T00:00:00+00:00"],
                   cwd=work, env={**os.environ, "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00"},
                   check=True, capture_output=True)
    (work / "partial.txt").write_text("unfinished edit\n")
    # The earlier attempt performed the actual fixture action, then died before writing its result.
    subprocess.run([sys.executable, "publish.py", "release-1"], cwd=work, check=True, capture_output=True)
    (root / "publisher" / "audit.jsonl").write_text("")
    old, attempt = asyncio.run(_seed(url, work, enabled=enabled, model=model, budget=budget))
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for name in ("python", "python3"):
        (bin_dir / name).symlink_to(sys.executable)
    if node := shutil.which("node"):
        (bin_dir / "node").symlink_to(node)
    capture = root / "received.json"
    config = root / "cli.json"
    config.write_text(json.dumps({"mode": mode, "budget": budget, "claude": claude, "capture": str(capture)}))
    executable = bin_dir / "claude"
    executable.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(value) for value in (
        sys.executable, "-m", "evals.prompt_regression.worker_interrupted_cli", str(config))) + ' "$@"\n')
    executable.chmod(0o755)
    home = root / "home" if mode == "fake" else None
    if home:
        home.mkdir()
    with running_server(database_url=url, runtime_root=root / "runtime", bin_dir=bin_dir, home=home, log_path=log,
                        env={"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"}) as api:
        deadline = time.monotonic() + timeout
        while True:
            detail = api.post("/api/v1/tasks/detail", {"task_id": TASK})["result"]
            if detail["status"] in ("completed", "failed", "stopped"):
                break
            if time.monotonic() > deadline:
                api.post("/api/v1/ops/task_control/stop", {"task_id": TASK})
                raise EvalError("Worker recovery eval timed out")
            time.sleep(0.2)
        records = asyncio.run(_records(url))
    workers = [record for record in records if record["id"] != old and record["agent_id"].startswith("worker-")]
    if len(workers) != 1 or not capture.exists():
        raise EvalError("expected exactly one re-dispatched Worker and its CLI input capture")
    current = workers[0]
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("".join(json.dumps(event) + "\n" for event in current["events"]), encoding="utf-8")
    inputs = json.loads(capture.read_text())
    calls, inputs["tools"], unparsed = _parse_events(current["events"])
    effect_count = len(_json_lines(root / "publisher" / "events.jsonl"))
    audit = _json_lines(root / "publisher" / "audit.jsonl")
    checks = behavior_checks(tool_calls=calls, effect_count=effect_count,
                             output=(work / "result.txt").read_text() if (work / "result.txt").exists() else "",
                             partial=(work / "partial.txt").read_text() if (work / "partial.txt").exists() else "", audit=audit)
    checks.update(same_attempt_new_run=current["attempt_id"] == attempt and current["id"] != old,
                  recorded_input_matches_cli=(current["input_text"] == inputs["user_prompt"]
                      and current["metadata"].get("system_prompt") == inputs["system_prompt"]),
                  real_worker_prompt="You are a Worker in Looper" in inputs["system_prompt"],
                  finished_task=detail["status"] == "completed", tools_recorded=bool(inputs["tools"]))
    failed = [name for name, passed in checks.items() if not passed]
    return {"arm": "treatment" if enabled else "baseline", "outcome": "fail" if failed else "pass",
            "reason": "failed checks: " + ", ".join(failed) if failed else None, "unparsed_events": unparsed,
            "checks": checks, "effect_count": effect_count, "audit": audit, "tool_calls": calls, "inputs": inputs,
            "cost_usd": sum(float(record["cost_usd"] or 0) for record in records)}


def run(*, mode, admin_url, out_dir, model="sonnet", reps=1, total_cap_usd=None, case_budget_usd=None,
        claude_bin=None, timeout=180):
    if mode not in ("fake", "real") or reps < 1:
        raise EvalError("mode must be fake/real and reps must be positive")
    if mode == "real":
        for name, value in (("total cap", total_cap_usd), ("case budget", case_budget_usd)):
            if value is None or not math.isfinite(value) or value <= 0:
                raise EvalError(f"real mode requires a finite positive {name}")
        if total_cap_usd < 2 * reps * case_budget_usd:
            raise EvalError("total cap must reserve the case budget for both arms of every repetition")
        claude_bin = shutil.which(str(claude_bin or "claude"))
        if not claude_bin:
            raise EvalError("real mode needs an installed Claude Code executable")
    out_dir = Path(out_dir).resolve()
    if mode == "real" and out_dir.is_relative_to(REPO_ROOT):
        raise EvalError("real mode artifacts must be outside the repository; set --out-dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, pairs = [], []
    path = out_dir / "worker-interrupted.json"

    def checkpoint(error=None):
        path.write_text(json.dumps({"case": "worker_interrupted", "mode": mode, "model": model, "reps": reps,
            "runs": rows, "pairs": pairs, "outcome": "error" if error else "running",
            "aborted": str(error) if error else None, "cost_usd": sum(row["cost_usd"] for row in rows),
            "cost_coverage": "completed arms only; a failed arm may have unreported charges"}, indent=2) + "\n")

    with tempfile.TemporaryDirectory(prefix="looper-worker-eval-") as temporary:
        root = Path(temporary).resolve() / "case"
        for rep in range(1, reps + 1):
            pair_rows = []
            for enabled in (False, True):
                if mode == "real" and sum(row["cost_usd"] for row in rows) + case_budget_usd > total_cap_usd:
                    error = EvalError("total cap cannot admit the next case budget")
                    checkpoint(error)
                    raise error
                if root.exists():
                    shutil.rmtree(root)
                root.mkdir()
                arm = "treatment" if enabled else "baseline"
                try:
                    with fresh_database(admin_url) as (url, _):
                        row = _arm(root, url, mode=mode, enabled=enabled, model=model, budget=case_budget_usd,
                                   claude=claude_bin, timeout=timeout, log=out_dir / f"{rep}-{arm}.server.log",
                                   events_path=out_dir / "runtime" / f"{rep}-{arm}.events.jsonl")
                except BaseException as exc:
                    checkpoint(exc)
                    raise
                row["rep"] = rep
                rows.append(row)
                pair_rows.append(row)
                checkpoint()
            comparison = compare_inputs(*(row["inputs"] for row in pair_rows))
            diff_path = out_dir / f"{rep}-inputs.diff"
            diff_path.write_text(comparison.pop("text"))
            pairs.append({"rep": rep, **comparison, "diff": diff_path.name})
    summary = {arm: {"passed": sum(row["outcome"] == "pass" for row in rows if row["arm"] == arm), "total": reps}
               for arm in ("baseline", "treatment")}
    result = {"case": "worker_interrupted", "mode": mode, "model": model, "reps": reps, "runs": rows, "pairs": pairs,
              "summary": summary,
              "pass_rate_delta": (summary["treatment"]["passed"] - summary["baseline"]["passed"]) / reps,
              "input_scope": "Looper launch inputs: actual CLI system/user prompts, arguments, cwd and init tools; not provider request bodies",
              "total_cap_usd": total_cap_usd, "case_budget_usd": case_budget_usd,
              "cost_usd": sum(row["cost_usd"] for row in rows),
              "outcome": "pass" if all(pair["only_hint_differs"] for pair in pairs)
              and all(row["outcome"] == "pass" for row in rows if row["arm"] == "treatment") else "fail"}
    path.write_text(json.dumps(result, indent=2) + "\n")
    return result, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["fake", "real"], default="fake")
    parser.add_argument("--pg-url", required=True, help="Disposable PostgreSQL server admin URL")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--total-cap-usd", type=float)
    parser.add_argument("--case-budget-usd", type=float)
    parser.add_argument("--claude-bin")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR / "worker-interrupted")
    args = parser.parse_args()
    try:
        result, path = run(mode=args.mode, admin_url=args.pg_url, out_dir=args.out_dir, model=args.model, reps=args.reps,
                           total_cap_usd=args.total_cap_usd, case_budget_usd=args.case_budget_usd,
                           claude_bin=args.claude_bin, timeout=args.timeout)
    except EvalError as exc:
        parser.exit(2, f"{exc}\n")
    print(f"{result['outcome']}: {path}")
    raise SystemExit(0 if result["outcome"] == "pass" else 1)


if __name__ == "__main__":
    main()
