"""A model-free ACP v1 stdio process, independent of vendor implementations.

Run with ``python tests/fake_acp.py --scenario success``. All optional tracing
stays under the explicitly supplied scratch directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="success")
    parser.add_argument("--trace", type=Path)
    parser.add_argument("transport", nargs="*")
    args = parser.parse_args()
    if args.transport not in ([], ["agent", "stdio"]):
        parser.error("the stand-in supports only agent stdio")
    if args.scenario == "early_exit":
        return

    def output(**message):
        print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)

    def update(update_type, **values):
        output(method="session/update", params={"sessionId": "review-session", "update": {
            "sessionUpdate": update_type, **values}})

    def trace(message):
        if args.trace:
            with args.trace.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(message) + "\n")

    prompt_id = None

    def finish():
        verdict = {"passed": args.scenario != "not_passed", "summary": "stand-in review", "findings": []}
        text = "review complete" if args.scenario == "no_verdict" else json.dumps(verdict)
        update("agent_message_chunk", content={"type": "text", "text": text})
        response = {"messageId": "v2-message"} if args.scenario == "v2_prompt" else {"stopReason": "end_turn"}
        if args.scenario == "no_text":
            # This path is handled before the first text chunk is sent.
            raise AssertionError
        if args.scenario == "vendor_usage":
            response["_meta"] = {"usage": {"inputTokens": 12, "outputTokens": 3, "costUsdTicks": 100}}
            response["usage"] = {"inputTokens": 12, "outputTokens": 3, "model": "another-model"}
        if args.scenario == "usd_cost":
            update("usage_update", used=51, size=1000, cost={"amount": 0.123, "currency": "USD"})
        if args.scenario == "tools":
            update("tool_call", toolCallId="read-1", title="Read file", kind="read", rawInput={"path": "README.md"})
            update("tool_call_update", toolCallId="read-1", status="completed",
                   content=[{"type": "content", "content": {"type": "text", "text": "example contents"}}])
        output(id=prompt_id, result=response)
        if args.scenario == "success_stays_alive":
            time.sleep(30)  # A v1 result completes the review, not this process.

    for line in sys.stdin:
        message = json.loads(line)
        trace(message)
        method = message.get("method")
        if method == "initialize":
            output(id=message["id"], result={"protocolVersion": 2 if args.scenario == "v2" else 1,
                                            "agentCapabilities": {}})
        elif method == "session/new":
            if args.scenario != "session_timeout":
                output(id=message["id"], result={"sessionId": "review-session"})
        elif method == "session/prompt":
            prompt_id = message["id"]
            if args.scenario == "hang":
                update("agent_message_chunk", content={"type": "text", "text": "waiting"})
            elif args.scenario in {"permission_write", "permission_unknown", "permission_read", "permission_search",
                                   "permission_misleading"}:
                kinds = {"permission_write": "edit", "permission_unknown": "not-a-kind",
                         "permission_read": "read", "permission_search": "search", "permission_misleading": "execute"}
                output(id="permission-1", method="session/request_permission", params={
                    "sessionId": "review-session", "toolCall": {
                        "toolCallId": "permission-tool", "kind": kinds[args.scenario],
                        "title": "read grep harmless", "rawInput": {"command": "write something"}},
                    "options": [{"kind": "allow_once", "optionId": "yes", "name": "read only"},
                                {"kind": "reject_once", "optionId": "no", "name": "do not read"}]})
            elif args.scenario == "no_text":
                output(id=prompt_id, result={"stopReason": "end_turn"})
            else:
                finish()
        elif method == "session/cancel":
            return
        elif message.get("id") == "permission-1":
            finish()


if __name__ == "__main__":
    main()
