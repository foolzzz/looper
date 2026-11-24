"""Bash IPC failures settle once without depending on executor cooperation."""
import json
import os
from pathlib import Path
import signal
import subprocess

import pytest

from dsh_runner_support import require_node

pytestmark = pytest.mark.installed


@pytest.mark.parametrize("fault", ["no_ack", "late_ack", "ack_after_kill", "no_result", "late_result"])
@pytest.mark.parametrize("ending", ["timeout", "abort"])
def test_bash_executor_failure_has_a_hard_return(tmp_path, fault, ending):
    node = require_node()
    executor = tmp_path / "executor.cjs"
    executor.write_text("""
process.on('SIGTERM', () => {});
process.on('message', () => {
  if (process.env.FAULT === 'ack_after_kill') setTimeout(() => process.send({pid: process.pid}), 2300);
  else if (process.env.FAULT === 'late_ack') setTimeout(() => process.send({pid: process.pid}), 200);
  else if (process.env.FAULT !== 'no_ack') process.send({pid: process.pid});
  if (process.env.FAULT === 'late_result') setTimeout(() => process.send({code: 0}), 150);
});
setInterval(() => {}, 1000);
""")
    module = Path(__file__).resolve().parents[1] / "runners/dsh/src/exec.ts"
    script = tmp_path / "probe.ts"
    script.write_text(f"""
import {{fork}} from 'node:child_process';
import {{writeFileSync}} from 'node:fs';
import {{registerBash}} from {json.dumps(module.as_uri())};
let definition, child;
const record = {{id: 'session-control', builtin: ['bash'], cwd: process.cwd(), roots: [process.cwd()],
  env: {{}}, home: process.cwd(), limits: {{tool_timeout_s: 86400}}, processes: new Set(),
  agentCtx: {{tools: {{register(tool) {{definition = tool;}}}}}}}};
registerBash(record, () => {{
  child = fork({json.dumps(str(executor))}, [], {{env: {{FAULT: {json.dumps(fault)}}}, detached: true,
    stdio: ['ignore', 'ignore', 'ignore', 'ipc']}});
  writeFileSync('executor.pid', String(child.pid));
  return child;
}});
const controller = new AbortController();
if ({json.dumps(ending)} === 'abort') setTimeout(() => controller.abort(), 100);
const started = performance.now();
let settles = 0;
try {{
  const text = await definition.execute({{command: 'sensitive-command-control', timeoutMs: 100}},
    {{signal: controller.signal, callId: 'call-control'}});
  settles++;
  console.log(JSON.stringify({{text, settles, elapsed: performance.now() - started}}));
}} catch (error) {{
  settles++;
  console.log(JSON.stringify({{error: error.name, settles, elapsed: performance.now() - started}}));
}}
""")
    process = subprocess.Popen([node, script], cwd=tmp_path, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    pid = None
    try:
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result["settles"] == 1 and result["elapsed"] < 4000
        if ending == "timeout":
            assert "timed out after 0.1 s" in result["text"]
        else:
            assert result["error"] == "AbortError"
        spans = [json.loads(line.removeprefix("dsh.tool ")) for line in stderr.splitlines()
                 if line.startswith("dsh.tool ")]
        assert spans and all(span["session_id"] == "session-control" and
                             span["call_id"] == "call-control" for span in spans)
        assert len([span for span in spans if span["phase"] == "result"]) == 1
        assert spans[0]["timeout_ms"] == 100
        assert "sensitive-command-control" not in stderr
        assert len(spans[0]["command_hash"]) == 64
        assert {"tool", "started_at", "deadline_at", "elapsed_ms", "phase"} <= spans[0].keys()
    finally:
        if (tmp_path / "executor.pid").exists():
            pid = int((tmp_path / "executor.pid").read_text())
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
        if pid is not None:
            status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True,
                                    text=True, timeout=3).stdout.strip()
            if status and not status.startswith("Z"):
                os.kill(pid, signal.SIGKILL)
                pytest.fail(f"executor survived: {pid}")


@pytest.mark.parametrize(("session_seconds", "requested_ms", "expected_ms"), [
    (120, None, 120000), (120, 0, 120000), (120, -1, 120000),
    (120, 25, 25), (120, 900000, 120000), (86400, 86400000, 600000),
])
def test_bash_deadline_selection(tmp_path, session_seconds, requested_ms, expected_ms):
    node = require_node()
    module = Path(__file__).resolve().parents[1] / "runners/dsh/src/exec.ts"
    script = tmp_path / "deadline.ts"
    script.write_text(f"""
import {{registerBash}} from {json.dumps(module.as_uri())};
let definition;
const record = {{id: 'session-control', builtin: ['bash'], cwd: process.cwd(), roots: [process.cwd()],
  limits: {{tool_timeout_s: {session_seconds}}},
  agentCtx: {{tools: {{register(tool) {{definition = tool;}}}}}}}};
registerBash(record, () => {{throw new Error('pre-aborted call spawned an executor');}});
const controller = new AbortController();
controller.abort();
try {{
  await definition.execute({{command: 'private command', timeoutMs: {json.dumps(requested_ms)}}},
    {{signal: controller.signal, callId: 'call-control'}});
  throw new Error('pre-aborted call succeeded');
}} catch (error) {{if (error.name !== 'AbortError') throw error;}}
""")
    result = subprocess.run([node, script], cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    span = json.loads(result.stderr.removeprefix("dsh.tool "))
    assert span["timeout_ms"] == expected_ms
