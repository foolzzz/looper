"""有界的 dsh 真实验收入口；preflight 离线，其他命令必须显式 --real。

运行：uv run --extra test python -m examples.dsh.validate preflight
  --disposable-pg-url postgresql://eval:password@127.0.0.1:55432/postgres

--disposable-pg-url 声明整个 PG 实例可丢弃。eval 自建/删除新数据库，
不接受现有业务数据库。preflight 不连接 PG，也不读取或验证 API key。
真实运行捕获交互 zsh 的 key，仅通过 eval 的 api_key_env 注册 provider。
产物固定在 ignored evals/runs/dsh/，stdout 仅有脱敏汇总。

每次启动 jobs=1、reps=1，每 run $0.50 软上限、总观测 $4、任务/对话
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import looper
from evals.prompt_regression import compare
from looper.agents import proc_tree
from looper.agents.runtimes.dsh import SUPPORTED_VERSION


ROOT = Path(__file__).resolve().parents[2]
SPEC = Path(__file__).with_name("agent-spec.json")
ARTIFACTS = ROOT / "evals" / "runs" / "dsh"
CASES = {"task-basic": "single_step", "task-review": "reviewed_step", "manager": "status"}
OUTER_TIMEOUT = 20 * 60


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
        result = asyncio.run(run_process(["zsh", "-ic", 'printf %s "$DEEPSEEK_API_KEY"'], timeout=10,
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
            "--dialogs" if manager else "--tasks", CASES[command], "--jobs", "1", "--reps", "1",
            "--max-budget-usd", "0.50", "--total-cap-usd", "4",
            "--timeout" if manager else "--task-timeout", "300", "--out-dir", str(out_dir)]


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
    return {"comparable": comparable, "gap": None if comparable else "基线缺同 case 的 real 结果或有效 append 证据"}


def summarize(run: dict[str, Any], cases: tuple[str, ...]) -> dict[str, Any]:
    outcomes = dict.fromkeys(("pass", "fail", "error", "skipped", "untested"), 0)
    rows = compare.rows(run)
    for case in cases:
        selected = [row for row in rows if row.get("id") == case]
        if not selected:
            outcomes["untested"] += 1
        for row in selected:
            outcome = compare.outcome(row)
            outcomes[outcome if outcome in outcomes else "error"] += 1
    cost = run.get("total_cost_usd")
    if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        cost = None
    return {"outcomes": outcomes, "observed_cost_usd": cost, "aborted": bool(run.get("aborted")),
            "per_run_soft_cap_usd": 0.50, "total_observed_cap_usd": 4,
            "auxiliary_cost_coverage": "unknown", "hard_billing_cap": False,
            "effective_system_prompt_mode": "append", "case_budget_enforced": False}


def execute(command: str, pg_url: str, key: str, baseline: Path | None) -> tuple[dict[str, Any], int]:
    # Fixed ignored destination: callers cannot redirect real artifacts into tracked paths.
    ARTIFACTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    out_dir = Path(tempfile.mkdtemp(prefix=f"{command}-", dir=ARTIFACTS)).resolve()
    home = out_dir / "home"
    home.mkdir(mode=0o700)
    temporary = out_dir / "tmp"
    temporary.mkdir(mode=0o700)
    # Do not inherit operator runtime/provider/ops configuration or credentials.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
           "PYTHONPATH": str(ROOT), "LOOPER_EVAL_API_KEY": key, "TMPDIR": str(temporary),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    result = asyncio.run(run_process(eval_argv(command, pg_url, out_dir), timeout=OUTER_TIMEOUT,
                                     env=env, capture=False))
    paths = list(out_dir.glob("*-real.json"))
    run = json.loads(paths[0].read_text()) if len(paths) == 1 else {}
    summary = summarize(run, (CASES[command],))
    try:
        base = json.loads(baseline.read_text()) if baseline else None
    except (OSError, ValueError):
        base = None
    summary["baseline"] = baseline_status(base, (CASES[command],), dialogs=command == "manager")
    if summary["baseline"]["comparable"]:
        # The comparison stays private, including the input baseline's provenance.
        (out_dir / "comparison.md").write_text(compare.markdown(run, base), encoding="utf-8")
    summary["outer_timeout"] = result.timed_out
    summary["eval_exit_code"] = result.returncode
    (out_dir / "validation-summary.json").write_text(json.dumps(summary, ensure_ascii=False) + "\n")
    passed = (result.returncode == 0 and not result.timed_out and not summary["aborted"] and
              summary["outcomes"] == {"pass": 1, "fail": 0, "error": 0, "skipped": 0, "untested": 0})
    return summary, 0 if passed else (2 if result.timed_out or result.returncode == 2 or not run else 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", *CASES):
        command = sub.add_parser(name)
        command.add_argument("--disposable-pg-url", help="显式声明为可丢弃的本机 PG 实例（仅 postgres 管理库）")
        if name != "preflight":
            command.add_argument("--real", action="store_true", help="明确授权读取 key 并运行付费模型")
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
