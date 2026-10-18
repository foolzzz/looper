"""``python -m evals.prompt_regression run|dialogs|compare`` (see ``evals/README.md``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from looper import env

from . import compare, dialogs, runner


def _reps(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--reps takes a whole number, not {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"--reps must be at least 1, not {value}")
    return value


def _jobs(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--jobs takes a whole number, not {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"--jobs must be at least 1, not {value}")
    return value


_JOBS_HELP = ("run this many cases at once (default 1: one after another). Rows stay in task/rep order, and "
              "--total-cap-usd still holds: a case starts only when what is spent plus the --max-budget-usd of every "
              "case in flight and of this one is within it")
_REPS_HELP = ("run each case this many times (default 1), rep 1 of every case first; --total-cap-usd covers them "
              "all. A rerun is a new run: never keep the best of several")
_BASELINE_HELP = ("compare with this run file, or with the newest earlier run in --out-dir (previous); default: the "
                  "committed baseline of the mode and model")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.prompt_regression", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run the task set and compare it with the previous run or the baseline.")
    run.add_argument("--mode", choices=("fake", "real"), default="fake",
                     help="fake: the scripted stand-in Claude (free); real: Claude Code (costs money)")
    run.add_argument("--model", default="haiku",
                     help="real mode: every role runs Claude Code with this model (default haiku)")
    run.add_argument("--agent", help="every role runs this agent, by name, instead (a registered agent of the "
                                     "eval's new database, such as the built-in claude-code/opus)")
    run.add_argument("--register-agent", type=Path, metavar="SPEC.json",
                     help="register this agent (runtime, provider, agent; the provider's key from the variable "
                          "its api_key_env names) and run every role on it; see evals/README.md")
    run.add_argument("--provider-preset",
                     help="every role runs Claude Code with a provider registered from this preset's "
                          f"Anthropic-format API (such as glm-bigmodel), its API key from ${runner.API_KEY_ENV}")
    run.add_argument("--provider-base-url", help="with --provider-preset: this base URL instead of the preset's")
    run.add_argument("--agent-model", help="with --provider-preset: the agent's model (default: the preset's first)")
    run.add_argument("--tasks", help="comma-separated task ids or task sets ("
                     + ", ".join(sorted(runner.TASK_SETS)) + "; default: all)")
    run.add_argument("--reps", type=_reps, default=1, help=_REPS_HELP)
    run.add_argument("--jobs", type=_jobs, default=1, help=_JOBS_HELP)
    run.add_argument("--max-budget-usd", type=float, default=0.25,
                     help="real mode: the cap of every Claude Code run (default 0.25): --max-budget-usd, or the cap "
                          "Looper keeps at the provider's prices with --provider-preset")
    run.add_argument("--total-cap-usd", type=float, default=0.80,
                     help="real mode: stop the run once its tasks have spent this much (default 0.80)")
    run.add_argument("--pg-url", default=env.get("LOOPER_EVAL_DATABASE_URL") or env.get("LOOPER_TEST_DATABASE_URL"),
                     help="a PostgreSQL server to create the run's database on "
                          "(default $LOOPER_EVAL_DATABASE_URL, else $LOOPER_TEST_DATABASE_URL)")
    run.add_argument("--out-dir", type=Path, default=runner.DEFAULT_OUT_DIR,
                     help="where the run's JSON and server log go (default evals/runs, ignored by git)")
    run.add_argument("--baseline", metavar="RUN.json|previous", help=_BASELINE_HELP)
    run.add_argument("--task-timeout", type=float, help="seconds before a task is cancelled")
    run.add_argument("--keep-work-dirs", action="store_true", help="keep the temporary root for inspection")
    run.add_argument("--step-workspaces", choices=("shared", "worktree"),
                     help="make each working directory a git repository and give the task's project this "
                          "step_workspaces setting")
    run.add_argument("--user-gates", choices=("on", "off"),
                     help="give every task's project this user_gates setting, over the task's own (gate tasks "
                          "turn it on)")

    talk = sub.add_parser("dialogs", help="Run the Manager's dialogs and check what Looper did after each reply.")
    talk.add_argument("--mode", choices=("fake", "real"), default="fake")
    talk.add_argument("--model", default="haiku", help="real mode: every role runs Claude Code with this model")
    talk.add_argument("--agent", help="every role runs this agent, by name, instead")
    talk.add_argument("--register-agent", type=Path, metavar="SPEC.json",
                      help="register this agent and run every role on it, as for run")
    talk.add_argument("--manager-model", help="the Manager alone runs Claude Code with this model (its role layer)")
    talk.add_argument("--manager-effort", help="with --manager-model: its effort, such as xhigh")
    talk.add_argument("--dialogs", help="comma-separated dialog ids (default: all)")
    talk.add_argument("--reps", type=_reps, default=1, help=_REPS_HELP)
    talk.add_argument("--jobs", type=_jobs, default=1, help=_JOBS_HELP)
    talk.add_argument("--baseline", metavar="RUN.json|previous", help=_BASELINE_HELP)
    talk.add_argument("--max-budget-usd", type=float, default=0.25, help="real mode: the cap of every run")
    talk.add_argument("--total-cap-usd", type=float, default=0.50,
                      help="real mode: stop once the run has spent this much (default 0.50)")
    talk.add_argument("--pg-url", default=env.get("LOOPER_EVAL_DATABASE_URL") or env.get("LOOPER_TEST_DATABASE_URL"))
    talk.add_argument("--out-dir", type=Path, default=runner.DEFAULT_OUT_DIR)
    talk.add_argument("--timeout", type=float, help="seconds to wait for each reply and each check")
    talk.add_argument("--keep-work-dirs", action="store_true")

    cmp = sub.add_parser("compare", help="Print the comparison table of two run files.")
    cmp.add_argument("new", type=Path)
    cmp.add_argument("base", type=Path, nargs="?")
    return parser


def exit_code(result: dict[str, Any]) -> int:
    """0 only when every row ran and passed (no fail, no error, nothing skipped); 2 when the run stopped midway."""
    if result.get("aborted"):
        return 2
    return 0 if all(compare.outcome(row) == runner.Outcome.PASS for row in compare.rows(result)) else 1


def _report(result: dict[str, Any], path: Path, args: argparse.Namespace, *, is_dialogs: bool) -> None:
    """The run's file, its cost, the base it is compared with (its commit and reps) and the table."""
    base, where = runner.comparison_base(args.baseline, out_dir=args.out_dir, current=path, mode=args.mode,
                                         model=runner.compared_model(result), dialogs=is_dialogs)
    print(f"Run written to {path}")
    print(f"Total cost: {result['total_cost_usd']:.4f} USD")
    if base is None:
        print(f"Base: none ({where})\n")
    else:
        commit = str((base.get("git") or {}).get("commit") or "?")[:9]
        print(f"Base: {where} @ {commit}, {compare.reps(base)} rep(s) per case\n")
    print(compare.markdown(result, base))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "compare":
        new = json.loads(args.new.read_text(encoding="utf-8"))
        base = json.loads(args.base.read_text(encoding="utf-8")) if args.base else None
        print(compare.markdown(new, base))
        if "dialogs" in new:
            print("\n" + dialogs.markdown(new))
        return 0

    if not args.pg_url:
        print("error: give --pg-url (or set LOOPER_EVAL_DATABASE_URL)", file=sys.stderr)
        return 2
    if args.command == "dialogs":
        return _dialogs(args)
    try:
        selected = runner.select_tasks(args.tasks.split(",") if args.tasks else None)
        real = args.mode == "real"
        provider = runner.ProviderChoice(args.provider_preset, args.agent_model, args.provider_base_url,
                                         env.get(runner.API_KEY_ENV)) if args.provider_preset else None
        result, path = runner.run(
            mode=args.mode,
            tasks=selected,
            admin_url=args.pg_url,
            out_dir=args.out_dir,
            model=args.model if real and not args.agent and provider is None and not args.register_agent else None,
            agent=args.agent,
            provider=provider,
            register=json.loads(args.register_agent.read_text(encoding="utf-8")) if args.register_agent else None,
            max_budget_usd=args.max_budget_usd if real else None,
            total_cap_usd=args.total_cap_usd if real else None,
            task_timeout=args.task_timeout,
            keep_work_dirs=args.keep_work_dirs,
            step_workspaces=args.step_workspaces,
            reps=args.reps,
            user_gates=args.user_gates,
            jobs=args.jobs,
        )
    except (ValueError, runner.EvalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _report(result, path, args, is_dialogs=False)
    return exit_code(result)


def _dialogs(args: argparse.Namespace) -> int:
    real = args.mode == "real"
    try:
        result, path = dialogs.run(
            mode=args.mode, dialogs=dialogs.select(args.dialogs.split(",") if args.dialogs else None),
            admin_url=args.pg_url, out_dir=args.out_dir,
            model=args.model if real and not args.agent and not args.register_agent else None, agent=args.agent,
            register=json.loads(args.register_agent.read_text(encoding="utf-8")) if args.register_agent else None,
            max_budget_usd=args.max_budget_usd if real else None, total_cap_usd=args.total_cap_usd if real else None,
            timeout=args.timeout, keep_work_dirs=args.keep_work_dirs,
            manager_model=args.manager_model if real else None, manager_effort=args.manager_effort, reps=args.reps,
            jobs=args.jobs,
        )
    except (ValueError, runner.EvalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _report(result, path, args, is_dialogs=True)
    print("\n" + dialogs.markdown(result))
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
