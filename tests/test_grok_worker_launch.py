"""A Grok Worker and Reviewer run through the Worker tier's own launch path (``WorkerPool._execute``), with the
stand-in ``grok`` of ``test_grok_runtime`` and no mocked ``run_command``."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.tiers import worker as worker_module
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from test_agent_planner_worker import _step_envelope, _worker_row
from test_grok_runtime import _FAKE_GROK, _launch, _operator


@pytest.mark.parametrize("verify", [False, True])
def test_a_grok_worker_and_reviewer_launch_through_the_worker_tier(
    verify: bool, agent_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 0, "max_turns": 0})
    grok = fake_claude(tmp_path / "grok", _FAKE_GROK)
    settings = _launch(executable=grok).settings

    async def load(*args: Any, **kwargs: Any) -> Any:
        return settings

    monkeypatch.setattr(worker_module, "load_agent_settings", load)

    async def _run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as work:
            pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path / "runtime")
            return await pool._execute(_step_envelope(Path(work), verify=verify), is_verify=verify)

    result = asyncio.run(_run())
    assert "session_dir" not in json.dumps(result, default=str)
    runs = (tmp_path / "runtime" / ".local" / "grok" / "argv.jsonl").read_text().splitlines()
    assert len(runs) == 1  # the stand-in ran: the launch got past building its command

