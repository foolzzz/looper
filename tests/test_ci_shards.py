"""Exercise PR sharding through real pytest collection, without running the suite."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PR_MARKERS = "not installed and not provenance"


@pytest.fixture
def collection(tmp_path: Path):
    # A plugin records the final items, avoiding terminal formatting and warning output.
    (tmp_path / "collection_ids.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "def pytest_collection_finish(session):\n"
        "    Path(os.environ['COLLECTION_IDS']).write_text(\n"
        "        json.dumps([item.nodeid for item in session.items]))\n",
        encoding="utf-8",
    )

    def collect(shard: str | None, *paths: str) -> list[str]:
        env = os.environ.copy()
        # Collection needs no database, even when this test runs inside a CI worker.
        for key in ("LOOPER_TEST_DATABASE_URL", "LOOPER_TEST_SHARD", "PYTEST_ADDOPTS"):
            env.pop(key, None)
        if shard is not None:
            env["LOOPER_TEST_SHARD"] = shard
        output = tmp_path / "nodeids.json"
        output.unlink(missing_ok=True)
        env["COLLECTION_IDS"] = str(output)
        env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")])
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", PR_MARKERS,
             "-p", "collection_ids", *paths],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(output.read_text(encoding="utf-8"))

    return collect


def test_shards_partition_pr_collection(collection) -> None:
    baseline = collection(None)
    assert len(baseline) == len(set(baseline))
    assert any(node.startswith("tests/test_prompt_eval.py::") for node in baseline)
    combined: set[str] = set()
    file_owners: dict[str, int] = {}
    counts = []
    for shard in range(1, 5):
        nodes = collection(f"{shard}/4")
        assert nodes, f"shard {shard} is empty"
        assert len(nodes) == len(set(nodes))
        assert combined.isdisjoint(nodes), f"shard {shard} overlaps another shard"
        combined.update(nodes)
        counts.append(len(nodes))
        for node in nodes:
            path = node.split("::", 1)[0]
            assert file_owners.setdefault(path, shard) == shard, f"{path} is split across shards"
    assert combined == set(baseline)
    print(f"PR collection: {len(baseline)} tests; shard counts: {counts}")


def test_single_shard_preserves_unsharded_collection(collection) -> None:
    path = "tests/test_ci_shards.py"
    assert collection("1/1", path) == collection(None, path)


@pytest.mark.parametrize("shard", ["2", "0/4", "5/4", "1/0", ""])
def test_invalid_shard_fails_at_configuration(shard: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_ci_shards.py"],
        cwd=ROOT, env={**os.environ, "LOOPER_TEST_SHARD": shard},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "LOOPER_TEST_SHARD must be N/TOTAL" in result.stderr
