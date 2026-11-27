"""Opt-in, offline real-CLI probes of the task Manager's network allowlist and the denial format it reports.

Run with ``LOOPER_CLAUDE_PROBE_BIN`` set to a real Claude Code binary (never a terminal shim) and ``--basetemp`` outside
``/tmp``; no real model is called: the CLI talks to a local scripted Messages API with a scratch ``HOME`` and
``CLAUDE_CONFIG_DIR``, and the only hosts are ``.invalid`` names and a loopback listener. The command under test is the
real ``CLAUDE_CODE.operator_resident_command``. A skip is not a pass. Verified against Claude Code 2.1.332.

- D1 a denied host: the proxy answers 403 and the result ends in a ``<sandbox_violations>`` block; ``parse`` and
  ``classify`` read it as ``not_allowed`` for that host.
- D2 a probe-only list (built without operator validation) naming the loopback listener lets a proxied request through
  and nothing else.
- D3 a denied write: recorded as observed; the CLI reports no violation block for it, so ``parse`` yields nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from looper.agents.task_manager_denials import Case, Category, classify, parse
from sandbox_probe_support import SandboxProbe, text, tool_use

pytestmark = pytest.mark.installed


@pytest.fixture
def probe(tmp_path, request):
    binary = os.environ.get("LOOPER_CLAUDE_PROBE_BIN")
    if not binary:
        pytest.skip("Set LOOPER_CLAUDE_PROBE_BIN to opt into the offline real-CLI probe; a skip is not a pass")
    assert not str(tmp_path.resolve()).startswith(("/private/tmp/", "/tmp/")), "use a --basetemp outside /tmp"
    instance = SandboxProbe(tmp_path, str(Path(binary).resolve()))
    try:
        yield instance
    finally:
        instance.close()
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps({"check": request.node.name, **instance.evidence()}, indent=2))
        print(f"Evidence: {evidence}")


def bash(command: str) -> dict:
    return tool_use("Bash", {"command": command})


def run_one(probe, shell: str, **kwargs) -> str:
    script = [bash(shell), text("done")]
    run = probe.run(script, **kwargs)
    return run.result_of(script[0]["id"])


def test_d1_a_denied_host_is_reported_and_read_as_not_allowed(probe):
    command = 'curl -sS -x "$HTTPS_PROXY" http://blocked.invalid/; echo rc=$?'
    result = run_one(probe, command)
    assert "<sandbox_violations>" in result and result.rstrip().endswith("</sandbox_violations>"), result
    assert "deny network-outbound blocked.invalid:80 (host is not on the allow list)" in result
    violations = parse(result)
    assert [(v.category, v.target) for v in violations] == [(Category.NETWORK, "blocked.invalid:80")]
    [denial] = classify(violations, command=command, output=result, allowed=())
    assert (denial.category, denial.case, denial.target, denial.command_name) == (
        Category.NETWORK, Case.NOT_ALLOWED, "blocked.invalid:80", "curl")
    https = run_one(probe, "curl -sS https://blocked.invalid/; echo rc=$?")
    assert "CONNECT tunnel failed, response 403" in https
    assert [v.target for v in parse(https)] == ["blocked.invalid:443"]
    assert probe.listener.count == 0
    probe.observations["d1"] = {"http": result, "https": https}


def test_d1_a_host_on_the_list_is_no_longer_reported_as_denied(probe):
    command = 'curl -sS -x "$HTTPS_PROXY" http://blocked.invalid/'
    [violation] = parse(run_one(probe, command))
    assert violation.target == "blocked.invalid:80"
    # The same denial read against a list that names the host is not "not on the allowlist".
    [denial] = classify([violation], command=command, output="", allowed=("blocked.invalid",))
    assert denial.case is Case.UNKNOWN and denial.client is None


def test_d2_a_probe_only_list_with_the_listener_lets_exactly_that_through(probe):
    port = probe.listener.port
    allowed = (f"127.0.0.1:{port}",)
    command = probe.command(allowed_domains=allowed)
    assert probe.settings_file(command)["sandbox"]["network"]["allowedDomains"] == list(allowed)
    base = 'env -u NO_PROXY -u no_proxy curl -sS -x "$HTTPS_PROXY" --max-time 8'
    first = run_one(probe, f"{base} http://127.0.0.1:{port}/; echo; echo rc=$?", command=command)
    other = run_one(probe, f"{base} http://127.0.0.1:{port + 1}/; echo; echo rc=$?", command=command)
    probe.observations["d2"] = {"listener_connections": probe.listener.count, "allowed": first, "other": other}
    assert "listener-ok" in first and probe.listener.count >= 1, first
    assert "listener-ok" not in other and [v.target for v in parse(other)] == [f"127.0.0.1:{port + 1}"], other


def test_d3_a_denied_write_is_refused_and_the_cli_reports_no_violation_block(probe):
    target = probe.outside / "x"
    result = run_one(probe, f"touch {target}; echo rc=$?")
    probe.observations["d3"] = {"result": result}
    assert not target.exists() and "rc=1" in result, result
    assert parse(result) == []
