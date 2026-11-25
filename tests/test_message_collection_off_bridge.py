"""Frozen per-role, per-tool and per-operation contracts from the recorded base."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from tests.message_collection_freeze_support import BASELINE, snapshot, database_snapshot
from tests.db_support import DSN, run_db

FIXTURE = Path(__file__).parent / "fixtures/message_collection_off_bridge.json"


@pytest.mark.parametrize("mode", ["no_source", "off", "bridge"])
def test_existing_surface_matches_fixed_revision_per_item(mode):
    # Source presence never enters these constructors; the DB companion exercises actual source ticks.
    expected = json.loads(FIXTURE.read_text())
    assert expected["baseline_revision"] == BASELINE
    actual = snapshot()
    assert actual.keys() == expected["items"].keys()
    for name, digest in actual.items():
        assert digest == expected["items"][name], (mode, name)


def test_off_bridge_fixture_provenance_is_pinned():
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["baseline_revision"] == BASELINE
    keys = fixture["items"]
    for role in ("manager", "planner", "worker", "reviewer", "processor"):
        assert all(f"{kind}/{role}" in keys for kind in ("prompt", "contract", "argv", "process"))
    assert "operation/message_stream.configure/request" in keys
    assert all(len(value) == 64 for value in keys.values())
    assert not any(name.endswith("module") for name in keys)


@pytest.mark.provenance
@pytest.mark.skipif(not DSN, reason="provenance regeneration requires disposable PostgreSQL")
def test_fixed_revision_reproduces_each_recorded_item(tmp_path):
    archive = tmp_path / "baseline.tar"
    root = tmp_path / "baseline"
    root.mkdir()
    with archive.open("wb") as output:
        subprocess.run(["git", "archive", BASELINE], stdout=output, check=True)
    with tarfile.open(archive) as source:
        source.extractall(root, filter="data")
    script = Path(__file__).parent / "message_collection_freeze_support.py"
    # Run the exact same capture script, importing only the immutable archived checkout.
    code = ("import sys,runpy;sys.path.insert(0,sys.argv[1]);"
            "sys.argv=[sys.argv[2], '--database'];runpy.run_path(sys.argv[0],run_name='__main__')")
    result = subprocess.run([sys.executable, "-c", code, str(root), str(script)], cwd=root,
                            env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == json.loads(FIXTURE.read_text())


@pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
@pytest.mark.parametrize("mode", ["no_source", "off", "bridge"])
def test_existing_database_and_outputs_are_frozen_without_collection_side_effects(monkeypatch, tmp_path, mode):
    from looper.agents.source_service import SourceService
    from tests import msgflow_support as flow

    async def check(pool):
        actual = await database_snapshot(pool, tmp_path, mode)
        expected = json.loads(FIXTURE.read_text())["database_items"][mode]
        assert actual.keys() == expected.keys()
        for name, digest in actual.items():
            assert digest == expected[name], (mode, name)

        backend_calls = []

        class ForbiddenBackend:
            def __getattr__(self, name):
                backend_calls.append(name)
                raise RuntimeError("default source attempted user CLI: " + name)

        service = SourceService(collector_backend=ForbiddenBackend())
        await service.run_once(clock=lambda: flow.NOW)
        assert service._collect_tasks == {}
        for table in ("source_collection_settings", "source_parity_sessions", "source_parity_members",
                      "source_parity_coverage", "source_stream_alerts"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
        await service.stop()
        assert backend_calls == []

    run_db(check, monkeypatch, timeout=30)
