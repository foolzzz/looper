"""The PreToolUse hook a Manager's Claude Code runs before each tool call while the outbound review is on
(``outbound_hook``): it asks ``looper serve`` (``POST /api/agent/v1/outbound/check``, with the Manager's token) and
prints Claude Code's answer, ``{}`` to let the call run or a ``deny`` with the reason.

It is run by path with ``python -I`` and imports only the standard library, so it starts fast and nothing on the
machine changes what it imports. Anything that goes wrong (no config, Looper does not answer, an answer it cannot
read) prints a ``deny``: Claude Code runs a call when its hook fails or outlasts its timeout, so this program never
fails, and an alarm set before anything else ends it with a ``deny`` once its deadline passes, whatever it is
waiting for.

    python -I outbound_hook_client.py <config file>    (the hook's JSON on stdin)
"""

import os
import signal

DEADLINE_SEC = 75  # until the config gives its own; less than Claude Code's wait for the hook
UNANSWERED = ("Looper's outbound check did not answer, so this action did not run. Tell the user what you were "
              "about to do; do not try to do the same thing another way.")
DENY = ('{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
        '"permissionDecisionReason": "' + UNANSWERED + '"}}')


def _expired(signum, frame):
    os.write(1, DENY.encode("utf-8"))
    os._exit(0)


signal.signal(signal.SIGALRM, _expired)
signal.alarm(DEADLINE_SEC)

import json  # noqa: E402 - after the alarm: nothing below may outlast the deadline
import sys  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402

# What the hook passes on: the call, and what places it (its id, the session, the subagent).
_FIELDS = ("tool_name", "tool_input", "tool_use_id", "session_id", "agent_id", "cwd")


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _config(config_path, started):
    """The hook's config, its deadline (no later than ``DEADLINE_SEC``) set as the alarm before anything waits on
    stdin; None when it cannot be read or the deadline has passed."""
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        config["deadline"] = min(float(config.get("deadline", DEADLINE_SEC)), DEADLINE_SEC)
    except Exception:
        return None
    left = config["deadline"] - (time.monotonic() - started)
    if left <= 1:
        return None
    signal.alarm(int(left))
    return config


def answer(config, stdin, started):
    """Claude Code's answer for the hook's input ``stdin``: ``{}`` (run it) only when Looper said ``allow``."""
    try:
        left = config["deadline"] - (time.monotonic() - started)
        if left <= 1:
            return deny(UNANSWERED)
        payload = json.loads(stdin or "{}")
        body = {key: payload.get(key) for key in _FIELDS if key in payload}
        headers = {"Authorization": "Bearer " + config["token"], "Content-Type": "application/json",
                   "Accept": "application/json"}
        request = urllib.request.Request(config["url"].rstrip("/") + "/api/agent/v1/outbound/check",
                                         data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
        # The URL is the local server that started the Manager; a proxy from the environment must not carry it.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=min(float(config["timeout"]), left)) as response:
            verdict = json.loads(response.read().decode("utf-8"))
        if verdict.get("decision") == "allow":
            return {}
        reason = verdict.get("message")
        return deny(reason if isinstance(reason, str) and reason else UNANSWERED)
    except Exception:
        return deny(UNANSWERED)


def main(argv):
    started = time.monotonic()
    config = _config(argv[1], started) if len(argv) > 1 else None  # its deadline holds while stdin is read
    if config is None:
        out = deny(UNANSWERED)
    else:
        try:
            stdin = sys.stdin.read()
        except Exception:
            stdin = ""
        out = answer(config, stdin, started)
    signal.alarm(0)
    os.write(1, json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
