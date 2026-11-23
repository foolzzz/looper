"""Usage is read-only: what a Worker is launched with, and what it records, is a fixed recorded baseline.

``fixtures/skill_usage/worker_launch.json`` holds the stand-in ``claude``'s full argv, environment changes, appended
system prompt and first user turn for a Worker of a project with one project skill, and the ``library`` record of the
run, as the code produced them before usage existed. Regenerate with ``UPDATE_SKILL_USAGE_BASELINE=1`` only on a
checkout whose run side is the baseline.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db

BASELINE = Path(__file__).parent / "fixtures" / "skill_usage" / "worker_launch.json"
AGENTS = Path(__file__).resolve().parents[1] / "looper" / "agents"


def test_no_run_side_module_imports_the_usage_query() -> None:
    importers = []
    for path in AGENTS.rglob("*.py"):
        if path.name == "skill_usage.py" or "v1_domains" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
            if "skill_usage" in names or "skill_usage" in (getattr(node, "module", None) or ""):
                importers.append(path.name)
    assert importers == []


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_a_worker_with_a_project_skill_is_launched_and_recorded_as_the_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import agent_launch_parity as parity

    from looper.agents import project_skills

    root = tmp_path.resolve()
    (root / "home").mkdir()
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setitem(parity.SCENARIOS, "skill", {"cases": [f"worker@{parity.PROJECT_A}"]})

    async def _test(pool: Any) -> None:
        await parity.seed_projects(pool)
        await project_skills.add_skill(parity.PROJECT_A, {
            "name": "deploy-check", "description": "Use for deploy-check.", "content": "Reply deploy-check.\n"})
        await parity.seed_rest("skill", root)
        seen = (await parity.capture(root, "skill"))[f"worker@{parity.PROJECT_A}"]
        record = json.loads(await pool.fetchval(
            "SELECT metadata->'library' FROM llm_records WHERE agent_id LIKE 'worker-%' ORDER BY id DESC LIMIT 1"))
        actual = {"launch": seen, "library": record}
        if os.environ.get("UPDATE_SKILL_USAGE_BASELINE") == "1":
            BASELINE.parent.mkdir(parents=True, exist_ok=True)
            BASELINE.write_text(json.dumps(actual, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        expected = json.loads(BASELINE.read_text())
        assert any("--plugin-dir" == arg for arg in seen["argv"])  # the skill really loaded
        assert actual == expected

    run_db(_test, monkeypatch, timeout=120)
