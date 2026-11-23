"""Rule boundaries and the alert switch's default-off behavior."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import metrics_alerts as alerts
from looper.agents import api_v1
from looper.agents.action_levels import ActionLevel


def ratio(value, n=20, upper=None):
    return {"n": n, "value": value, "wilson": [max(0, value - .1), upper if upper is not None else value + .1]}


@pytest.mark.parametrize("rule", [alerts.success_drop, alerts.first_pass_drop])
@pytest.mark.parametrize("n,drop,upper,judged,fired,tier", [
    (19, .3, .6, False, False, 0), (20, .1, .7, True, True, 1),
    (20, .099, .7, True, False, 0), (20, .2, .6, True, True, 2),
    (20, .3, .5, True, True, 3), (20, .2, .9, True, False, 0),
])
def test_drop_boundaries(rule, n, drop, upper, judged, fired, tier):
    result = rule(ratio(.9 - drop, n, upper), ratio(.9))
    assert (result.judged, result.fired, result.tier) == (judged, fired, tier)
    assert not rule(ratio(.5), ratio(.9, 19)).judged


@pytest.mark.parametrize("current,previous,fired,tier", [
    (1.5, 1, False, 0), (1.51, 1, True, 1), (2, 1, True, 2), (3, 1, True, 3),
    (1, 0, True, 3), (0, 0, False, 0),
])
def test_cost_bases_are_judged_separately(current, previous, fired, tier):
    result = alerts.step_cost_rise(
        {"provider_priced": {"n": 19, "median": 200}, "equivalent": {"n": 20, "median": current}},
        {"provider_priced": {"n": 20, "median": 1}, "equivalent": {"n": 20, "median": previous}})
    assert (result.judged, result.fired, result.tier) == (True, fired, tier)
    assert result.values[0].basis == "equivalent"


@pytest.mark.parametrize("n,previous,count,judged,fired,tier", [
    (19, 0, 3, False, False, 0), (20, 0, 2, True, False, 0),
    (20, 4, 3, True, True, 1), (20, 8, 4, True, False, 0),
    (20, 4, 4, True, True, 2), (20, 4, 8, True, True, 3),
])
def test_missed_boundaries(n, previous, count, judged, fired, tier):
    result = alerts.missed_increase({"n": n, "count": count}, {"n": 20, "count": previous})
    assert (result.judged, result.fired, result.tier) == (judged, fired, tier)
    assert not alerts.missed_increase({"n": 20, "count": 3}, {"n": 19, "count": 0}).judged


@pytest.mark.parametrize("count", [0, 1])
def test_missing_attempts_has_no_sample_floor(count):
    result = alerts.missing_attempts(count, 0)
    assert result.judged and result.fired == bool(count)


def test_off_tick_does_not_access_database_compute_metrics_or_notify(monkeypatch):
    monkeypatch.setattr(alerts, "_enabled", False)
    pool = Mock()
    get_pool = Mock(return_value=pool)
    store = AsyncMock(spec=alerts.system_settings)
    metric_reads = AsyncMock(spec=alerts.db_metrics)
    compute = AsyncMock()
    monkeypatch.setattr(alerts.connection, "get_pool", get_pool)
    monkeypatch.setattr(alerts, "system_settings", store)
    monkeypatch.setattr(alerts, "db_metrics", metric_reads)
    monkeypatch.setattr(alerts.metrics, "compute", compute)
    sender = AsyncMock()
    asyncio.run(alerts.MetricsAlertService(sender).run_once())
    # Assert calls after the tick: its exception handler must not hide an off-path side effect.
    get_pool.assert_not_called()
    assert pool.mock_calls == []
    assert store.mock_calls == []
    assert metric_reads.mock_calls == []
    compute.assert_not_called()
    sender.notify.assert_not_called()


def test_operator_only_operations_and_impact():
    for name in ("get", "update"):
        operation = api_v1.find("POST", f"/api/v1/metrics_alerts/{name}")
        assert operation.operator_only and operation.tool is None and operation.level is ActionLevel.INSIDE
        assert operation.description.startswith(operation.summary) and len(operation.summary) <= 120
    assert operation.risk == "high" and operation.confirms
    text = asyncio.run(api_v1.domain_for(operation).impact(None, operation, {"enabled": True}, None, {}))
    assert "daily" in text and "quiet hours" in text and "alert chat" in text
