"""The ``claude`` the advisor acceptance's server runs (``evals/advisor_acceptance.py``).

Every run is real Claude Code (capped with ``--max-budget-usd`` when the call sets no cap of its own), except the
advisor's or the judge's run when the acceptance's mode file says to stand in for it:

- ``{"advisor": "timeout"}``: an advisor that never answers;
- ``{"advisor": "rate_limited"}``: one its model's rate limit stops (HTTP 429);
- ``{"judge": "block"}``: a judge that finds an internal name in every request.

A run is the advisor's or the judge's by its system prompt, which ends with that role's reply contract. Standard
library only: ``python <this file> <real claude> <mode file> <cap USD> -- <claude's arguments>``.
"""

from __future__ import annotations

import json
import os
import sys
import time

# A line of each reply contract (``looper.agents.advice``); ``tests/test_advisor_acceptance.py`` keeps them in step.
ADVISOR_MARK = '{"status": "answered | needs_info | out_of_scope",'
JUDGE_MARK = "Reply with exactly one JSON object and nothing else, not even a sentence"
BLOCKING_VERDICT = {"standalone": True, "answerable": 0.9, "missing": [],
                    "leaks": [{"category": "internal_name", "field": "problem", "confidence": "high"}]}


def role(args: list[str]) -> str | None:
    """``advisor``, ``judge``, or None (any other run), by the system prompt file the run is given."""
    if "--system-prompt-file" not in args:
        return None
    try:
        with open(args[args.index("--system-prompt-file") + 1], encoding="utf-8") as prompt:
            text = prompt.read()
    except (OSError, IndexError):
        return None
    if JUDGE_MARK in text:
        return "judge"
    return "advisor" if ADVISOR_MARK in text else None


def stand_in(mode: str, model: str) -> list[dict]:
    """The stream-json events of a stood-in run, after its init event; [] for an advisor that never answers."""
    if mode == "timeout":
        return []
    if mode == "rate_limited":
        return [{"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "s",
                 "result": "", "api_error_status": 429, "total_cost_usd": 0.0,
                 "modelUsage": {model: {"costUSD": 0.0}}}]
    if mode == "block":
        text = json.dumps(BLOCKING_VERDICT)
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        return [{"type": "assistant", "session_id": "s",
                 "message": {"id": "m", "role": "assistant", "model": model,
                             "content": [{"type": "text", "text": text}], "usage": usage}},
                {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": "s",
                 "stop_reason": "end_turn", "total_cost_usd": 0.0, "usage": usage,
                 "modelUsage": {model: {"costUSD": 0.0}}}]
    raise ValueError(f"unknown stand-in mode {mode!r}")


def main(argv: list[str]) -> int:
    real, mode_file, cap = argv[1], argv[2], argv[3]
    args = argv[argv.index("--") + 1:]
    who = role(args)
    try:
        with open(mode_file, encoding="utf-8") as modes:
            mode = json.load(modes).get(who or "", "real")
    except (OSError, ValueError):
        mode = "real"
    if who is None or mode == "real":
        if cap and "--max-budget-usd" not in args:
            args = [*args, "--max-budget-usd", cap]
        os.execv(real, [real, *args])
    model = args[args.index("--model") + 1] if "--model" in args else "stand-in"
    sys.stdin.readline()  # the turn
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "s", "model": model, "tools": [],
                      "mcp_servers": []}), flush=True)
    events = stand_in(mode, model)
    if not events:
        time.sleep(3600)  # until its run's timeout stops it
    for event in events:
        print(json.dumps(event), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
