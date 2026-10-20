#!/usr/bin/env bash
# Targeted Claude CLI comparison for pi's runtime notes, context and tool bridge.
# Re-read evals/README.md "Which tier to run" before use. This entry point owns
# tier choices; it does not change the shared runner or release baselines.
# Required: ANTHROPIC_API_KEY, PI_EVAL_TOTAL_CAP_USD, PI_EVAL_CASE_BUDGET_USD,
# LOOPER_EVAL_DATABASE_URL (or LOOPER_TEST_DATABASE_URL), on a disposable local PG.
# Optional prior real pi reports, separately for each provider and phase:
# PI_EVAL_RESPONSES_TASKS_JSON / PI_EVAL_RESPONSES_DIALOGS_JSON
# PI_EVAL_DEEPSEEK_TASKS_JSON / PI_EVAL_DEEPSEEK_DIALOGS_JSON
# Paths must be in this checkout's ignored .local/ or evals/runs/ directories.
# Launch in the background, e.g. bash examples/pi/eval-prompt-comparison.sh \
#   > .local/pi-prompt-comparison.log 2>&1 &
# One shared deadline (default/max 1500 seconds) and spending cap cover all tiers.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"
python3 - "$ROOT" <<'PY'
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit

from looper.agents import proc_tree

root = Path(sys.argv[1])
state = root / ".local/pi-eval-prompt-comparison"
state.mkdir(parents=True, exist_ok=True)
run = Path(tempfile.mkdtemp(prefix="run-", dir=state))
latest = state / "latest"
if latest.is_symlink():
    latest.unlink()
latest.symlink_to(run.name, target_is_directory=True)
ledger = run / "ledger.txt"
started = time.monotonic()
# Match the independently evaluated pi cases. No full task set or release tier.
tasks = "single_step,dependent_steps,reviewed_step,room_interface,room_consult,operator_notes,planner_plan_file,vault_closeout_keeps,vault_closeout_nothing"
planner = "dependent_steps,room_interface,room_consult,operator_notes,planner_plan_file,vault_closeout_keeps,vault_closeout_nothing"
dialogs = "status,confirm_create_start,config_tools,cancel_confirmation"


def note(text):
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


note("定向对照：13 个不同 case；四档共 48 个 case/rep；未执行项均为 unverified。")
note("范围：runtime_notes/pi.py 最终答复契约 → single_step,reviewed_step；context/_prompt_text → operator_notes,vault_closeout_*；schema/工具桥 → dependent_steps,room_*,planner_plan_file；Manager 工具 → status,confirm_create_start,config_tools,cancel_confirmation。")
note("依据：RFC §6；evals/README.md Which tier to run / compare。共享 runner 不写入新档位。")
note("共享模型可见变化审查：unverified，需对照基线 commit 的共享 prompt/contract；pi 专属说明仍需独立真实 profile 评测。")
note("positive control: unverified（正负检测由 tests/test_pi_eval_commands.py 和 test_prompt_eval.py 验证；fake uv 只检查命令，不能证明真实模型通过）。")
note("工具拒绝和 Planner 文件修改：只引用实际 planner_denials/planner_modified_files；缺字段为 unverified，0 不能替代正向检测。")
note("未覆盖：operator_notes_stale、schedule_*、steering、session resume、cancel tree、optional/strict schema 原始请求读回；本卡不刷新发布基线。")
note("Responses / DeepSeek 真实 profile 结果及逐项比较：等待输入；缺少时保留 unverified。")


def fail(message, code=2):
    note("unverified: " + message)
    print("评测未完成：" + message, file=sys.stderr)
    raise SystemExit(code)


def money(name):
    text = os.environ.get(name, "")
    try:
        value = Decimal(text)
        if not value.is_finite() or value <= 0:
            raise ValueError()
    except (ValueError, ArithmeticError):
        fail(name + " must be finite and positive")
    return value


if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
    fail("ANTHROPIC_API_KEY missing; Claude CLI 对照 0/48，真实通过 0、失败 0、未验证 48")
remaining = money("PI_EVAL_TOTAL_CAP_USD")
case_budget = money("PI_EVAL_CASE_BUDGET_USD")
if case_budget > remaining:
    fail("case budget exceeds total cap")
deadline_text = os.environ.get("PI_EVAL_DEADLINE_SECONDS", "1500")
if not deadline_text.isdigit() or not 1 <= int(deadline_text) <= 1500:
    fail("deadline must be a whole number from 1 to 1500")
deadline = int(deadline_text)
pg = os.environ.get("LOOPER_EVAL_DATABASE_URL") or os.environ.get("LOOPER_TEST_DATABASE_URL", "")
try:
    url = urlsplit(pg)
    if (url.scheme not in ("postgres", "postgresql") or url.hostname not in ("localhost", "127.0.0.1")
            or unquote(url.path.lstrip("/")) in ("", "loopx", "looper")):
        raise ValueError()
except ValueError:
    fail("local disposable database URL required")

# Explicit key only: no operator login/config, notes or real channel credentials.
private_home = run / "home"
private_home.mkdir(mode=0o700)
child = {k: v for k, v in os.environ.items() if k in (
    "PATH", "LANG", "TMPDIR", "SYSTEMROOT", "UV_CACHE_DIR", "UV_PYTHON", "VIRTUAL_ENV")}
child.update(HOME=str(private_home), CLAUDE_CONFIG_DIR=str(private_home / "claude"),
             ANTHROPIC_API_KEY=os.environ["ANTHROPIC_API_KEY"])
# Fake uv is a command-boundary test seam, never a real acceptance result.
child.update({k: v for k, v in os.environ.items() if k.startswith("PI_EVAL_FAKE_")})
command = ["uv", "run", "--extra", "test", "python", "-m", "evals.prompt_regression"]


def bounded(argv, log):
    seconds = deadline - (time.monotonic() - started)
    if seconds <= 0:
        fail("deadline: remaining tiers/comparisons 未执行", 124)
    with log.open("w", encoding="utf-8") as out:
        async def run():
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv, cwd=root, env=child, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError:
                fail("runner/compare executable unavailable")
            try:
                return await asyncio.wait_for(process.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                # Capture independent agent groups before TERM orphans them;
                # allow cleanup, then KILL every surviving descendant group.
                try:
                    await proc_tree.end(process, label="eval deadline")
                finally:
                    # Keep the own-group fallback if process listing fails.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                fail("deadline: process tree stopped; remaining tiers/comparisons 未验证", 124)

        return asyncio.run(run())


def read_report(path, phase, ids, *, model=None):
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get(phase, [])
    expected = {(case, rep) for case in ids.split(",") for rep in (1, 2)}
    found = [(row["id"], row.get("rep", 1)) for row in rows]
    if data.get("mode") != "real" or data.get("aborted"):
        raise ValueError("not a completed real report")
    if not expected.issubset(set(found)) or len(found) != len(set(found)):
        raise ValueError("missing or duplicate selected case/reps")
    if model and (data.get("model") != model or data.get("agent") != "claude-code/" + model):
        raise ValueError("unexpected Claude model")
    if not model and not str(data.get("agent", "")).startswith("pi/"):
        raise ValueError("not an independent pi profile report")
    return data, [row for row in rows if (row["id"], row.get("rep", 1)) in expected]


status = 0
for label, phase, ids, model, extra, baseline in (
    ("tasks-haiku", "tasks", tasks, "haiku", [], "real-haiku.json"),
    ("planner-opus", "tasks", planner, "opus", [], "real-opus.json"),
    ("dialogs-haiku", "dialogs", dialogs, "haiku", [], "dialogs-real-haiku.json"),
    ("manager-opus", "dialogs", dialogs, "haiku", ["--manager-model", "opus", "--manager-effort", "xhigh"], "dialogs-real-opus.json"),
):
    if remaining < case_budget:
        fail("remaining budget below case reservation; remaining tiers 未验证")
    output = run / label
    output.mkdir()
    argv = command + ["run" if phase == "tasks" else "dialogs", "--mode", "real", "--model", model,
        "--reps", "2", "--jobs", "4", "--case-budget-usd", str(case_budget),
        "--max-budget-usd", str(case_budget), "--total-cap-usd", str(remaining), "--pg-url", pg,
        "--out-dir", str(output), "--tasks" if phase == "tasks" else "--dialogs", ids,
        "--task-timeout" if phase == "tasks" else "--timeout", "180", *extra]
    code = bounded(argv, output / "runner.log")
    status = max(status, code)
    try:
        paths = list(output.glob("*.json"))
        if len(paths) != 1:
            raise ValueError("missing or ambiguous fresh report")
        report = paths[0]
        data, rows = read_report(report, phase, ids, model=model)
        if phase == "dialogs" and (data.get("manager_model") or model) != ("opus" if extra else model):
            raise ValueError("unexpected Manager tier")
        cost = Decimal(str(data["total_cost_usd"]))
        costs = [Decimal(str(row["cost_usd"])) for row in data[phase]]
        if (not cost.is_finite() or cost < 0 or any(not c.is_finite() or c < 0 for c in costs)
                or abs(cost - sum(costs)) > Decimal("0.000001")):
            raise ValueError("missing/unknown/inconsistent spend")
        remaining -= cost
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        fail(label + ": readback unverified (" + str(exc) + "); later tiers 未执行")
    counts = Counter(row.get("outcome", "unverified") for row in rows)
    if counts['pass'] != len(rows):
        status = max(status, 1)
    note(f"{label}: case/rep {len(rows)}，通过 {counts['pass']}，失败 {counts['fail']}，未验证 {len(rows) - counts['pass'] - counts['fail']}；花费 USD {cost}")
    for row in rows:
        note(f"{label} {row['id']} rep {row.get('rep')}: outcome={row.get('outcome', 'unverified')} "
             + " ".join(f"{key}={row.get(key, 'unverified')}" for key in (
                 "error_class", "planner_denials", "planner_modified_files", "planner_edit_tool_paths", "planner_changed_files")))
    base = root / "evals/prompt_regression/baselines" / baseline
    if bounded(command + ["compare", str(report), str(base)], output / "baseline-compare.txt"):
        fail(label + " baseline compare failed")
    for provider, env_prefix, agent_name in (("Responses", "RESPONSES", "pi/cpa/gpt-6.1-sol"),
                                              ("DeepSeek", "DEEPSEEK", "pi/deepseek/deepseek-flash")):
        name = "PI_EVAL_" + env_prefix + "_" + phase.upper() + "_JSON"
        source = os.environ.get(name)
        if not source:
            note(label + " / " + provider + ": unverified（缺少独立真实 pi profile 报告 " + name + "）")
            continue
        try:
            path = Path(source).resolve()
            if not any(path.is_relative_to(root / p) for p in (".local", "evals/runs")):
                raise ValueError("report outside ignored checkout results")
            pi_data, pi_rows = read_report(path, phase, ids)
            if pi_data.get("agent") != agent_name:
                raise ValueError("wrong provider/profile")
            pi_counts = Counter(row.get("outcome", "unverified") for row in pi_rows)
            note(f"{label} / {provider}: case/rep {len(pi_rows)}，通过 {pi_counts['pass']}，失败 {pi_counts['fail']}，未验证 {len(pi_rows) - pi_counts['pass'] - pi_counts['fail']}")
            for row in pi_rows:
                note(f"{provider} {row['id']} rep {row.get('rep')}: "
                     + " ".join(f"{key}={row.get(key, 'unverified')}" for key in (
                         "planner_denials", "planner_modified_files", "planner_edit_tool_paths", "planner_changed_files")))
            # Compare the very same case/reps; do not fold other pi cases into totals.
            selected = {**pi_data, phase: pi_rows}
            subset = output / (env_prefix.lower() + "-selected.json")
            subset.write_text(json.dumps(selected, indent=2) + "\n", encoding="utf-8")
            if bounded(command + ["compare", str(subset), str(report)], output / (env_prefix.lower() + "-compare.txt")):
                raise ValueError("compare failed")
            note(label + " / " + provider + f": 同一组 {len(pi_rows)} case/rep；已输出 compare 表（非协议/schema 验收）")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            note(label + " / " + provider + ": unverified（" + str(exc) + "）")
    if code not in (0, 1):
        fail(label + " runner aborted; remaining tiers 未验证")
    if remaining <= 0:
        fail("total budget exhausted; remaining tiers 未验证")
note("完成命令与结果读回；未验证项继续保留，fake uv 结果不属于真实评测证据。")
print("对照台账：" + str(ledger.relative_to(root)))
raise SystemExit(status)
PY
