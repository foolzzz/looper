"""The typed pieces of a triage turn: its origin, its metrics record and its delivery source."""

from datetime import datetime, timezone

from looper.agents import delivery_guard, proactive_delivery as delivery
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.turn_metrics import ProactiveSource, TurnDelivery, TurnTimes


def test_a_triage_turn_is_autonomous_and_owes_the_user_no_delivery():
    turn = Turn(channel="lark", chat="ou_synthetic", origin=TurnOrigin.TRIAGE)
    assert turn.autonomous
    assert delivery_guard.initial_debt(enabled=True, origin=TurnOrigin.TRIAGE) is delivery_guard.DeliveryDebt.EXEMPT
    assert delivery_guard.initial_debt(enabled=True, origin=TurnOrigin.USER) is delivery_guard.DeliveryDebt.OWED


def test_a_triage_turn_records_the_short_background_metadata_a_completion_turn_does():
    times = TurnTimes(received_at=datetime.now(timezone.utc), tool_calls=2)
    for origin in (TurnOrigin.COMPLETION, TurnOrigin.TRIAGE):
        assert times.to_metadata(next(iter(TurnDelivery)), origin, False) == {
            "origin": origin.value, "tool_calls": 2, "delivery": next(iter(TurnDelivery)).value}


def test_triage_is_a_proactive_source_the_delivery_layer_knows():
    assert ProactiveSource("triage") is ProactiveSource.TRIAGE is delivery.DeliveryKind.TRIAGE
