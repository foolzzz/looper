"""Typed limit windows are observations, never scheduling or failure decisions."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import limit_signals
from looper.db import system_settings

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
RESET = int((NOW + timedelta(hours=5)).timestamp())


def test_claude_windows_inherit_status_and_top_level_does_not_erase_utilization():
    event = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "rateLimitType": "seven_day", "resetsAt": RESET,
        "unifiedWindows": {"seven_day": {"utilization": .96, "resetsAt": RESET},
                           "seven_day_opus": {"status": "rejected", "utilization": 1.2}}}}
    windows = dict(limit_signals.parse(event, now=NOW))
    assert windows["seven_day"]["status"] == "warning"
    assert windows["seven_day"]["utilization"] == .96
    assert windows["seven_day"]["resets_at"].timestamp() == RESET
    assert windows["seven_day_opus"]["utilization"] == 1
    assert windows["seven_day_opus"]["status"] == "rejected"


def test_codex_sparse_windows_and_epoch_seconds():
    windows = dict(limit_signals.parse({"type": limit_signals.CODEX_EVENT, "rateLimits": {
        "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": RESET},
        "secondary": None}}, now=NOW))
    assert list(windows) == ["primary"]
    assert windows["primary"]["utilization"] == .25
    assert windows["primary"]["resets_at"].timestamp() == RESET
    assert windows["primary"]["window_minutes"] == 300
    sparse = dict(limit_signals.parse({"type": limit_signals.CODEX_EVENT, "rateLimits": {
        "secondary": {"usedPercent": 100}}}, now=NOW))
    assert sparse["secondary"]["status"] == "rejected"
    assert "resets_at" not in sparse["secondary"]


@pytest.mark.parametrize("info", [None, [], {"status": "future", "rateLimitType": "seven_day"},
    {"status": "allowed", "rateLimitType": "bad/name"},
    {"status": "allowed", "rateLimitType": "seven_day", "utilization": float("nan")}])
def test_malformed_claude_signals_are_ignored(info):
    assert limit_signals.parse({"type": "rate_limit_event", "rate_limit_info": info}, now=NOW) == []


@pytest.mark.parametrize("reset", [True, None, "invalid", 10**100, RESET * 1000,
                                  int(NOW.timestamp()) - 1, int((NOW + timedelta(days=9)).timestamp())])
def test_untrusted_reset_preserves_the_last_good_reset(reset):
    result = dict(limit_signals.parse({"type": limit_signals.CODEX_EVENT, "rateLimits": {
        "primary": {"usedPercent": 10, "resetsAt": reset}}}, now=NOW))
    assert "resets_at" not in result["primary"]


def test_off_does_not_parse_or_write_and_live_disable_takes_effect(monkeypatch):
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(side_effect=[{}, {limit_signals.KEY: "true"}, {}]))
    save = AsyncMock()
    monkeypatch.setattr(limit_signals.db_limits, "upsert", save)
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}}
    async def run():
        await limit_signals.observe(event, "account", 1)
        save.assert_not_awaited()
        await limit_signals.observe(event, "account", 1)
        assert save.await_count == 1
        await limit_signals.observe(event, "account", 1)
        assert save.await_count == 1
    asyncio.run(run())


def test_storage_failure_is_observational(monkeypatch, caplog):
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={limit_signals.KEY: "true"}))
    monkeypatch.setattr(limit_signals.db_limits, "upsert", AsyncMock(side_effect=RuntimeError("private payload")))
    asyncio.run(limit_signals.observe({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour"}}, "account", 1))
    assert "could not save provider limit signal" in caplog.text
    assert "private payload" not in caplog.text


def test_processor_filter_cannot_drop_observations(monkeypatch):
    from looper.agents.claude_cli import ClaudeRunRecorder
    from looper.agents.processor import typed_events
    from tests.runtime_model_helpers import builtin_launch
    from looper.db import llm_records
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={limit_signals.KEY: "true"}))
    saved = AsyncMock()
    monkeypatch.setattr(limit_signals.db_limits, "upsert", saved)
    monkeypatch.setattr(llm_records, "touch_record", AsyncMock())
    appended = AsyncMock()
    monkeypatch.setattr(llm_records, "append_events", appended)
    recorder = ClaudeRunRecorder(agent_id="processor:synthetic", task_id=None, command=["claude"],
        cwd="/workspace", input_text="synthetic", launch=builtin_launch(), keep=typed_events)
    recorder._record_id = 1
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}}
    assert typed_events(event) is None
    asyncio.run(recorder.record(event))
    assert saved.await_args.args[:2] == ("prov-claude-subscription", "five_hour")
    appended.assert_not_awaited()


def test_codex_internal_event_is_not_transcript_content(monkeypatch):
    from looper.agents.claude_cli import ClaudeRunRecorder
    from tests.runtime_model_helpers import builtin_launch
    from looper.db import llm_records
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={limit_signals.KEY: "true"}))
    saved = AsyncMock()
    monkeypatch.setattr(limit_signals.db_limits, "upsert", saved)
    appended = AsyncMock()
    monkeypatch.setattr(llm_records, "append_events", appended)
    recorder = ClaudeRunRecorder(agent_id="worker:synthetic", task_id=None, command=["codex"],
        cwd="/workspace", input_text="synthetic", launch=builtin_launch())
    recorder._record_id = 1
    async def run():
        event = {"type": limit_signals.CODEX_EVENT, "rateLimits": {"primary": {"usedPercent": 40}}}
        await recorder.record(event)
    asyncio.run(run())
    assert saved.await_count == 1
    appended.assert_not_awaited()


def test_verified_protocol_fixtures_and_sparse_null_preservation():
    import json
    from pathlib import Path
    root = Path(__file__).parent / "fixtures/limit_signals"
    claude = dict(limit_signals.parse(json.loads((root / "claude_windows.json").read_text()), now=NOW))
    assert set(claude) == {"five_hour", "seven_day"}
    assert claude["seven_day"]["status"] == "warning"
    full = json.loads((root / "codex_windows.json").read_text())
    assert len(limit_signals.parse({"type": limit_signals.CODEX_EVENT, **full["params"]}, now=NOW)) == 2
    sparse = json.loads((root / "codex_sparse.json").read_text())
    patch = dict(limit_signals.parse({"type": limit_signals.CODEX_EVENT, **sparse["params"]}, now=NOW))
    assert list(patch) == ["primary"]
    assert patch["primary"]["utilization"] == .43
    assert "resets_at" not in patch["primary"] and "window_minutes" not in patch["primary"]


def test_claude_top_status_does_not_reject_an_unrelated_window():
    windows = dict(limit_signals.parse({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "seven_day", "unifiedWindows": {
            "seven_day": {"utilization": 1}, "five_hour": {"utilization": .1}}}}, now=NOW))
    assert windows["seven_day"]["status"] == "rejected"
    assert windows["five_hour"]["status"] == "allowed"


@pytest.mark.parametrize("error", [ConnectionRefusedError, TimeoutError, OSError])
def test_provider_limit_doctor_probe_tolerates_connection_failure(monkeypatch, error):
    import asyncpg
    from looper.agents import self_check

    monkeypatch.setattr(asyncpg, "connect", AsyncMock(side_effect=error("unavailable")))
    assert asyncio.run(self_check.provider_limit_checks_for_database("postgresql://unused")) == []


def test_provider_limit_doctor_probe_tolerates_invalid_password(monkeypatch):
    import asyncpg
    from looper.agents import self_check

    monkeypatch.setattr(asyncpg, "connect", AsyncMock(side_effect=asyncpg.InvalidPasswordError("private")))
    assert asyncio.run(self_check.provider_limit_checks_for_database("postgresql://unused")) == []
