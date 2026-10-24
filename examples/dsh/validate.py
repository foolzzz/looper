"""有界的 dsh 真实验收入口；preflight 离线，其他命令必须显式 --real。

运行：uv run --extra test python -m examples.dsh.validate preflight
  --disposable-pg-url postgresql://eval:password@127.0.0.1:55432/postgres

--disposable-pg-url 声明整个 PG 实例可丢弃。eval 自建/删除新数据库，
不接受现有业务数据库。preflight 不连接 PG，也不读取或验证 API key。
真实运行捕获交互 zsh 的 key，仅通过 eval 的 api_key_env 注册 provider。
eval 产物固定在 ignored evals/runs/dsh/；smoke 的临时根随服务清理，
stdout 仅有脱敏汇总。smoke 严格检查 room consult，费用未知导致的拒绝不算通过。

每次启动 jobs=1；task-basic、task-review、manager 各两 case、各 reps=2。
每 run $0.50 软上限、总观测 $4、任务/对话
300 秒、外层 20 分钟（另有清理宽限）；不是硬账单上限。辅助费用 coverage
未知。jobs=1 不执行 case budget，本入口不传它，也不承诺每 case $2。
可选 baseline 必须是 real eval JSON，显式提供 validation 中的
effective_system_prompt_mode=append 证据，且包含相同 case；否则记 gap。
不自动寻找基线，不启动其他模型。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import looper
from looper.agents import proc_tree
from looper.agents.runtimes.dsh import SUPPORTED_VERSION

# `python examples/dsh/validate.py` puts this directory on sys.path, not the
# checkout. evals is a source package and is not installed with the wheel.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.prompt_regression import compare  # noqa: E402

SPEC = Path(__file__).with_name("agent-spec.json")
ARTIFACTS = ROOT / "evals" / "runs" / "dsh"
CASES = {"task-basic": "single_step,dependent_steps", "task-review": "reviewed_step,planner_plan_file",
         "manager": "status,confirm_create_start"}
OUTER_TIMEOUT = 20 * 60
ROLES = ("manager", "planner", "worker", "reviewer")
KEY_CAPTURE_TIMEOUT = 30  # an interactive zsh can take ~10 s to start on a loaded machine
STAGE_TIMEOUT = 300
RUN_SOFT_CAP_USD = .50
TOTAL_OBSERVED_CAP_USD = 4


class ValidationError(RuntimeError):
    """只包含公开错误类别，不保留 shell 输出、URL 或原异常。"""


@dataclass
class ProcessResult:
    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    timed_out: bool = False


async def run_process(argv: list[str], *, timeout: float, env: dict[str, str],
                      capture: bool = True, cwd: Path = ROOT) -> ProcessResult:
    """只清理本次创建的进程树；正常退出后也回收遗留子进程。"""
    target = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=target, stderr=target, start_new_session=True)
    known: proc_tree.Tree = {}

    async def track() -> None:
        while True:
            known.update(proc_tree.tree_of(process))
            await asyncio.sleep(0.2)

    async def drain(stream: asyncio.StreamReader | None) -> bytes:
        # Shell startup output is untrusted. Bound retained bytes and never relay it.
        retained = bytearray()
        if stream is not None:
            while chunk := await stream.read(8192):
                retained.extend(chunk[:max(0, 65536 - len(retained))])
        return bytes(retained)

    async def collect() -> tuple[bytes, bytes]:
        stdout, stderr = await asyncio.gather(drain(process.stdout), drain(process.stderr))
        await process.wait()
        return stdout, stderr

    watcher = asyncio.create_task(track())
    output = asyncio.create_task(collect())
    timed_out = False

    def signal_group(sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)

    try:
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(output), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            # Give eval's finally blocks a chance to cancel tasks and drop its new DB.
            known.update(proc_tree.tree_of(process))
            signal_group(signal.SIGINT)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(output), 5)
            await proc_tree.end(process, label="dsh validation", known=known)
            signal_group(signal.SIGKILL)
            stdout, stderr = await asyncio.wait_for(asyncio.shield(output), 1)
        return ProcessResult(process.returncode or 0, stdout, stderr, timed_out)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        signal_group(signal.SIGTERM)
        await proc_tree.end(process, label="dsh validation", known=known)
        signal_group(signal.SIGKILL)
        if not output.done():
            output.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await output


def probe_dsh() -> None:
    executable = shutil.which("dsh")
    if executable is None:
        raise ValidationError(f"需要安装指定 dsh 版本 {SUPPORTED_VERSION}")
    # Probe in a credential-free Python process: even inherited() cannot read
    # the operator's ambient provider variables there. The adapter isolates dsh's home.
    script = ("import json,sys; from looper.agents.runtimes.dsh import DshRuntime; "
              "p=DshRuntime().probe(sys.argv[1]); "
              "print(json.dumps({'version':p.version,'ready':bool(p.capabilities)}))")
    try:
        with tempfile.TemporaryDirectory(prefix="looper-dsh-preflight-") as home:
            result = asyncio.run(run_process(
                [sys.executable, "-c", script, executable], timeout=30,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "PYTHONPATH": str(ROOT)}))
        probe = json.loads(result.stdout)
        valid = (not result.returncode and not result.timed_out and probe.get("ready") is True and
                 probe.get("version") == SUPPORTED_VERSION)
    except Exception:
        valid = False
    if not valid:
        raise ValidationError(f"需要可用的指定 dsh 版本 {SUPPORTED_VERSION}")
    if not proc_tree._snapshot():
        raise ValidationError("进程树观察不可用，不能安全运行真实验收")


def preflight(pg_url: str | None) -> dict[str, Any]:
    if not pg_url:
        raise ValidationError("缺少 --disposable-pg-url；必须显式指定可丢弃 PG 实例")
    try:
        parts = urlsplit(pg_url)
        valid = (parts.scheme in ("postgres", "postgresql") and
                 parts.hostname in ("localhost", "127.0.0.1", "::1") and
                 parts.port != 8767 and parts.path == "/postgres" and not parts.query and not parts.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValidationError("PG URL 必须指向本机可丢弃实例的 postgres 管理库，禁止业务库和 8767")
    if not Path(sys.executable).is_file() or Path(looper.__file__ or "").resolve() != ROOT / "looper/__init__.py":
        raise ValidationError("interpreter/checkout 不匹配；请从本 worktree 启动")
    probe_dsh()
    return {"status": "pass", "interpreter_matches": True, "checkout_matches": True,
            "dsh_version": SUPPORTED_VERSION, "credentials": "untested", "database_connection": "untested",
            "real_validation": "untested", "auxiliary_cost_coverage": "unknown", "hard_billing_cap": False}


def read_key() -> str:
    # Never check=True, log CompletedProcess, or expose the exception's captured output.
    try:
        result = asyncio.run(run_process(["zsh", "-ic", 'printf %s "$DEEPSEEK_API_KEY"'], timeout=KEY_CAPTURE_TIMEOUT,
                                         env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}))
        key = result.stdout.decode("utf-8").strip()
        if result.returncode or result.timed_out or not key or len(key) > 512 or any(c.isspace() for c in key):
            raise ValidationError("API key 捕获失败或为空")
        return key
    except Exception:
        raise ValidationError("API key 捕获失败或为空") from None


def eval_argv(command: str, pg_url: str, out_dir: Path) -> list[str]:
    manager = command == "manager"
    return [sys.executable, "-m", "evals.prompt_regression", "dialogs" if manager else "run",
            "--mode", "real", "--register-agent", str(SPEC), "--pg-url", pg_url,
            "--dialogs" if manager else "--tasks", CASES[command], "--jobs", "1", "--reps",
            "2",
            "--max-budget-usd", f"{RUN_SOFT_CAP_USD:.2f}", "--total-cap-usd", str(TOTAL_OBSERVED_CAP_USD),
            "--timeout" if manager else "--task-timeout", str(STAGE_TIMEOUT), "--out-dir", str(out_dir)]


def baseline_status(base: dict[str, Any] | None, cases: tuple[str, ...], *, dialogs: bool) -> dict[str, Any]:
    if not isinstance(base, dict):
        return {"comparable": False, "gap": "未提供可比基线；未启动其他模型"}
    rows = base.get("dialogs" if dialogs else "tasks")
    validation = base.get("validation")
    comparable = (base.get("mode") == "real" and
                  isinstance(validation, dict) and validation.get("effective_system_prompt_mode") == "append" and
                  isinstance(rows, list) and
                  all(any(isinstance(row, dict) and row.get("id") == case and
                          compare.outcome(row) in ("pass", "fail") for row in rows)
                      for case in cases))
    return {"comparable": comparable, "gap": None if comparable else "基线缺同 case 的 real 结果或有效 append 证据",
            "mode": base.get("mode"),
            "effective_system_prompt_mode": validation.get("effective_system_prompt_mode") if isinstance(validation, dict) else None,
            "samples": {case: sum(isinstance(row, dict) and row.get("id") == case for row in rows)
                        if isinstance(rows, list) else 0 for case in cases}}


def summarize(run: dict[str, Any], cases: tuple[str, ...], *, reps: int = 1) -> dict[str, Any]:
    outcomes = dict.fromkeys(("pass", "fail", "error", "skipped", "untested"), 0)
    rows = compare.rows(run)
    per_case = {}
    sample_gaps = []
    for case in cases:
        selected = [row for row in rows if row.get("id") == case]
        counts = dict.fromkeys(outcomes, 0)
        counts["untested"] = max(0, reps - len(selected))
        if len(selected) != reps or (reps > 1 and sorted(row.get("rep", 0) for row in selected) != list(range(1, reps + 1))):
            sample_gaps.append(case)
        for row in selected:
            outcome = "error" if row.get("timed_out") or row.get("stopped_by_cap") else compare.outcome(row)
            counts[outcome if outcome in counts else "error"] += 1
        for outcome, count in counts.items():
            outcomes[outcome] += count
        per_case[case] = {"outcomes": counts, "samples": selected}
    cost = run.get("total_cost_usd")
    if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        cost = None
    evidence = [record for row in rows for record in row.get("runtime_evidence") or []]
    if "dialogs" in run:
        observed = [record.get("observed_cost_usd") for record in evidence
                    if type(record.get("observed_cost_usd")) in (int, float) and
                    math.isfinite(record["observed_cost_usd"]) and record["observed_cost_usd"] >= 0]
        cost = sum(observed) if observed else None
    return {"outcomes": outcomes, "cases": per_case, "sample_gaps": sample_gaps,
            "root_usage_unknown_fraction": (sum(record.get("root_usage_complete") is not True
                                                 for record in evidence) / len(evidence)) if evidence else None,
            "observed_cost_usd": cost, "aborted": bool(run.get("aborted")),
            "per_run_soft_cap_usd": RUN_SOFT_CAP_USD, "total_observed_cap_usd": TOTAL_OBSERVED_CAP_USD,
            "auxiliary_cost_coverage": "unknown", "hard_billing_cap": False,
            "effective_system_prompt_mode": "append", "case_budget_enforced": False}


def isolated_env(home: Path, temporary: Path, key: str) -> dict[str, str]:
    # Shared by eval and smoke: no ambient provider, channel or operator config.
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
            "PYTHONPATH": str(ROOT), "LOOPER_EVAL_API_KEY": key, "TMPDIR": str(temporary),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def basic_evidence_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps = []
    for row in rows:
        reasons = []
        evidence = row.get("runtime_evidence") or []
        if not {"planner", "worker"}.issubset({r.get("role") for r in evidence}) or any(
            r.get("runtime") != "dsh" or r.get("executable") != "dsh" or
            r.get("system_prompt_mode") != "append" or r.get("session_present") is not True for r in evidence
        ):
            reasons.append("缺少每角色实际 dsh/append/session 证据")
        if (row.get("contract_violations") or {}).get("total") != 0:
            reasons.append("契约违规非零或未知")
        if row.get("planner_modified_files") != 0:
            reasons.append("Planner 写文件非零或未知")
        if row.get("id") == "dependent_steps":
            order = row.get("dependency_order") or {}
            if not (order.get("edges", 0) > 0 and order.get("verified") == order.get("edges") and
                    order.get("violations") == 0 and order.get("unknown") == 0):
                reasons.append("依赖顺序违规或缺少 API 运行时间证据")
        if reasons:
            gaps.append({"id": row.get("id"), "rep": row.get("rep"), "reasons": reasons})
    return gaps


def review_evidence_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps = basic_evidence_gaps(rows)
    for row in rows:
        workers = [run for run in row.get("runtime_evidence") or [] if run.get("role") == "worker"]
        if not any(run.get("status") == "completed" and run.get("report_valid") is True for run in workers):
            gaps.append({"id": row.get("id"), "rep": row.get("rep"),
                         "reasons": ["缺少 Worker 实际完成且契约可解析的报告"]})
        if row.get("id") != "reviewed_step":
            continue
        reviewers = [run for run in row.get("runtime_evidence") or [] if run.get("role") == "reviewer"]
        if not any(run.get("status") == "completed" and run.get("report_valid") is True and
                   run.get("verdict_passed") is True for run in reviewers):
            gaps.append({"id": row.get("id"), "rep": row.get("rep"),
                         "reasons": ["缺少 Reviewer 实际完成且契约可解析的通过报告"]})
    return gaps


def manager_evidence_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps = []
    for row in rows:
        reasons = []
        evidence = row.get("runtime_evidence") or []
        managers = [run for run in evidence if run.get("role") == "manager"]
        expected = 1 if row.get("id") == "status" else 3
        # Starting the task can leave its Planner awaiting its first result/Session.
        if len(managers) != expected or any(
            run.get("runtime") != "dsh" or run.get("executable") != "dsh" or
            run.get("system_prompt_mode") != "append" for run in evidence
        ) or any(run.get("status") != "completed" or not run.get("session_id") for run in managers):
            reasons.append("缺少每轮实际 dsh/append/Session 成功证据")
        if managers and len({run.get("session_id") for run in managers}) != 1:
            reasons.append("Manager 对话未续接同一 Session")
        turns = row.get("turns") or []
        if row.get("id") == "status":
            if not turns or not any(call.endswith(":ok") for call in turns[0].get("calls", [])):
                reasons.append("status 缺少成功 MCP 读回")
        elif len(turns) != 3 or "looper_task_create:ok" not in turns[1].get("calls", []) or \
                "looper_task_control:ok" not in turns[2].get("calls", []):
            reasons.append("确认建任务或开始缺少成功工具操作证据")
        if reasons:
            gaps.append({"id": row.get("id"), "rep": row.get("rep"), "reasons": reasons})
    return gaps


def execute(command: str, pg_url: str, key: str, baseline: Path | None) -> tuple[dict[str, Any], int]:
    # Fixed ignored destination: callers cannot redirect real artifacts into tracked paths.
    ARTIFACTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_dir = Path(tempfile.mkdtemp(prefix=f"{command}-", dir=ARTIFACTS)).resolve()
    home = out_dir / "home"
    home.mkdir(mode=0o700)
    temporary = out_dir / "tmp"
    temporary.mkdir(mode=0o700)
    # Do not inherit operator runtime/provider/ops configuration or credentials.
    env = isolated_env(home, temporary, key)
    cases = tuple(CASES[command].split(","))
    reps = 2
    try:
        base = json.loads(baseline.read_text()) if baseline else None
    except (OSError, ValueError):
        base = None
    baseline_check = baseline_status(base, cases, dialogs=command == "manager")
    result = asyncio.run(run_process(eval_argv(command, pg_url, out_dir), timeout=OUTER_TIMEOUT,
                                     env=env, capture=False))
    paths = list(out_dir.glob("*-real.json"))
    run = json.loads(paths[0].read_text()) if len(paths) == 1 else {}
    summary = summarize(run, cases, reps=reps)
    summary["baseline"] = baseline_check
    evidence_checks = {"task-basic": basic_evidence_gaps, "task-review": review_evidence_gaps,
                       "manager": manager_evidence_gaps}
    summary["evidence_gaps"] = evidence_checks[command](compare.rows(run)) if command in evidence_checks else []
    if command in evidence_checks:
        summary["effective_system_prompt_mode"] = None if summary["evidence_gaps"] or not compare.rows(run) else "append"
    if summary["baseline"]["comparable"]:
        # The comparison stays private, including the input baseline's provenance.
        (out_dir / "comparison.md").write_text(compare.markdown(run, base), encoding="utf-8")
    summary["outer_timeout"] = result.timed_out
    summary["eval_exit_code"] = result.returncode
    passed = (result.returncode == 0 and not result.timed_out and not summary["aborted"] and
              not summary["sample_gaps"] and not summary["evidence_gaps"] and summary["outcomes"] == {
                  "pass": len(cases) * reps, "fail": 0, "error": 0, "skipped": 0, "untested": 0})
    summary["status"] = "pass" if passed else "error" if result.timed_out or not run else "fail"
    summary["real_validation"] = summary["status"]
    (out_dir / "validation-summary.json").write_text(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary, 0 if passed else (2 if result.timed_out or result.returncode == 2 or not run else 1)


def disposable_root(directory: str | Path) -> Path:
    """Physical spelling of a disposable root.

    macOS tempfile keeps the ``/var`` alias of ``/private/var``. DSH snapshot
    parents and trees reject any symlink ancestor, and a child ``getcwd``
    reports the physical spelling. One spelling keeps the guard intact.
    """
    root = Path(directory).resolve()
    if not root.is_dir() or root.is_symlink() or ".." in root.parts:
        raise ValidationError("smoke 临时根不可用")
    return root


def expose_selected_dsh(bin_dir: Path, executable: str) -> Path:
    """Expose the selected dsh as a symlink so its realpath is the installation.

    A shell ``exec`` wrapper leaves ``realpath`` on the wrapper. Bootstrap then
    cannot load ``@deepseek-ai/*``, the headless runner never mounts, and the
    turn ends without an opening session.
    """
    launcher = bin_dir / "dsh"
    launcher.symlink_to(executable)
    return launcher


def _require(condition: Any) -> None:
    if not condition:
        raise ValidationError("smoke 契约未满足")


def _wait(read, predicate, *, check_budget, timeout: float = STAGE_TIMEOUT):
    deadline = time.monotonic() + timeout
    while True:
        check_budget()
        value = read()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("smoke 阶段超时")
        time.sleep(.1)


def _smoke_database(database_url: str) -> dict[str, Any]:
    import asyncpg

    async def read():
        conn = await asyncpg.connect(database_url, timeout=10, command_timeout=10)
        try:
            records = await conn.fetch("SELECT id, agent_id, task_id, session_id, status, model, input_tokens, "
                "output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd FROM llm_records ORDER BY id")
            sessions = await conn.fetch("SELECT agent_id, session_id, runtime_type, provider_id, profile_id "
                                        "FROM agent_sessions WHERE session_id IS NOT NULL")
            consults = await conn.fetch("SELECT task_id, state, source_record, consult_run FROM room_consults")
            return {"records": [dict(r) for r in records], "sessions": [dict(r) for r in sessions],
                    "consults": [dict(r) for r in consults]}
        finally:
            await conn.close()
    return asyncio.run(read())


# Only the installed public persistence reader decodes durable Sessions. It owns
# generation selection, validation and compression; no filename/codec assumptions.
# The standard token-meter fold excludes compaction/summary, which is added here.
SESSION_READER = r"""
import {createRequire} from 'node:module';
import {realpathSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const require = createRequire(realpathSync(process.argv[1]));
const load = name => import(pathToFileURL(require.resolve('@deepseek-ai/' + name)));
const {Context} = await load('cordis');
const persistence = await load('dsh-session-persistence-jsonl');
const {TokenMeter} = await load('dsh-token-meter');
const {SessionProjectionRegistry} = await load('dsh-session-projection');
const {Session} = await load('dsh-session');
const ctx = new Context();
try {
  new SessionProjectionRegistry(ctx);
  new TokenMeter(ctx);
  await persistence.apply(ctx, {root: process.argv[2]});
  const sessions = [];
  for (const {header} of await ctx.sessionPersistence.list()) {
    const handle = await ctx.sessionPersistence.open(header.id, 'read');
    try {
      const {events, eventState} = await handle.read();
      const owned = events.slice(handle.inheritedEventCount);
      let tokens = null;
      const summaries = [];
      for (const event of owned) {
        if (event.type === 'compaction/summary') summaries.push(event.data.usage ?? null);
      }
      // Projection is reference data too. A missing interpreter/unsupported
      // event must not erase the separately observed summary requests.
      try {
        const session = Session.fromRestore(header.id, events, header, handle.inheritedEventCount, eventState);
        tokens = ctx.sessionProjections.stateOf(session, 'tokenUsage').totals;
        if (handle.inheritedEventCount) {
          const prefix = Session.fromRestore(header.id, events.slice(0, handle.inheritedEventCount),
            header, handle.inheritedEventCount, eventState);
          const earlier = ctx.sessionProjections.stateOf(prefix, 'tokenUsage').totals;
          tokens = Object.fromEntries(Object.entries(tokens).map(([key, value]) => [key, value - earlier[key]]));
        }
      } catch {}
      sessions.push({id: header.id, parent: header.parentSession ?? null,
                     tokens, summaries, inherited_events: handle.inheritedEventCount});
    } finally { await handle.close(); }
  }
  console.log(JSON.stringify({sessions}));
} finally { await ctx.dispose(); }
"""


def _session_accounting(records: list[dict[str, Any]], runtime: Path) -> dict[str, Any]:
    from looper.agents.profile_registry import AuthKind, ProviderSpec, VendorType
    from looper.agents.run_usage import Usage
    from looper.agents.runtimes.dsh import DshRuntime
    from looper.agents.runtimes.pricing import price_cost

    owners = set()
    executable = shutil.which("dsh")
    result: dict[str, Any] = {"reader": "untested", "coverage": "unknown", "root_sessions": 0,
                              "child_sessions": 0, "summary_requests": 0, "summary_usage": [],
                              "session_usage": [], "token_meter_includes_summaries": False,
                              "root_observed_cost_usd": None, "child_observed_cost_usd": None,
                              "summary_observed_cost_usd": None}
    # Same eval spec and shared price table as the runtime. No parallel provider
    # registry or second price catalog. These figures remain observations.
    provider = ProviderSpec("validation", "validation", VendorType.CUSTOM, AuthKind.API_KEY)
    token_fields = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")

    def usage(value):
        if not isinstance(value, dict):
            return None
        return {name: value[name] for name in token_fields
                if type(value.get(name)) is int and value[name] >= 0}

    def account(kind, sample):
        if sample is None:
            return
        amount = price_cost(Usage(input_tokens=sample.get("inputTokens"), output_tokens=sample.get("outputTokens"),
            cache_read_tokens=sample.get("cacheReadTokens"), cache_creation_tokens=sample.get("cacheWriteTokens")),
            provider, "deepseek-flash").usage.cost_usd
        if amount is not None:
            field = f"{kind}_observed_cost_usd"
            result[field] = (result[field] or 0) + amount
    for record in records:
        command = record.get("metadata", {}).get("command", [])
        owner = DshRuntime().kept_session_dir(command)
        if owner is not None and owner.resolve().is_relative_to(runtime.resolve()):
            owners.add(owner)
    if not executable or not owners:
        return result
    unreadable = 0
    for owner in sorted(owners):
        try:
            done = asyncio.run(run_process(["node", "--input-type=module", "-e", SESSION_READER,
                executable, str(owner / "dsh" / "sessions")], timeout=30, env=dict(os.environ)))
            if done.returncode or done.timed_out:
                raise ValidationError("Session reader 不可用")
            rows = json.loads(done.stdout)["sessions"]
            _require(rows)  # Empty reads are not evidence of zero spend.
            for row in rows:
                result["child_sessions" if row["parent"] else "root_sessions"] += 1
                result["summary_requests"] += len(row["summaries"])
                samples = [usage(sample) for sample in row["summaries"]]
                result["summary_usage"].extend(samples)
                for sample in samples:
                    account("summary", sample)
                tokens = row["tokens"]
                tokens = usage({**tokens, "inputTokens": tokens.get("uncachedInputTokens")} if tokens else None)
                kind = "child" if row["parent"] else "root"
                account(kind, tokens)
                inherited = row["inherited_events"]
                result["session_usage"].append({"kind": kind, "tokens": tokens,
                    "inherited_events": inherited if type(inherited) is int and inherited >= 0 else None})
        except Exception:
            unreadable += 1
    result["unreadable_owners"] = unreadable
    result["reader"] = ("partial" if result["session_usage"] else "unavailable") if unreadable else "observed"
    # Even a successful read cannot prove search or a deleted consult fork's
    # coverage. Never promote this comparison to a complete bill or hard cap.
    return result


def _smoke_workflow(pg_url: str, root: Path) -> tuple[dict[str, Any], int]:
    from evals.prompt_regression import runner
    from looper.agents.runtimes.dsh import DshRuntime

    summary: dict[str, Any] = {"status": "error", "stage": "checkout", "observed_cost_usd": None,
        "auxiliary_cost_coverage": "unknown", "hard_billing_cap": False,
        "per_run_soft_cap_usd": RUN_SOFT_CAP_USD, "total_observed_cap_usd": TOTAL_OBSERVED_CAP_USD,
        "real_validation": "error"}
    try:
        root = disposable_root(root)
        _require(Path(looper.__file__ or "").resolve() == ROOT / "looper/__init__.py")
        _require(Path(sys.executable).is_file())
        runtime, home, bin_dir = root / "runtime", root / "home", root / "bin"
        for directory in (runtime, home, bin_dir):
            directory.mkdir(mode=0o700)
        # Discovery sees only the selected dsh and harmless system helpers.
        executable = shutil.which("dsh")
        _require(executable)
        expose_selected_dsh(bin_dir, executable)
        if node := shutil.which("node"):
            (bin_dir / "node").symlink_to(node)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        tasks: list[str] = []
        persisted: list[dict[str, Any]] = []
        try:
            summary["stage"] = "database"
            with runner.fresh_database(pg_url) as (database_url, _):
                summary["stage"] = "serve"
                with runner.running_server(database_url=database_url, runtime_root=runtime, home=home,
                        bin_dir=bin_dir, log_path=root / "serve.log", startup_timeout=30) as api:
                    api.timeout = 10
                    request = api.request
                    stage, began = summary["stage"], time.monotonic()

                    def bounded_request(*args, **kwargs):
                        nonlocal stage, began
                        if stage != summary["stage"]:
                            stage, began = summary["stage"], time.monotonic()
                        remaining = STAGE_TIMEOUT - (time.monotonic() - began)
                        if remaining <= 0:
                            raise TimeoutError("smoke 阶段超时")
                        api.timeout = min(10, remaining)
                        return request(*args, **kwargs)

                    api.request = bounded_request
                    _require(not api.base_url.endswith(":8767"))
                    channels = api.get("/api/channel-configs")["configs"]
                    _require(channels and all(not c["enabled"] for c in channels))
                    summary["stage"] = "profile"
                    profile = runner.register_agent(api, json.loads(SPEC.read_text()))
                    for role in ROLES:
                        api.put(f"/api/agent-settings/roles/{role}", {
                            "profile_id": profile["profile_id"], "run_budget_usd": RUN_SOFT_CAP_USD})
                    settings = api.get("/api/agent-settings")
                    _require(all(settings["roles"][role]["values"]["profile_id"] == profile["profile_id"]
                                 for role in ROLES))

                    observed = {}

                    def check_budget():
                        # Result events carry observed spend even when full cost is unknown.
                        for record in api.get("/api/llm-records", limit=200)["records"]:
                            if record["id"] in observed:
                                continue
                            events = runner._all_events(api, record["id"])
                            results = [e["event"] for e in events if e["event"].get("type") == "result"]
                            if results:
                                amount = results[-1].get("observed_cost_usd")
                                if isinstance(amount, (int, float)) and math.isfinite(amount) and amount >= 0:
                                    observed[record["id"]] = amount
                        if observed:
                            summary["observed_cost_usd"] = sum(observed.values())
                            _require(max(observed.values()) <= RUN_SOFT_CAP_USD and
                                     sum(observed.values()) <= TOTAL_OBSERVED_CAP_USD)

                    summary["stage"] = "manager"
                    for turn in (1, 2):
                        message = f"smoke-{turn}"
                        api.post("/api/chat/send", {"text": "Read the platform status using looper_overview.",
                            "session_id": "smoke", "message_id": message})
                        rows = _wait(lambda: api.get("/api/chat/messages", session_id="smoke")["messages"],
                            lambda rows: any(r.get("message_id") == message + ":reply" for r in rows),
                            check_budget=check_budget)
                        _require(any(r.get("message_id") == message + ":reply" and r.get("content") for r in rows))
                    summary["stage"] = "tasks"
                    for number in (1, 2):
                        work = root / f"project-{number}"
                        work.mkdir()
                        project = api.post("/api/projects", {"name": f"smoke-{number}", "work_dir": str(work),
                            "task_workspaces": "in_place", "operator_notes": "off", "user_gates": "off"})
                        task = api.post("/api/tasks/create", {"project_id": project["project_id"],
                            "requirement": "Write delivery.txt containing exactly 'delivered by disposable DSH' "
                            "and a newline. Have an independent Reviewer read it. Before accepting, ask the "
                            "finished Worker's seat a question via looper_room_post, wait for its session "
                            "consult answer, then finish."})["task_id"]
                        tasks.append(task)
                        api.post(f"/api/tasks/{task}/start", {})
                        detail = _wait(lambda: api.get(f"/api/tasks/{task}"),
                            lambda r: r["status"] in ("completed", "failed", "stopped", "cancelled"),
                            check_budget=check_budget)
                        if detail["status"] != "completed":
                            activity = api.post("/api/v1/tasks/activity", {"task_id": task, "limit": 40})["result"].get("activity") or []
                            records = api.get("/api/llm-records", limit=20).get("records") or []
                            ARTIFACTS.mkdir(parents=True, exist_ok=True, mode=0o700)
                            (ARTIFACTS / "task-diag.json").write_text(json.dumps({
                                "status": detail.get("status"),
                                "activity": [str(row.get("summary") or "")[:300] for row in activity[-10:]],
                                "records": [{"agent_id": row.get("agent_id"), "status": row.get("status"),
                                             "model": row.get("model")} for row in records[-10:]],
                            }, ensure_ascii=False), encoding="utf-8")
                        _require(detail["status"] == "completed")
                        _require((work / "delivery.txt").read_text() == "delivered by disposable DSH\n")
                        summary["stage"] = "room_consult"
                        room = api.post("/api/v1/tasks/room_view", {"task_id": task})["result"]
                        _require(room["consults"]["answered"] >= 1)
                        _require(all(c["state"] == "answered" for c in room["consults"]["list"]))
                        summary["stage"] = "tasks"
                    summary["stage"] = "api_readback"
                    homes = {}
                    for row in api.get("/api/llm-records", limit=200)["records"]:
                        record = api.get(f"/api/llm-records/{row['id']}")["record"]
                        _require(record["status"] == "completed" and record["session_id"])
                        _require(record["model"] == "deepseek-flash")
                        metadata = record["metadata"]
                        _require(metadata["profile"]["runtime"]["type"] == "dsh")
                        _require(metadata["profile"]["id"] == profile["profile_id"])
                        _require(metadata["profile"]["provider"]["id"] == profile["provider_id"])
                        _require(metadata["system_prompt_mode"] == "append")
                        _require(all(type(record[key]) is int and record[key] >= 0
                                     for key in ("input_tokens", "output_tokens")))
                        events = runner._all_events(api, row["id"])
                        results = [e["event"] for e in events if e["event"].get("type") == "result"]
                        _require(results and results[-1].get("is_error") is False)
                        owner = DshRuntime().kept_session_dir(metadata.get("command", []))
                        _require(owner is not None and owner.resolve().is_relative_to(runtime.resolve()))
                        _require(homes.setdefault(record["agent_id"], owner) == owner)
                        persisted.append(record)
                    for agent in ("manager", *(f"planner-{task}" for task in tasks)):
                        rows = [r for r in persisted if r["agent_id"] == agent]
                        _require(len(rows) >= 2 and len({r["session_id"] for r in rows}) == 1)
                    for task in tasks:
                        for role in ("worker", "reviewer", "consult"):
                            _require(any(r["task_id"] == task and r["agent_id"].startswith(role + "-")
                                         for r in persisted))
                    # Consults intentionally adopt the source Session in a new
                    # home. All other owners must have distinct Session ids.
                    owners = {r["agent_id"]: r["session_id"] for r in persisted
                              if not r["agent_id"].startswith("consult-")}
                    _require(len(set(owners.values())) == len(owners))
                    _require(len(set(homes.values())) == len(homes))
                    check_budget()
                    _require(summary["observed_cost_usd"] is not None)
                summary["stage"] = "db_readback"
                database = _smoke_database(database_url)
                _require(len(database["records"]) == len(persisted))
                by_id = {r["id"]: r for r in persisted}
                for row in database["records"]:
                    _require(all(by_id[row["id"]][key] == value for key, value in row.items()))
                for agent in ("manager", *(f"planner-{task}" for task in tasks)):
                    rows = [s for s in database["sessions"] if s["agent_id"] == agent]
                    _require(len(rows) == 1 and rows[0]["runtime_type"] == "dsh")
                    _require(rows[0]["profile_id"] == profile["profile_id"])
                    _require(rows[0]["provider_id"] == profile["provider_id"])
                    _require(rows[0]["session_id"] == next(r["session_id"] for r in persisted if r["agent_id"] == agent))
                _require(all(any(c["task_id"] == task and c["state"] == "answered" for c in database["consults"])
                             for task in tasks))
                for consult in database["consults"]:
                    _require(consult["state"] == "answered")
                    source = by_id[consult["source_record"]]
                    answers = [r for r in persisted if r["agent_id"] == consult["consult_run"]]
                    _require(answers and all(r["session_id"] == source["session_id"] for r in answers))
                accounting = _session_accounting(persisted, runtime)
                accounting["headless_observed_cost_usd"] = summary["observed_cost_usd"]
                amounts = [accounting.get(f"{kind}_observed_cost_usd") for kind in ("root", "child", "summary")]
                known = [amount for amount in amounts if amount is not None]
                accounting["reader_observed_cost_usd"] = sum(known) if known else None
                accounting["observed_cost_delta_usd"] = (sum(known) - summary["observed_cost_usd"]) if known else None
                summary["session_accounting"] = accounting
        finally:
            os.environ["PATH"] = old_path
        summary.update(status="pass", stage="completed", roles=list(ROLES), tasks=2,
                       manager_turns=2, database_readback=True, real_validation="pass")
        return summary, 0
    except Exception as exc:
        summary["timeout"] = isinstance(exc, TimeoutError)
        return summary, 2 if summary["timeout"] else 1


def smoke(pg_url: str, key: str) -> tuple[dict[str, Any], int]:
    # An isolated process keeps serve's inherited environment and all private
    # artifacts disposable. The outer supervisor also reaps owned descendants.
    with tempfile.TemporaryDirectory(prefix="looper-dsh-smoke-") as directory:
        root = disposable_root(directory)
        for name in ("launch-home", "tmp"):
            (root / name).mkdir(mode=0o700)
        script = ("import json,sys; from pathlib import Path; "
                  "from examples.dsh.validate import _smoke_workflow; "
                  "summary,code=_smoke_workflow(sys.argv[1],Path(sys.argv[2])); "
                  "print(json.dumps(summary)); sys.exit(code)")
        result = asyncio.run(run_process([sys.executable, "-c", script, pg_url, str(root)],
            timeout=OUTER_TIMEOUT, env=isolated_env(root / "launch-home", root / "tmp", key), capture=True))
        try:
            summary = json.loads(result.stdout)
            _require(isinstance(summary, dict))
        except Exception:
            summary = {"status": "error", "real_validation": "error", "auxiliary_cost_coverage": "unknown",
                       "hard_billing_cap": False}
        summary["outer_timeout"] = result.timed_out
        if result.returncode or result.timed_out or summary.get("status") != "pass":
            summary.update(status="error", real_validation="error")
            return summary, 2 if result.timed_out or not result.stdout else 1
        return summary, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", *CASES, "smoke"):
        command = sub.add_parser(name)
        command.add_argument("--disposable-pg-url", help="显式声明为可丢弃的本机 PG 实例（仅 postgres 管理库）")
        if name != "preflight":
            command.add_argument("--real", action="store_true", help="明确授权读取 key 并运行付费模型")
        if name not in ("preflight", "smoke"):
            command.add_argument("--baseline", type=Path, help="显式 real eval JSON；需有效 append 证据")
    args = parser.parse_args(argv)
    began = False
    try:
        if args.command != "preflight" and not args.real:
            raise ValidationError("真实验收必须显式提供 --real")
        summary = preflight(args.disposable_pg_url)
        code = 0
        if args.command != "preflight":
            key = read_key()
            began = True
            if args.command == "smoke":
                summary, code = smoke(args.disposable_pg_url, key)
            else:
                summary, code = execute(args.command, args.disposable_pg_url, key, args.baseline)
    except ValidationError as exc:
        summary, code = {"status": "error", "reason": str(exc), "real_validation": "untested"}, 2
    except Exception:
        # Provider/shell/API failures can contain secrets. Never print repr or a traceback.
        summary, code = {"status": "error", "reason": "验收入口执行失败；未输出原异常",
                         "real_validation": "error" if began else "untested"}, 2
    print(json.dumps(summary, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
