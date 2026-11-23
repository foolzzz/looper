"""Quota observations are read-only and available only to the operator."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1, limit_signals
from looper.agents.ops import OpError
from looper.agents.v1_domains import provider_limits


def test_limits_operation_and_generated_cli():
    op = api_v1.find("POST", "/api/v1/providers/limits")
    assert op.operation_id == "providers.limits"
    assert op.risk == "read" and op.operator_only and not op.confirms and op.tool is None
    assert len(op.summary) <= 120
    assert op.description.startswith(op.summary)
    args = loopercli.build_parser(api_v1.document()).parse_args(["providers", "limits", "--provider-id", "sample"])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="synthetic", operator=True))
    assert calls == [("POST", "/api/v1/providers/limits", {"provider_id": "sample"})]
    with pytest.raises(OpError):
        api_v1.validate_arguments(op.request, {"unexpected": True})


def test_limits_readback_and_missing_provider(monkeypatch):
    monkeypatch.setattr(limit_signals.profile_registry, "list_providers", AsyncMock(return_value=[
        {"id": "sample", "name": "Sample", "auth": "oauth"}]))
    monkeypatch.setattr(limit_signals, "enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(limit_signals.db_limits, "windows", AsyncMock(return_value=[]))
    monkeypatch.setattr(limit_signals.db_limits, "usage", AsyncMock(return_value=[]))
    result = asyncio.run(limit_signals.limits("sample"))
    assert result["providers"][0]["cost_basis"] == "api_equivalent"
    api_v1.validate_arguments(provider_limits.LIMITS, result)
    with pytest.raises(LookupError):
        asyncio.run(limit_signals.limits("absent"))


def test_settings_validate_before_write_and_require_confirmation(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(limit_signals.system_settings, "put_settings", save)
    for value in (0, "true", []):
        with pytest.raises(ValueError):
            asyncio.run(limit_signals.update_settings({"enabled": value}))
    save.assert_not_awaited()
    op = api_v1.find("POST", "/api/v1/limit_signals_settings/update")
    assert op.operator_only and op.confirms and op.risk == "high"
