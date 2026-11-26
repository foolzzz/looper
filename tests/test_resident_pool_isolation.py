"""A Manager a test leaves behind never reaches the next test's resident pool or metrics."""

from __future__ import annotations

from pathlib import Path

from looper.agents import chat_scope_metrics
from looper.agents.tiers import manager_routing
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

_KEPT: list[ManagerAgent] = []  # held on purpose: the garbage collector cannot clean the pool up in between


def test_a_manager_with_a_stand_in_session_is_left_behind(tmp_path: Path) -> None:
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    manager._sessions["main"].claude = object()  # no `started`, like the stand-ins other tests install
    _KEPT.append(manager)
    assert list(manager_routing.pool.routers)


def test_the_next_test_starts_with_an_empty_pool_and_readable_metrics() -> None:
    assert _KEPT, "runs after the test above in the same process"
    assert not list(manager_routing.pool.routers)
    assert chat_scope_metrics.residents()["total"] == 0
