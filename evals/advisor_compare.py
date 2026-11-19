"""The three-group comparison of the advisor (the advisor RFC, section 16): its harness's cost estimate.

The comparison retries hard problems (snapshots of steps whose Worker failed twice or more) three ways: the advisor
given the raw context (``raw``), the advisor given an abstract request (``abstract``, what Looper sends), and no
advisor but the retry's effort raised with the same budget (``effort``). It passes when the abstract group's pass
rate is at most 5 points below the raw group's, it costs less, and nothing leaks.

Running it costs money and needs a set of hard-problem snapshots (step attempts, M1), so this phase builds only the
estimate (``estimate``): what one run of the comparison would cost, from the measured cost of one round and the
token sizes given, priced at the built-in prices. Nothing here starts a model run.

    uv run python -m evals.advisor_compare estimate --problems 30 --reps 3
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from looper.agents.run_usage import Usage
from looper.agents.runtimes.pricing import BUILTIN_PRICES

GROUPS = ("raw", "abstract", "effort")


@dataclass(frozen=True)
class Sizes:
    """The tokens of one problem's runs, by kind. The defaults are this phase's measurements and guesses: an abstract
    request of about 3,000 tokens and an answer of about 3,000 (one real round: 0.18 USD), a raw context of 150,000
    (a step's files, logs and reports), a judge's check of 3,000 in and 400 out, and a retried Worker's run of
    400,000 cache-read, 60,000 written and 15,000 out."""

    advisor_model: str = "claude-fable-5-2"
    judge_model: str = "claude-haiku-4-5"
    worker_model: str = "claude-sonnet-5-5"
    abstract_in: int = 3_000
    raw_in: int = 150_000
    advice_out: int = 3_000
    judge_in: int = 3_000
    judge_out: int = 400
    worker_cache_read: int = 400_000
    worker_cache_write: int = 60_000
    worker_out: int = 15_000
    effort_factor: float = 1.5  # the effort group's retry, with its effort raised, against a plain one


def _cost(model: str, **tokens: int) -> float:
    price = BUILTIN_PRICES[model]
    cost = price.cost(Usage(**tokens))
    if cost is None:
        raise ValueError(f"model {model!r} has no full built-in price")
    return cost


def per_problem(sizes: Sizes) -> dict[str, float]:
    """What one run of one problem costs in each group: the consult (none for ``effort``) and the retry after it."""
    worker = _cost(sizes.worker_model, cache_read_tokens=sizes.worker_cache_read,
                   cache_creation_tokens=sizes.worker_cache_write, output_tokens=sizes.worker_out)
    judge = _cost(sizes.judge_model, input_tokens=sizes.judge_in, output_tokens=sizes.judge_out)
    return {
        "raw": round(_cost(sizes.advisor_model, cache_creation_tokens=sizes.raw_in, output_tokens=sizes.advice_out)
                     + worker, 4),
        "abstract": round(_cost(sizes.advisor_model, cache_creation_tokens=sizes.abstract_in,
                                output_tokens=sizes.advice_out) + judge + worker, 4),
        "effort": round(worker * sizes.effort_factor, 4),
    }


def estimate(problems: int, reps: int, sizes: Sizes = Sizes()) -> dict[str, object]:
    """The estimated cost of the comparison: ``problems`` hard problems, each run ``reps`` times in every group."""
    if problems < 1 or reps < 1:
        raise ValueError("give at least one problem and one repetition")
    each = per_problem(sizes)
    groups = {group: round(cost * problems * reps, 2) for group, cost in each.items()}
    return {"problems": problems, "reps": reps, "per_run_usd": each, "groups_usd": groups,
            "total_usd": round(sum(groups.values()), 2), "sizes": asdict(sizes)}


def _table(result: Mapping[str, object]) -> str:
    each, groups = result["per_run_usd"], result["groups_usd"]
    lines = [f"Advisor comparison, estimated: {result['problems']} problems x {result['reps']} runs per group", "",
             "| group | per run (USD) | group (USD) |", "|---|---:|---:|"]
    lines += [f"| {group} | {each[group]:.4f} | {groups[group]:.2f} |" for group in GROUPS]
    lines += [f"| total | | {result['total_usd']:.2f} |", "",
              "Nothing ran: this is the dry-run estimate; a real run needs the hard-problem set and the maintainer's "
              "budget approval."]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.advisor_compare")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("estimate", help="the comparison's estimated cost (no model runs)")
    run.add_argument("--problems", type=int, default=30)
    run.add_argument("--reps", type=int, default=3)
    run.add_argument("--raw-tokens", type=int, default=Sizes.raw_in)
    run.add_argument("--abstract-tokens", type=int, default=Sizes.abstract_in)
    run.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = estimate(args.problems, args.reps, Sizes(raw_in=args.raw_tokens, abstract_in=args.abstract_tokens))
    print(json.dumps(result, indent=1) if args.json else _table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
