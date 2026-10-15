"""``python -m evals.prompt_regression run|dialogs|compare`` (see ``evals/README.md``)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from looper import env

from . import compare, dialogs, runner, tasks


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
    run.add_argument("--tasks", help="comma-separated task ids (default: all)")
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
    run.add_argument("--baseline", type=Path,
                     help="compare with this run instead of the previous one (or the committed baseline)")
    run.add_argument("--task-timeout", type=float, help="seconds before a task is cancelled")
    run.add_argument("--keep-work-dirs", action="store_true", help="keep the temporary root for inspection")
    run.add_argument("--step-workspaces", choices=("shared", "worktree"),
                     help="make each working directory a git repository and give the task's project this "
                          "step_workspaces setting")

    talk = sub.add_parser("dialogs", help="Run the Manager's dialogs and check what Looper did after each reply.")
    talk.add_argument("--mode", choices=("fake", "real"), default="fake")
    talk.add_argument("--model", default="haiku", help="real mode: every role runs Claude Code with this model")
    talk.add_argument("--agent", help="every role runs this agent, by name, instead")
    talk.add_argument("--register-agent", type=Path, metavar="SPEC.json",
                      help="register this agent and run every role on it, as for run")
    talk.add_argument("--dialogs", help="comma-separated dialog ids (default: all)")
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "compare":
        new = json.loads(args.new.read_text(encoding="utf-8"))
        base = json.loads(args.base.read_text(encoding="utf-8")) if args.base else None
        print(compare.markdown(new, base))
        return 0

    if not args.pg_url:
        print("error: give --pg-url (or set LOOPER_EVAL_DATABASE_URL)", file=sys.stderr)
        return 2
    if args.command == "dialogs":
        return _dialogs(args)
    try:
        selected = tasks.select(args.tasks.split(",") if args.tasks else None)
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
        )
    except (ValueError, runner.EvalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.baseline:
        base = json.loads(args.baseline.read_text(encoding="utf-8"))
    else:
        base = runner.previous_run(args.out_dir, path, mode=args.mode, model=result.get("model"))
    print(f"Run written to {path}")
    print(f"Total cost: {result['total_cost_usd']:.4f} USD\n")
    print(compare.markdown(result, base))
    return 0 if all(t.get("success") for t in result["tasks"]) else 1


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
        )
    except (ValueError, runner.EvalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Run written to {path}")
    print(dialogs.markdown(result))
    return 0 if all(d.get("success") for d in result["dialogs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
