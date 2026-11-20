"""The PreToolUse hook a Manager's Claude Code runs before each tool call while the outbound review is on
(``outbound_hook``): it asks ``looper serve`` (``POST /api/agent/v1/outbound/check``, with the Manager's token) and
prints Claude Code's answer, ``{}`` to let the call run or a ``deny`` with the reason.

It is run by path with ``python -I`` and imports only the standard library, so it starts fast and nothing on the
machine changes what it imports. Anything that goes wrong (no config, Looper does not answer in time, an answer it
cannot read) prints a ``deny``: Claude Code lets a call run when a hook fails, so this program never fails.

    python -I outbound_hook_client.py <config file>    (the hook's JSON on stdin)
"""

from __future__ import annotations

import json
import sys
import urllib.request

UNANSWERED = ("Looper's outbound check did not answer, so this action did not run. Tell the user what you were "
              "about to do; do not try to do the same thing another way.")
# What the hook passes on: the call, and what places it (the session, the subagent).
_FIELDS = ("tool_name", "tool_input", "tool_use_id", "session_id", "agent_id", "cwd")


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _binding(turn_file: str | None) -> str | None:
    """The turn the Manager runs now, as its MCP bridge reads it (``ManagerOps.publish_binding``)."""
    if not turn_file:
        return None
    try:
        with open(turn_file, encoding="utf-8") as handle:
            turn = json.load(handle).get("turn")
    except (OSError, ValueError, AttributeError):
        return None
    return turn if isinstance(turn, str) and turn.isprintable() and turn else None


def answer(config_path: str, stdin: str) -> dict:
    """Claude Code's answer for the hook's input ``stdin``: ``{}`` (run it) only when Looper said ``allow``."""
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        payload = json.loads(stdin or "{}")
        body = {key: payload.get(key) for key in _FIELDS if key in payload}
        headers = {"Authorization": f"Bearer {config['token']}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        if (binding := _binding(config.get("turn_file"))) is not None:
            headers["X-Looper-Turn"] = binding
        request = urllib.request.Request(config["url"].rstrip("/") + "/api/agent/v1/outbound/check",
                                         data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
        # The URL is the local server that started the Manager; a proxy from the environment must not carry it.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=float(config["timeout"])) as response:
            verdict = json.loads(response.read().decode("utf-8"))
        if verdict.get("decision") == "allow":
            return {}
        reason = verdict.get("message")
        return deny(reason if isinstance(reason, str) and reason else UNANSWERED)
    except Exception:
        return deny(UNANSWERED)


def main(argv: list[str]) -> int:
    try:
        stdin = sys.stdin.read()
    except Exception:
        stdin = ""
    out = answer(argv[1], stdin) if len(argv) > 1 else deny(UNANSWERED)
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
