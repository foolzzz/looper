"""Exercise PR sharding through real pytest collection, without running the suite."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
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
    for shard in range(1, 7):
        nodes = collection(f"{shard}/6")
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


@pytest.mark.parametrize("shard", ["2", "0/6", "7/6", "1/0", ""])
def test_invalid_shard_fails_at_configuration(shard: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_ci_shards.py"],
        cwd=ROOT, env={**os.environ, "LOOPER_TEST_SHARD": shard},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "LOOPER_TEST_SHARD must be N/TOTAL" in result.stderr


def test_workflow_runs_six_shards_behind_stable_required_check() -> None:
    workflow = (ROOT / ".github/workflows/python-tests.yml").read_text(encoding="utf-8")
    shards = workflow.split("  agent-system:\n", 1)[1].split("\n  bridge-schema-pg18:", 1)[0]
    assert "shard: [1, 2, 3, 4, 5, 6]" in shards
    assert "name: Agent system tests (shard ${{ matrix.shard }}/6)" in shards
    assert "LOOPER_TEST_SHARD: ${{ matrix.shard }}/6" in shards
    assert "timeout-minutes: 20" in shards
    assert "fail-fast: false" in shards
    result = workflow.split("  agent-system-result:\n", 1)[1].split("\n  dashboard:", 1)[0]
    assert re.search(r"^    name: Agent system tests$", result, re.MULTILINE)
    assert "needs: [agent-system, bridge-schema-pg18]" in result
    assert "if: always()" in result
    assert "SHARDS_RESULT: ${{ needs.agent-system.result }}" in result
    assert 'test "$SHARDS_RESULT" = success' in result
    assert 'test "$PG18_RESULT" = success' in result


def test_workflow_keeps_docs_only_required_jobs_successful() -> None:
    workflow = (ROOT / ".github/workflows/python-tests.yml").read_text(encoding="utf-8")
    assert "paths-ignore:" not in workflow
    assert "docs_only: ${{ steps.filter.outputs.docs_only }}" in workflow
    for job in ("agent-system", "bridge-schema-pg18", "dashboard"):
        body = re.split(r"\n  (?=\S)", workflow.split(f"  {job}:\n", 1)[1])[0]
        assert "needs: changes" in body
        assert not re.search(r"^    if:", body, re.MULTILINE)
        steps = body.split("    steps:\n", 1)[1].split("      - ")[1:]
        assert steps
        assert all("if: needs.changes.outputs.docs_only != 'true'" in step
                   or "if: needs.changes.outputs.docs_only == 'true'" in step
                   for step in steps)
        assert any("run: echo" in step and "docs_only == 'true'" in step for step in steps)
        if job == "dashboard":
            # Checkout is skipped, so the job's default web/ directory does not exist.
            report = next(step for step in steps if "docs_only == 'true'" in step)
            assert "working-directory: ${{ github.workspace }}" in report
    assert "name: PostgreSQL 18 bridge upgrades and restarts" in workflow
    assert "name: Dashboard tests" in workflow
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "python examples/release/release-readiness-doc-smoke.py" in release
    assert "needs.changes" not in release


def test_workflow_cancels_only_superseded_pull_requests() -> None:
    workflow = (ROOT / ".github/workflows/python-tests.yml").read_text(encoding="utf-8")
    assert "group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow


@pytest.mark.parametrize("event,change,expected", [
    ("pull_request", "docs", "true"),
    ("pull_request", "mixed", "false"),
    ("pull_request", "rename-code", "false"),
    ("pull_request", "delete-code", "false"),
    ("pull_request", "delete-doc", "true"),
    ("pull_request", "empty", "false"),
    ("push", "docs", "false"),
    ("workflow_dispatch", "docs", "false"),
])
def test_workflow_docs_filter_uses_actual_changed_paths(
    tmp_path: Path, event: str, change: str, expected: str,
) -> None:
    workflow = (ROOT / ".github/workflows/python-tests.yml").read_text(encoding="utf-8")
    changes = workflow.split("  changes:\n", 1)[1].split("\n  agent-system:", 1)[0]
    script = textwrap.dedent(changes.split("        run: |\n", 1)[1])

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI Test")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "app.py").write_text("original\n")
    (tmp_path / "README.md").write_text("original\n")
    git("add", "app.py", "README.md")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    if change in ("docs", "mixed"):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/image.png").write_bytes(b"image")
        (tmp_path / "README.md").write_text("updated\n")
        (tmp_path / "notes\nwith spaces.md").write_text("updated\n")
    if change == "mixed":
        (tmp_path / "app.py").write_text("updated\n")
    elif change == "rename-code":
        git("mv", "app.py", "app.md")
    elif change.startswith("delete-"):
        (tmp_path / ("app.py" if change == "delete-code" else "README.md")).unlink()
    git("add", "--all")
    git("commit", "--allow-empty", "-qm", "change")
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script], cwd=tmp_path,
        env={**os.environ, "GITHUB_EVENT_NAME": event, "BASE_SHA": base,
             "HEAD_SHA": git("rev-parse", "HEAD"), "RUNNER_TEMP": str(tmp_path),
             "GITHUB_OUTPUT": str(output)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text().strip() == f"docs_only={expected}"
