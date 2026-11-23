"""Lane counts are visible only while Manager lanes are enabled."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from looper.agents import lanes_settings, session_routing, status_card
from looper.agents.lanes import Lane
from looper.agents.tiers.manager_session import MAIN_SESSION, ManagerSession

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def _agent() -> SimpleNamespace:
    sessions = {key: ManagerSession(key=key, claude=SimpleNamespace(), claude_runtime=SimpleNamespace(),
                                    last_activity=0) for key in (MAIN_SESSION, "dm:bot:chat")}
    counts = {MAIN_SESSION: (2, 1), "dm:bot:chat": (0, 4)}

    def queued_count(session: ManagerSession, lane: Lane | None = None) -> int:
        interactive, background = counts[session.key]
        return (interactive if lane is Lane.INTERACTIVE else background if lane is Lane.BACKGROUND
                else interactive + background)

    agent = SimpleNamespace(agent_id="manager", name="Manager", _sessions=sessions,
                            _router=SimpleNamespace(queued_count=queued_count),
                            _bus=SimpleNamespace(pending_count=lambda _: 7), _handling=None)
    agent.activity = lambda: status_card.session_activity(agent)
    return agent


@pytest.mark.parametrize("routing", [False, True])
def test_lanes_off_preserves_session_json_and_status_text(monkeypatch: pytest.MonkeyPatch, routing: bool) -> None:
    # Fixed baseline: ecc2eedbf2ac4a1b44ff9078e634002c228458c1 (before lanes).
    monkeypatch.setattr(lanes_settings, "enabled", lambda: False)
    monkeypatch.setattr(session_routing, "routes", lambda _: routing)
    agent = _agent()
    section = asyncio.run(status_card._manager_section(status_card.SoloFleet(agent), NOW))
    main_count = 3 if routing else 10
    expected = [{"key": key, "state": "idle", "origin": None, "channel": None, "chat": None,
                 "group": False, "client_id": None, "started_at": None, "elapsed_seconds": None,
                 "queued": count} for key, count in ((MAIN_SESSION, main_count), ("dm:bot:chat", 4))]
    assert section == {"name": "manager", "state": "ok", "error": None,
                       "managers": [{"manager_id": "manager", "name": "Manager",
                                     "queued": main_count + 4, "sessions": expected}]}
    assert status_card._manager_text(section) == f"管家：空闲；排队 {main_count + 4} 条"


@pytest.mark.parametrize("routing", [False, True])
def test_lanes_on_reports_each_session_and_names_both_queues(monkeypatch: pytest.MonkeyPatch, routing: bool) -> None:
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    monkeypatch.setattr(session_routing, "routes", lambda _: routing)
    agent = _agent()
    section = asyncio.run(status_card._manager_section(status_card.SoloFleet(agent), NOW))
    manager = section["managers"][0]
    assert manager["queued"] == 7
    assert [(row["key"], row["queued"], row["queued_interactive"], row["queued_background"])
            for row in manager["sessions"]] == [(MAIN_SESSION, 3, 2, 1), ("dm:bot:chat", 4, 0, 4)]
    assert status_card._manager_text(section) == "管家：空闲；交互排队 2 条；后台排队 5 条"
