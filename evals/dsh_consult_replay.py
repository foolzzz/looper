"""Opt-in real DeepSeek replay of the synthetic serve test's ended Worker.

Run ``python -m evals.dsh_consult_replay --capture DIR/consult-source.json --out DIR``.
For the before version, pass --runner-entry pointing to a local copy of that
revision's runners/dsh/src/main.ts. Requires the test extra and installed DSH.
Requests, responses and frames are private local evidence; never commit them.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import urllib.request

from looper.agents.consult import CONSULT_CONTRACT, consult_answers
from looper.agents.runtimes.runner import dsh_command


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def replay(args):
    root = Path.cwd().resolve()

    def local(value):
        path = Path(value).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Replay inputs and output must be inside this worktree")
        return path

    source = json.loads(local(args.capture).read_text())
    state, cwd, output = local(source["state"]), local(source["cwd"]), local(args.out)
    output.mkdir(parents=True, exist_ok=False)
    if args.runner_entry:
        entry = local(args.runner_entry)
        dsh_command.entry = lambda: entry
    # Reuse the installed-test isolation and the host protocol implementation.
    sys.path.insert(0, str(root / "tests"))
    from dsh_runner_support import guard_env, raw_lease, raw_runner, require_dsh
    from test_dsh_runner_tools import Client

    _, executable = require_dsh()
    (directory,) = state.glob(f"*/{source['source']}")

    def hashes():
        return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.rglob("*") if p.is_file()}

    before = hashes()
    if not before:
        raise ValueError("Source session has no durable files")
    questions = [int(n) for n in re.findall(r"#(\d+) from", source["prompt"])]
    if not questions:
        raise ValueError("Capture contains no room questions")
    results = []
    for rep in range(args.reps):
        run = output / f"rep-{rep}"
        run.mkdir()
        requests, errors = [], []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    if self.path != "/v1/messages" or len(requests) >= 8:
                        self.send_error(409)
                        return
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    index = len(requests)
                    requests.append(body)
                    (run / f"request-{index}.json").write_text(json.dumps(body, indent=2))
                    # The capture is synthetic. Remove workstation paths before
                    # sending it, and map model tool paths back for real execution.
                    wire = json.loads(json.dumps(body).replace(str(root), "/workspace"))
                    wire["max_tokens"] = 4096
                    (run / f"wire-{index}.json").write_text(json.dumps(wire, indent=2))
                    request = urllib.request.Request("https://api.deepseek.com/anthropic/v1/messages",
                        data=json.dumps(wire).encode(), headers={"x-api-key": os.environ["DEEPSEEK_API_KEY"],
                            "anthropic-version": "2023-06-01", "content-type": "application/json"})
                    with urllib.request.urlopen(request, timeout=65) as response:
                        payload = response.read()
                    (run / f"response-{index}.sse").write_bytes(payload)
                    payload = payload.replace(b"/workspace", str(root).encode())
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as error:
                    errors.append(type(error).__name__)  # Never print credential-bearing errors.
                    self.send_error(502)

            def log_message(self, *unused):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                async with raw_runner(run, executable, env=guard_env(run, server.server_port),
                                      state_root=state) as (process, send, _):
                    async def receive():
                        return json.loads(await asyncio.wait_for(process.stdout.readline(), 95))

                    client = Client(send, receive)
                    await client.start()
                    from dsh_runner_support import session_params
                    params = session_params(raw_lease(run), role="worker", cwd=str(cwd),
                        system_prompt={"mode": "append", "text": source["system_prompt"]},
                        fork_from={"session_id": source["source"]})
                    params["provider"].update(base_url=f"http://127.0.0.1:{server.server_port}",
                                              model="deepseek-flash", effort="high")
                    params["limits"]["max_requests_per_turn"] = 8
                    sid = (await client.call("session/new", params))["session_id"]
                    await client.prompt(sid, text=source["prompt"])
                    settled = await client.settled(sid)
                    (run / "frames.json").write_text(json.dumps(client.frames, indent=2))
                    await client.call("session/close", {"session_id": sid})
            finally:
                server.shutdown()
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("Replay server did not stop")
        if errors:
            raise RuntimeError("Real provider request failed: " + ", ".join(errors))
        if hashes() != before:
            raise RuntimeError("Replay changed the source session")
        usage = [f["params"] for f in client.frames if f.get("method") == "session/update"
                 and f["params"]["kind"] == "request_end"]
        try:
            answers, _ = consult_answers(settled["final_text"], questions)
            shape = "answers"
        except ValueError:
            answers, shape = {}, "invalid_consult"
        first = requests[0]
        result = {"outcome": settled["outcome"], "answer_shape": shape,
            "answered": sorted(answers), "requests": len(usage), "usage_complete": settled["usage_complete"],
            "first_input_tokens": sum(usage[0][k] for k in ("input_uncached", "cache_read", "cache_write")),
            "total_input_tokens": sum(e[k] for e in usage for k in ("input_uncached", "cache_read", "cache_write")),
            "output_tokens": sum(e["output"] for e in usage),
            "system_sha256": digest(first["system"]), "tools_sha256": digest(first["tools"]),
            "messages_sha256": digest(first["messages"]),
            "system_utf8_bytes": len(first["system"].encode()), "tool_count": len(first["tools"]),
            "consult_contract_count": first["system"].count(CONSULT_CONTRACT)}
        results.append(result)
        (output / "summary.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--runner-entry", type=Path)
    parser.add_argument("--reps", type=int, default=2, choices=(1, 2))
    args = parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("DEEPSEEK_API_KEY is required")
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
