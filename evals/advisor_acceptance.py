"""The advisor's go-live acceptance (the advisor RFC, D22 and section 16), on a disposable ``looper serve``.

It starts a server of its own (a new database on the given PostgreSQL server, a temporary runtime root, the given
port), makes every role but the advisor's two run Claude Code on ``--model`` (the advisor and its judge keep
their seeded profiles, ``claude-code/fable-5-2`` and ``claude-code/sonnet-5-5``), and runs these tasks one after
another, each in a project of its own (the advisor on only where a case says so):

1. ``diagnose``: a build that fails on its first attempt and passes only when it is run the way an expert would
   say; the Planner consults the advisor and retries with what it takes. Real advisor and judge.
2. Failure paths, each on a critical step, so the consult is the step's checkpoint (the advisor or the judge is a
   stand-in, ``evals/advisor_acceptance_claude.py``):
   ``timeout`` (the advisor never answers; its role's timeout is lowered for the case), ``blocked`` (the judge finds
   a leak in every request: two returns, then blocked) and ``rate_limited`` (HTTP 429, not run again). Each step
   still starts and passes, and the task shows that its checkpoint consult did not finish.
3. ``switch_off``: a task in a project with the advisor off. Its Planner's system prompt holds the reply contract
   the switch-off unit test pins (byte for byte), and nothing the Planner or its Workers are given names the
   advisor.

It then (optionally) takes headless screenshots of the consult nodes, and reports each case's checks, what each
consult cost (its runs by kind) and the run's total, under ``--total-cap-usd``. Nothing runs against the
operator's server or database: the port must not be 8767 and the database is new.

    uv run --extra test python -m evals.advisor_acceptance \\
        --pg-url postgresql://postgres:<password>@127.0.0.1:54331/postgres \\
        --claude ~/.local/bin/claude --port 8885 --total-cap-usd 10 --shots evals/runs/advisor-shots
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.prompt_regression import runner
from evals.prompt_regression.tasks import ADVISOR_TASKS, TASKS, EvalTask, _build_log

STAND_IN = Path(__file__).resolve().with_name("advisor_acceptance_claude.py")
SHOTS = Path(__file__).resolve().with_name("advisor_acceptance_shots.cjs")
OFF_TEST = runner.REPO_ROOT / "tests" / "test_advisor_off.py"
SCHEMA = "looper-advisor-acceptance/v1"
MARKER = "检查点请教未完成"  # a step whose checkpoint consult did not finish (the task summary, the step's card)
# What only an advisor-on Planner or Worker is given; checked against an advisor-on contract first, so a miss
# means something.
ADVISOR_WORDS = re.compile(r'advice_request|advice_adoption|"advise"|\badvisor\b|"difficulty"')

# Builds an index whose entries must be in byte order: it passes only under the C collation, which its error
# message hints at without naming.
BUILD_SH = """\
#!/bin/sh
# Builds the archive index. Its entries must come out in plain byte order.
if [ "${LC_ALL:-}" != "C" ]; then
  echo "BUILD FAILED: index check E-COLL: entries are not in plain byte order (the archive index needs bytewise" >&2
  echo "ordering, and this environment's collation orders them as a dictionary would)" >&2
  exit 1
fi
echo "BUILD OK"
"""

DIAGNOSE = replace(
    next(task for task in ADVISOR_TASKS if task.id == "advisor_optional_flaky"),
    id="accept_diagnose",
    requirement="The working directory has build.sh, which builds an archive index and has side effects, so only "
                "the Worker of the step may run it. The step runs it once with `sh build.sh` (with the environment "
                "the Planner's instructions give, if any) and saves its standard output in build.log. Do not change "
                "build.sh. If it exits non-zero, do not run it again in the same attempt: report the step as failed "
                "with the error. This task is part of an acceptance run of the advisor: after a failed attempt, "
                "consult the advisor (advise, kind diagnose) before you retry, and retry with what you take from "
                "its answer. The task is done when build.log contains BUILD OK.",
    files={"build.sh": BUILD_SH}, check=_build_log, advisor=True,
    expect={"min_retries": 1, "min_advisor_answered": 1, "min_advisor_adoptions": 1},
)
# Its checkpoint gives no answer in the failure cases: only the checkpoint itself is expected.
CHECKPOINT = replace(next(task for task in ADVISOR_TASKS if task.id == "advisor_checkpoint"),
                     expect={"min_advisor_checkpoints": 1})
SWITCH_OFF = replace(next(task for task in TASKS if task.id == "single_step"), id="accept_switch_off", advisor=False)


@dataclass(frozen=True)
class Case:
    name: str
    task: EvalTask
    modes: dict[str, str] = field(default_factory=dict)  # the stand-in's modes (advisor, judge); unset: real
    advisor_timeout: int | None = None  # the advisor role's timeout for the case (seconds)


CASES = (
    Case("diagnose", DIAGNOSE),
    Case("timeout", replace(CHECKPOINT, id="accept_timeout"), {"advisor": "timeout"}, advisor_timeout=20),
    Case("blocked", replace(CHECKPOINT, id="accept_blocked"), {"judge": "block"}),
    Case("rate_limited", replace(CHECKPOINT, id="accept_rate_limited"), {"advisor": "rate_limited"}),
    Case("switch_off", SWITCH_OFF),
)


# --- Reading a task back ---------------------------------------------------------------------------------------


async def _read(database_url: str, work_dir: Path) -> dict[str, Any]:
    """The task of ``work_dir``'s project: its row, steps, consults with their advice, and runs."""
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        task = dict(await conn.fetchrow(
            "SELECT t.id, t.status, t.result_summary, p.advisor FROM tasks t JOIN projects p ON p.id = t.project_id "
            "WHERE p.work_dir = $1 ORDER BY t.created_at DESC LIMIT 1", str(work_dir)))
        steps = [dict(r) for r in await conn.fetch(
            "SELECT id, step_key, status, difficulty, retries FROM task_steps WHERE task_id = $1 ORDER BY step_index",
            task["id"])]
        spawns = [dict(r) for r in await conn.fetch(
            "SELECT s.id, s.step_id, s.state, s.rounds, s.returns, s.spent_usd, s.brief, s.result, s.created_at, "
            "s.finished_at, a.kind, a.adoption, a.unlocked FROM agent_spawns s "
            "LEFT JOIN advisor_advice a ON a.spawn_id = s.id WHERE s.task_id = $1 ORDER BY s.id", task["id"])]
        records = [dict(r) for r in await conn.fetch(
            "SELECT id, agent_id, run_kind, parent_record_id, cost_usd, status, input_text, "
            "metadata->>'system_prompt' AS system_prompt, metadata->>'advisor_consult' AS advisor_consult "
            "FROM llm_records WHERE task_id = $1 ORDER BY id", task["id"])]
    finally:
        await conn.close()
    for spawn in spawns:
        for key in ("brief", "result"):
            spawn[key] = json.loads(spawn[key]) if isinstance(spawn[key], str) else (spawn[key] or {})
    return {"task": task, "steps": steps, "spawns": spawns, "records": records}


async def _spent(database_url: str) -> float:
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        return float(await conn.fetchval("SELECT COALESCE(sum(cost_usd), 0) FROM llm_records"))
    finally:
        await conn.close()


def kind_of(record: dict[str, Any]) -> str:
    agent = str(record["agent_id"])
    return "advice_check" if agent.startswith("advice_check-") else "advisor" if agent.startswith("advisor-") \
        else str(record.get("run_kind") or agent.split("-")[0])


def consult_costs(read: dict[str, Any]) -> list[dict[str, Any]]:
    """Each consult's cost: what it booked, and its runs (``result.runs``) by kind with their recorded costs."""
    records = {r["id"]: r for r in read["records"]}
    rows = []
    for spawn in read["spawns"]:
        runs = [records[i] for i in spawn["result"].get("runs", ()) if i in records]
        by_kind: dict[str, dict[str, float]] = {}
        for run in runs:
            entry = by_kind.setdefault(kind_of(run), {"runs": 0, "cost_usd": 0.0})
            entry["runs"] += 1
            entry["cost_usd"] = round(entry["cost_usd"] + float(run["cost_usd"] or 0.0), 6)
        ended = spawn["finished_at"]
        checks = Counter(f"{c.get('stage')}:{c.get('category')}" for c in spawn["result"].get("checks", ()))
        rows.append({"consult": spawn["id"], "source": spawn["brief"].get("source"), "kind": spawn["kind"],
                     "state": spawn["state"], "failure": spawn["result"].get("failure"), "rounds": spawn["rounds"],
                     "returns": spawn["returns"], "checks": dict(checks),
                     "spent_usd": round(float(spawn["spent_usd"]), 6), "runs": by_kind,
                     "duration_s": round((ended - spawn["created_at"]).total_seconds(), 1) if ended else None})
    return rows


# --- The checks ---------------------------------------------------------------------------------------------


def _check(checks: list[dict[str, Any]], name: str, ok: bool, detail: Any = "") -> None:
    checks.append({"check": name, "ok": bool(ok), **({"detail": detail} if detail != "" else {})})


def _runs(read: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [r for r in read["records"] if kind_of(r) == kind]


def check_diagnose(read: dict[str, Any], detail: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    answered = [s for s in read["spawns"] if s["state"] == "answered"]
    _check(checks, "the task completed and build.log has BUILD OK", row.get("outcome") == "pass",
           row.get("problems") or "")
    _check(checks, "the Planner consulted after the failed attempt and got an answer",
           any(s["brief"].get("source") == "optional" for s in answered), [s["state"] for s in read["spawns"]])
    _check(checks, "its decision said what it took (advice_adoption)", any(s["adoption"] for s in answered))
    retried = [r for r in _runs(read, "worker") if r["advisor_consult"]]
    _check(checks, "the retry's Worker ran with the consult named on its record", bool(retried))
    _check(checks, "the retried attempt passed (unlocked)", any(s["unlocked"] for s in answered))
    spawned = _runs(read, "advisor") + _runs(read, "advice_check")
    _check(checks, "each advisor and judge run hangs under the Planner run that asked",
           bool(spawned) and all(r["parent_record_id"] for r in spawned))
    _check(checks, "the task summary has its advisor line", "## Advisor" in str(read["task"]["result_summary"] or ""))
    _check(checks, "the task detail has the summary line", bool((detail.get("advisor") or {}).get("summary")))
    return checks


def check_failure(case: Case, read: dict[str, Any], detail: dict[str, Any], row: dict[str, Any],
                  activity: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checkpoints = [s for s in read["spawns"] if s["brief"].get("source") == "checkpoint"]
    spawn = checkpoints[0] if checkpoints else None
    _check(checks, "the critical step's checkpoint consulted the advisor", spawn is not None,
           [s["brief"].get("source") for s in read["spawns"]])
    if spawn is None:
        return checks
    advisor_runs = [r for r in _runs(read, "advisor") if r["id"] in spawn["result"].get("runs", ())]
    judge_runs = [r for r in _runs(read, "advice_check") if r["id"] in spawn["result"].get("runs", ())]
    if case.name == "timeout":
        _check(checks, "the consult failed as timeout", (spawn["state"], spawn["result"].get("failure"))
               == ("failed", "timeout"), spawn["result"].get("failure"))
        _check(checks, "the advisor ran once (a timeout is not run again)", len(advisor_runs) == 1, len(advisor_runs))
    elif case.name == "blocked":
        _check(checks, "the request was blocked after 2 returns", (spawn["state"], spawn["returns"]) == ("blocked", 2),
               (spawn["state"], spawn["returns"]))
        last = (spawn["result"].get("checks") or [{}])[-1]
        _check(checks, "the judge blocked it last, and the advisor never ran",
               bool(judge_runs) and last.get("stage") == "judge" and not advisor_runs,
               (len(judge_runs), last.get("stage"), len(advisor_runs)))
    else:
        _check(checks, "the consult failed as rate_limited", (spawn["state"], spawn["result"].get("failure"))
               == ("failed", "rate_limited"), spawn["result"].get("failure"))
        _check(checks, "the advisor ran once (429 is not run again)", len(advisor_runs) == 1, len(advisor_runs))
    step = next((s for s in read["steps"] if s["id"] == spawn["step_id"]), {})
    _check(checks, "the step still started and passed", step.get("status") == "verified", step.get("status"))
    _check(checks, "the task completed", row.get("outcome") == "pass", row.get("problems") or "")
    _check(checks, "its activity says the checkpoint consult did not finish",
           any("its checkpoint consult did not finish" in str(a.get("summary")) for a in activity))
    _check(checks, f"the summary line has {MARKER}", MARKER in str((detail.get("advisor") or {}).get("summary")))
    _check(checks, f"the task summary has {MARKER}", MARKER in str(read["task"]["result_summary"] or ""))
    return checks


def off_contracts() -> dict[str, str]:
    """Every advisor-off reply contract, as ``tests/test_advisor_off.py`` enumerates them (its flags and retries)."""
    from looper.agents.tiers.planner_protocol import reply_contract

    flags = ("roles", "gates", "room", "conditions", "review_panel", "adjudication", "wait_commands")
    return {json.dumps(dict(zip(flags, values)), sort_keys=True): reply_contract(3, **dict(zip(flags, values)))
            for values in itertools.product((False, True), repeat=len(flags))}


def pinned_off_digest() -> str | None:
    match = re.search(r'^CONTRACT_SHA256 = "([0-9a-f]{64})"', OFF_TEST.read_text(encoding="utf-8"), re.M) \
        if OFF_TEST.exists() else None
    return match.group(1) if match else None


def check_switch_off(read: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    from looper.agents.tiers.planner_protocol import reply_contract

    checks: list[dict[str, Any]] = []
    contracts = off_contracts()
    digest = hashlib.sha256(json.dumps(contracts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    _check(checks, "the advisor-off contracts are the ones pinned before the advisor", digest == pinned_off_digest())
    _check(checks, "the advisor words are found in an advisor-on contract (the check can fail)",
           bool(ADVISOR_WORDS.search(reply_contract(3, advisor=True))))
    _check(checks, "the project has the advisor off and no consult", read["task"]["advisor"] in (None, "off")
           and not read["spawns"])
    planner = _runs(read, "planner")
    prompts = [str(r["system_prompt"] or "") for r in planner]
    # The task's own retries setting: the contract is compared with every count it may hold.
    candidates = [reply_contract(n, **json.loads(flags)) for flags in contracts for n in range(0, 21)]
    _check(checks, "the Planner's system prompt holds an advisor-off contract byte for byte",
           bool(prompts) and all(any(c in prompt for c in candidates) for prompt in prompts))
    given = [*prompts, *(str(r["input_text"] or "") for r in planner),
             *(str(r["system_prompt"] or "") + str(r["input_text"] or "") for r in _runs(read, "worker"))]
    found = sorted({m.group(0) for text in given for m in ADVISOR_WORDS.finditer(text)})
    _check(checks, "nothing the Planner or its Workers are given names the advisor", not found, found)
    _check(checks, "the task completed", row.get("outcome") == "pass", row.get("problems") or "")
    return checks


def turned_off(api: runner.Api, task_id: str) -> dict[str, Any]:
    """A project that had consults turns the advisor off: the task's page keeps its consults and their cost (its
    history), and the Manager's view of the task names the advisor nowhere."""
    checks: list[dict[str, Any]] = []
    detail = api.post("/api/v1/tasks/detail", {"task_id": task_id})["result"]
    api.post("/api/v1/project_admin/update", {"project_id": detail["project_id"], "fields": {"advisor": "off"}})
    page = api.post("/api/v1/tasks/detail", {"task_id": task_id})["result"]
    manager = api.post("/api/v1/ops/tasks/get", {"task_id": task_id})["result"]
    _check(checks, "the task's page keeps its consults and their cost", bool((page.get("advisor") or {}).get("consults")))
    keys: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            keys.update(k for k in value if re.search(r"(?i)advis|consult", k))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(manager)
    _check(checks, "the Manager's view of the task has no advisor field", not keys, sorted(keys))
    _check(checks, "nor the advisor's line in the task's result", "## Advisor" not in str(manager.get("result")))
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


# --- Running ----------------------------------------------------------------------------------------------------


def install_claude(bin_dir: Path, real: str, modes: Path, cap_usd: float) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(part) for part in (
        sys.executable, str(STAND_IN), real, str(modes), f"{cap_usd:g}")) + ' -- "$@"\n', encoding="utf-8")
    script.chmod(0o755)


def screenshots(base_url: str, out: Path, task_ids: list[str]) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(["node", str(SHOTS), base_url, str(out), *task_ids], capture_output=True, text=True,
                              timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if done.returncode != 0:
        return {"error": (done.stderr or done.stdout)[-2000:]}
    return json.loads(done.stdout.strip().splitlines()[-1])


def run(args: argparse.Namespace) -> dict[str, Any]:
    real = os.path.expanduser(args.claude)
    if not os.access(real, os.X_OK):
        raise runner.EvalError(f"{args.claude} is not an executable Claude Code")
    root = Path(tempfile.mkdtemp(prefix="looper-accept-")).resolve()
    modes = root / "modes.json"
    install_claude(root / "bin", real, modes, args.max_run_usd)
    (root / "runtime").mkdir(mode=0o700)
    report: dict[str, Any] = {"schema": SCHEMA, "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "git": runner._git(), "model": args.model, "cases": []}
    selected = [case for case in CASES if not args.cases or case.name in args.cases]
    log_path = args.out_dir / f"advisor-acceptance-{report['started'].replace(':', '')}.server.log"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with runner.fresh_database(args.pg_url, keep=args.keep_database) as (database_url, clock_offset), \
            runner.running_server(
        database_url=database_url, runtime_root=root / "runtime", bin_dir=root / "bin", log_path=log_path,
        port=args.port,
    ) as api:
        runner.select_agent(api, agent=None, model=args.model)
        profiles = {p["name"]: p for p in api.post("/api/v1/registry/view", {})["result"]["profiles"]}
        report["profiles"] = {name: name in profiles for name in ("claude-code/fable-5-2", "claude-code/sonnet-5-5")}
        task_ids: dict[str, str] = {}
        for case in selected:
            spent = asyncio.run(_spent(database_url))
            if spent >= args.total_cap_usd:
                report["cases"].append({"case": case.name, "skipped": f"the total cap was reached ({spent:.2f} USD)"})
                continue
            modes.write_text(json.dumps(case.modes), encoding="utf-8")
            before = None
            if case.advisor_timeout is not None:
                before = api.post("/api/v1/roles/get", {"role": "advisor"})["result"]["timeout_seconds"]
                api.post("/api/v1/roles/update", {"role": "advisor", "fields": {"timeout_seconds": case.advisor_timeout}})
            work_dir = root / "work" / case.name
            try:
                row = runner.run_task(api, case.task, work_dir, timeout=args.task_timeout, poll=2.0,
                                      cap_left=args.total_cap_usd - spent, clock_offset=clock_offset)
            except runner.TaskError as exc:
                row = {"outcome": "error", "problems": [str(exc)]}
            finally:
                if before is not None:
                    api.post("/api/v1/roles/update", {"role": "advisor", "fields": {"timeout_seconds": before}})
            read = asyncio.run(_read(database_url, work_dir))
            task_ids[case.name] = read["task"]["id"]
            detail = api.post("/api/v1/tasks/detail", {"task_id": read["task"]["id"]})["result"]
            if case.name == "diagnose":
                checks = check_diagnose(read, detail, row)
            elif case.name == "switch_off":
                checks = check_switch_off(read, row)
            else:
                checks = check_failure(case, read, detail, row, runner._all_activity(api, read["task"]["id"]))
            report["cases"].append({
                "case": case.name, "passed": all(c["ok"] for c in checks), "checks": checks,
                "task_status": read["task"]["status"], "task_cost_usd": row.get("cost_usd"),
                "duration_s": row.get("duration_s"), "consults": consult_costs(read),
                "summary_line": (detail.get("advisor") or {}).get("summary"),
            })
        if "rate_limited" in task_ids:
            report["turned_off"] = turned_off(api, task_ids["rate_limited"])
        if args.shots is not None:
            shown = [task_ids[name] for name in ("diagnose", "blocked", "timeout") if name in task_ids]
            report["screenshots"] = screenshots(api.base_url + "/", args.shots, shown) if shown else {}
        report["total_cost_usd"] = round(asyncio.run(_spent(database_url)), 6)
    report["passed"] = all(case.get("passed") for case in report["cases"] if "skipped" not in case) and \
        report.get("turned_off", {}).get("passed", True)
    consults = [c for case in report["cases"] for c in case.get("consults", ())]
    report["consult_cost_usd"] = {"consults": len(consults),
                                  "total": round(sum(c["spent_usd"] for c in consults), 6),
                                  "per_consult": round(sum(c["spent_usd"] for c in consults) / len(consults), 6)
                                  if consults else None}
    path = args.out_dir / f"advisor-acceptance-{report['started'].replace(':', '')}.json"
    path.write_text(json.dumps(runner.scrub(report, {str(root): "<root>"}), indent=2, ensure_ascii=False,
                               default=str) + "\n", encoding="utf-8")
    report["path"] = str(path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.advisor_acceptance", description=__doc__.split("\n\n")[0])
    parser.add_argument("--pg-url", required=True, help="an admin URL of a disposable PostgreSQL server")
    parser.add_argument("--claude", required=True, help="the real Claude Code executable")
    parser.add_argument("--port", type=int, default=8885)
    parser.add_argument("--model", default="sonnet", help="the model every other role runs (default sonnet)")
    parser.add_argument("--total-cap-usd", type=float, default=10.0)
    parser.add_argument("--max-run-usd", type=float, default=2.0, help="each real run's cap, unless it sets one")
    parser.add_argument("--task-timeout", type=float, default=900.0)
    parser.add_argument("--cases", nargs="*", choices=[case.name for case in CASES])
    parser.add_argument("--shots", type=Path, help="take headless screenshots into this directory (needs node)")
    parser.add_argument("--out-dir", type=Path, default=runner.DEFAULT_OUT_DIR)
    parser.add_argument("--keep-database", action="store_true",
                        help="keep the run's database to look into afterwards (drop it yourself)")
    args = parser.parse_args(argv)
    report = run(args)
    for case in report["cases"]:
        mark = "skipped" if "skipped" in case else "PASS" if case["passed"] else "FAIL"
        print(f"{case['case']}: {mark} {case.get('skipped', '')}")
        for check in case.get("checks", ()):
            if not check["ok"]:
                print(f"  failed: {check['check']} {check.get('detail', '')}")
    if "turned_off" in report:
        print(f"turned off: {'PASS' if report['turned_off']['passed'] else 'FAIL'}")
    print(f"consults: {report['consult_cost_usd']}; total {report['total_cost_usd']:.4f} USD; {report['path']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
