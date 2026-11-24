"""Exercise PR sharding through real pytest collection, without running the suite."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests import conftest


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


def test_duration_assignment_is_deterministic_complete_and_balanced() -> None:
    # Each shard can have one large and one small file for the optimal load of 13.
    durations = {f"tests/test_{i}.py": float(seconds)
                 for i, seconds in enumerate(range(1, 13))}
    counts = dict.fromkeys(durations, 1)
    assigned = conftest._assign_test_shards(counts, durations, 6)
    assert assigned == conftest._assign_test_shards(
        dict(reversed(list(counts.items()))), dict(reversed(list(durations.items()))), 6)
    assert set(assigned) == set(counts)
    assert set(assigned.values()) == set(range(1, 7))
    loads = [sum(durations[path] for path in counts if assigned[path] == shard)
             for shard in range(1, 7)]
    assert max(loads) / min(loads) <= 1.05
    assert max(loads) == pytest.approx(sum(durations.values()) / 6)


def test_missing_file_uses_median_seconds_per_item() -> None:
    counts = {"tests/test_a.py": 2, "tests/test_b.py": 4, "tests/test_new.py": 3}
    # Rates are 2 and 4 s/item, so the new file weighs 9s and is placed first.
    durations = {"tests/test_a.py": 4.0, "tests/test_b.py": 16.0,
                 "tests/test_removed.py": 10000.0}
    assert conftest._assign_test_shards(counts, durations, 2) == {
        "tests/test_b.py": 1, "tests/test_new.py": 2, "tests/test_a.py": 2,
    }


def test_equal_weights_break_ties_by_path_then_shard() -> None:
    counts = {f"tests/test_{letter}.py": 1 for letter in "dcba"}
    durations = dict.fromkeys(counts, 2.0)
    assert conftest._assign_test_shards(counts, durations, 2) == {
        "tests/test_a.py": 1, "tests/test_b.py": 2,
        "tests/test_c.py": 1, "tests/test_d.py": 2,
    }


def test_collection_of_only_new_files_can_still_be_sharded() -> None:
    counts = {"tests/test_a.py": 1, "tests/test_b.py": 3}
    assert conftest._assign_test_shards(counts, {"tests/test_old.py": 2.0}, 2) == {
        "tests/test_b.py": 1, "tests/test_a.py": 2,
    }


@pytest.mark.parametrize("content", [None, "{", "{}", '{"tests/test_a.py": -1}',
                                    '{"tests/test_a.py": NaN}', '{"tests/test_a.py": "slow"}'])
def test_duration_table_fails_clearly_when_unavailable_or_invalid(tmp_path, content) -> None:
    path = tmp_path / "shard_durations.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(pytest.UsageError, match="shard_durations.json"):
        conftest._load_shard_durations(path)


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
    assert "timeout-minutes: 30" in shards
    assert "fail-fast: false" in shards
    result = workflow.split("  agent-system-result:\n", 1)[1].split("\n  dashboard:", 1)[0]
    assert re.search(r"^    name: Agent system tests$", result, re.MULTILINE)
    assert "needs: [changes, agent-system, bridge-schema-pg18, provenance, dashboard]" in result
    assert "if: always()" in result
    assert "SHARDS_RESULT: ${{ needs.agent-system.result }}" in result
    assert 'test "$SHARDS_RESULT" = success' in result


def test_shards_upload_total_test_durations_even_on_failure() -> None:
    steps = _workflow()["jobs"]["agent-system"]["steps"]
    run = next(step for step in steps if step.get("name") == "Run the tests")["run"]
    assert "--dist worksteal" in run
    assert "--durations=25" in run
    assert "--junitxml=agent-shard-junit.xml" in run
    assert "junit_family=xunit1" in run  # Preserve file paths in xdist JUnit reports.
    assert "junit_duration_report=total" in run  # setup + call + teardown
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert "always()" in upload["if"]
    assert upload["with"]["name"] == "agent-shard-junit-${{ matrix.shard }}-of-6"
    assert upload["with"]["path"] == "agent-shard-junit.xml"
    assert int(upload["with"]["retention-days"]) <= 7


def test_workflow_keeps_docs_only_required_jobs_successful() -> None:
    workflow = (ROOT / ".github/workflows/python-tests.yml").read_text(encoding="utf-8")
    assert "paths-ignore:" not in workflow
    assert "docs_only: ${{ steps.filter.outputs.docs_only }}" in workflow
    assert "web: ${{ steps.filter.outputs.web }}" in workflow
    for job in ("agent-system", "bridge-schema-pg18", "provenance", "dashboard"):
        body = re.split(r"\n  (?=\S)", workflow.split(f"  {job}:\n", 1)[1])[0]
        assert "needs: changes" in body
        if job == "agent-system":
            assert not re.search(r"^    if:", body, re.MULTILINE)
        steps = body.split("    steps:\n", 1)[1].split("      - ")[1:]
        assert steps
        assert all("if: needs.changes.outputs.docs_only != 'true'" in step
                   or "if: always() && needs.changes.outputs.docs_only != 'true'" in step
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
    assert dict(line.split("=", 1) for line in output.read_text().splitlines())["docs_only"] == expected


def _workflow() -> dict:
    # BaseLoader keeps GitHub's "on" key and boolean-looking outputs as strings.
    return yaml.load((ROOT / ".github/workflows/python-tests.yml").read_text(), Loader=yaml.BaseLoader)


def test_workflow_runs_on_pull_requests_and_dispatch_only() -> None:
    assert _workflow()["on"] == {
        "pull_request": "", "workflow_dispatch": "",
    }


@pytest.mark.parametrize("event,ref,head,web,docs,canary,dashboard", [
    ("pull_request", "refs/pull/1/merge", "codex/backend", "false", "false", False, False),
    ("pull_request", "refs/pull/1/merge", "codex/frontend", "true", "false", False, True),
    ("pull_request", "refs/pull/1/merge", "codex/release-2.0", "false", "false", True, True),
    ("pull_request", "refs/pull/1/merge", "feature/codex/release-2.0", "false", "false", False, False),
    ("pull_request", "refs/pull/1/merge", "codex/release", "false", "false", False, False),
    ("pull_request", "refs/pull/1/merge", "codex/docs", "true", "true", False, False),
    ("pull_request", "refs/pull/1/merge", "codex/release-2.0", "false", "true", True, False),
    ("push", "refs/heads/main", "", "false", "false", False, False),
    ("push", "refs/heads/feature", "codex/release-2.0", "false", "false", False, False),
    ("workflow_dispatch", "refs/heads/main", "", "false", "false", True, True),
    ("workflow_dispatch", "refs/heads/feature", "", "false", "false", True, True),
])
def test_workflow_optional_job_conditions(event, ref, head, web, docs, canary, dashboard) -> None:
    # Evaluate the workflow's own conditions, with all dependencies successful.
    # The expressions use only comparisons, &&, || and startsWith.
    context = {
        "github": SimpleNamespace(event_name=event, ref=ref, head_ref=head),
        "needs": SimpleNamespace(changes=SimpleNamespace(
            outputs=SimpleNamespace(web=web, docs_only=docs))),
        "startsWith": str.startswith,
    }
    jobs = _workflow()["jobs"]
    for name, expected in (("bridge-schema-pg18", canary), ("provenance", canary), ("dashboard", dashboard)):
        expression = jobs[name]["if"].replace("&&", " and ").replace("||", " or ")
        assert eval(expression, {"__builtins__": {}}, context) is expected, name


@pytest.mark.parametrize("results", [
    dict(zip(("changes", "agent-system", "bridge-schema-pg18", "provenance", "dashboard"),
             ("success", "success", *optional)))
    for optional in product(("success", "skipped"), repeat=3)
] + [
    {name: result} for name in ("changes", "agent-system", "bridge-schema-pg18", "provenance", "dashboard")
    for result in ("failure", "cancelled")
] + [{name: "skipped"} for name in ("changes", "agent-system")])
def test_required_check_uses_actual_needs_results(results) -> None:
    job = _workflow()["jobs"]["agent-system-result"]
    assert job["if"] == "always()"  # Still execute after a failed or skipped dependency.
    all_results = {name: "success" for name in job["needs"]}
    all_results.update(results)
    step, = job["steps"]
    env = os.environ.copy()
    for name, expression in step["env"].items():
        match = re.fullmatch(r"\$\{\{ needs\.([\w-]+)\.result \}\}", expression)
        assert match, expression
        env[name] = all_results[match[1]]
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                            env=env, capture_output=True, text=True, timeout=10)
    expected = (all(all_results[name] == "success" for name in ("changes", "agent-system"))
                and all(value in ("success", "skipped") for value in all_results.values()))
    assert (result.returncode == 0) is expected, result.stdout + result.stderr


@pytest.mark.parametrize("operation", ["add", "delete", "rename"])
@pytest.mark.parametrize("path,expected", [
    ("web/src/App.tsx", "true"), ("web/package-lock.json", "true"),
    ("web/vite.config.ts", "true"), ("scripts/stage-dashboard.sh", "true"),
    ("looper/components.py", "true"), ("looper/components.toml", "true"),
    (".github/workflows/python-tests.yml", "true"), (".npmrc", "true"),
    (".gitignore", "true"), ("looper/agents/serve.py", "false"),
    ("tests/test_ci_shards.py", "false"), ("README.md", "false"),
])
def test_workflow_web_filter_scans_past_backend_changes(tmp_path, path, expected, operation) -> None:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI Test")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "app.py").write_text("base")
    changed = tmp_path / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    if operation != "add":
        changed.write_text("original")
    git("add", "--all")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "app.py").write_text("backend changes sort before web changes")
    if operation == "delete":
        changed.unlink()
    elif operation == "rename":
        # Moving a build input out of web/ must still enable the dashboard job.
        git("mv", path, "moved-input.txt")
    else:
        changed.write_text("changed")
    git("add", "--all")
    git("commit", "-qm", "change")
    script = _workflow()["jobs"]["changes"]["steps"][-1]["run"]
    output = tmp_path / "output"
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=tmp_path,
        env={**os.environ, "GITHUB_EVENT_NAME": "pull_request", "BASE_SHA": base,
             "HEAD_SHA": git("rev-parse", "HEAD"), "RUNNER_TEMP": str(tmp_path),
             "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert dict(line.split("=", 1) for line in output.read_text().splitlines())["web"] == expected
