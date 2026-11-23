"""Replay synthetic Claude quota events through an isolated real Looper server.

From a source checkout:
  LOOPER_TEST_DATABASE_URL=<disposable PostgreSQL URL with CREATEDB> \
    uv run --extra test python docs/validation/provider-limit-signals-smoke.py

The supplied URL is used only to create/drop a new database. This script
uses a temporary HOME/runtime root and a scripted CLI, never a real model.
Only an aggregate report is printed; logs, credentials and runtime files
stay in the temporary directory and are removed on exit.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.prompt_regression.runner import fresh_database
from tests.test_llm_registry_runtime_smoke import query, server


def set_collection(api, value):
    answer = api.post("/api/v1/limit_signals_settings/update", {"enabled": value})
    if answer.get("status") == "needs_confirmation":
        answer = api.post("/api/v1/confirm", {"confirmation_id": answer["confirmation_id"]})
    assert answer["result"]["enabled"] is value, answer


def run_turn(api, url):
    before = query(url, "SELECT coalesce(max(id),0) AS id FROM llm_records")[0]["id"]
    api.post("/api/v1/operator_chat/send", {
        "text": "Synthetic quota smoke: reply without tools.",
        "session_id": "quota-smoke", "message_id": "smoke-" + uuid.uuid4().hex,
    })
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        rows = query(url, "SELECT id,status,input_tokens,output_tokens FROM llm_records "
                         "WHERE id>$1 AND agent_id='manager' ORDER BY id", before)
        if rows and rows[-1]["status"] != "running":
            return rows[-1]
        time.sleep(0.2)
    raise AssertionError("scripted Manager run did not finish within 30 seconds")


def main():
    admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin:
        raise SystemExit("Set LOOPER_TEST_DATABASE_URL to a disposable server with CREATEDB")
    import looper
    assert Path(looper.__file__).resolve() == ROOT / "looper/__init__.py"
    report = {"runtime": "scripted Claude", "model_requests": 0}
    with tempfile.TemporaryDirectory(prefix="looper-quota-smoke-", dir="/tmp") as directory:
        case = Path(directory)
        with fresh_database(admin) as (url, _), server(case, url) as running:
            # The existing server scaffolding exposes only this scripted CLI.
            # Replace it before launching a Manager; no personal executable or
            # credential enters the child environment.
            script = case / "bin" / "claude"
            script.write_text(f"#!{sys.executable}\n" + """
import json, sys, time
from pathlib import Path
from evals.prompt_regression import fake_claude as cli
emit = cli._emit
count = 0
def measured(event):
    global count
    if event.get('type') == 'result':
        count += 1
        emit({'type': 'rate_limit_event', 'rate_limit_info': {
            'status': 'allowed_warning', 'rateLimitType': 'seven_day',
            'utilization': .91 if count == 1 else .97,
            'resetsAt': int(time.time()) + 3600}})
        event['modelUsage'] = {'fake': {'inputTokens': count * 10,
            'outputTokens': count * 5, 'cacheReadInputTokens': count * 2,
            'cacheCreationInputTokens': count}}
        event['total_cost_usd'] = count * .01
        event.update(is_error=True, subtype='error_during_execution',
            api_error='usage_limit_reached', api_error_status=429,
            api_error_type='rate_limit_error', terminal_reason='api_error')
    emit(event)
cli._emit = measured
""" + f"sys.exit(cli.main([{str(case / 'scenarios.json')!r}, *sys.argv[1:]]))\n")
            script.chmod(0o755)
            api = running.api
            api.post("/api/v1/agent_settings/update_role", {
                "role": "manager", "values": {"profile_id": "agent-claude-code-opus"}})
            initial = api.post("/api/v1/providers/limits", {"provider_id": "prov-claude-subscription"})["result"]
            assert initial["enabled"] is False
            assert initial["providers"][0]["windows"] == []
            set_collection(api, True)
            first = run_turn(api, url)
            observed = api.post("/api/v1/providers/limits", {"provider_id": "prov-claude-subscription"})["result"]
            provider = observed["providers"][0]
            assert len(provider["windows"]) == 1, observed
            window = provider["windows"][0]
            assert window["window_name"] == "seven_day" and window["utilization"] == .91
            assert window["record_id"] == first["id"]
            assert provider["usage"][0]["runs"] == 1
            assert provider["usage"][0]["input_tokens"] == 10
            assert provider["usage"][0]["output_tokens"] == 5
            assert provider["cost_basis"] == "api_equivalent"
            saved = query(url, "SELECT * FROM provider_limit_windows")
            set_collection(api, False)
            second = run_turn(api, url)
            assert second["id"] > first["id"]
            assert query(url, "SELECT * FROM provider_limit_windows") == saved
            disabled = api.post("/api/v1/providers/limits", {"provider_id": "prov-claude-subscription"})["result"]
            assert disabled["enabled"] is False
            assert disabled["providers"][0]["windows"] == []
            set_collection(api, True)
            readback = api.post("/api/v1/providers/limits", {"provider_id": "prov-claude-subscription"})["result"]
            assert readback["providers"][0]["windows"] == provider["windows"]
            assert readback["providers"][0]["usage"][0]["runs"] == 2
            report.update(window_persisted=True, http_usage_readback=True,
                disabled_no_write=True, reenabled_snapshot_preserved=True,
                scripted_429_runs=2, admission_tested=False)
    report["temporary_server_stopped_and_database_dropped"] = True
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
