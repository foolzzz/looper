"""A Manager a test leaves behind never reaches the next test's resident pool or metrics."""

from __future__ import annotations

from pathlib import Path

import pytest

from looper.agents import chat_scope_metrics
from looper.agents.tiers import manager_routing
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.conftest import own_resident_pool


def test_a_manager_with_a_stand_in_session_does_not_reach_the_next_resident_pool(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    manager._sessions["main"].claude = object()  # no `started`, like the stand-ins other tests install
    assert list(manager_routing.pool.routers)
    # The manager stays alive (this local), as one an earlier test left behind does until the garbage collector
    # frees it. The next test's fixture swaps in its own pool; no ordering between tests is assumed.
    own_resident_pool(monkeypatch)
    assert not list(manager_routing.pool.routers)
    assert chat_scope_metrics.residents()["total"] == 0
    assert manager._sessions["main"].claude is not None  # the old Manager was alive throughout
