"""Credential diagnostics stay safe before they reach any attention surface."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from looper.agents import attention, self_check
from looper.agents.http_server import AgentHTTPServer
from looper.agents.message import channel_address
from tests.db_support import run_db

CASES = json.loads((Path(__file__).parent / "fixtures" / "attention-redaction.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_self_check_masks_detail_and_fix_before_banner_serialization(case):
    check = self_check.Check("claude_login", "Synthetic login", "fail", case["input"], case["input"])
    assert check.detail == check.fix == case["expected"]
    assert check.to_dict()["detail"] == check.to_dict()["fix"] == case["expected"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_chat_summary_masks_raw_attention_facts(case):
    items = attention._login_items([{"id": "claude_login", "status": "fail", "label": "Synthetic login",
                                     "detail": case["input"], "fix": case["input"]}])
    text = attention.summary_text({"read_at": datetime.now(timezone.utc), "total": 1, "sections": [
        {"kind": "logins", "state": "ok", "count": 1, "items": items, "error": None}]})
    assert f"Synthetic login：{case['expected']}。修复：{case['expected']}" in text
    for secret in case["secrets"]:
        assert secret not in text


@pytest.mark.parametrize("surface", ["attention/summary", "system/status"])
def test_http_attention_and_startup_banner_receive_safe_facts(monkeypatch, tmp_path, surface):
    async def check(pool):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('attention.enabled', 'true')")
        refresh = AsyncMock()
        monkeypatch.setattr(self_check, "refresh_claude", refresh)
        server = AgentHTTPServer(port=0, runtime_root=tmp_path, static_dir=tmp_path,
            operator_token="synthetic-operator-token", task_router=SimpleNamespace(),
            supervisor=SimpleNamespace(agent_status=lambda: []),
            web_adapter=SimpleNamespace(_address=channel_address("web"), healthcheck=lambda: {}))
        await server.start()
        try:
            assert server._port != 8767
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server._port}",
                                         limits=httpx.Limits(max_keepalive_connections=0)) as client:
                for case in CASES:
                    diagnostic = self_check.Check("claude_login", "Synthetic login", "fail",
                                                  case["input"], case["input"])
                    # Startup snapshot, then the same raw CLI diagnostic on refresh.
                    server._self_checks = [diagnostic.to_dict()]
                    server._check_results = {}
                    for phase in ("startup", "refresh"):
                        refresh.return_value = [] if phase == "startup" else [diagnostic]
                        response = await client.post(f"/api/v1/{surface}", json={},
                            headers={"Authorization": "Bearer synthetic-operator-token"})
                        assert response.status_code == 200, response.text
                        result = response.json()["result"]
                        facts = ([item["facts"] for section in result["sections"] if section["kind"] == "logins"
                                  for item in section["items"]] if surface == "attention/summary" else result["checks"])
                        fact, = facts
                        assert fact["detail"] == fact["fix"] == case["expected"], case["name"]
        finally:
            await server.stop()

    run_db(check, monkeypatch)
