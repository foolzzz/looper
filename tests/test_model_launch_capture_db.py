"""Where the pinned v2 launch fixtures come from: a clean patched v2 checkout, captured offline.

The fixtures hold what v2.12 started every role of every case with. These tests clone that revision, capture the
launches again on it with its own interpreter, and expect the fixture back: the proof the fixtures were not made
from the code they now judge. They need git history and a disposable LOOPER_TEST_DATABASE_URL, and they take a
while (a clone and a venv each), so they carry the ``provenance`` mark. Run them when a fixture or
``model_launch_parity.py`` changes: ``pytest -m provenance tests/test_model_launch_capture_db.py``.
"""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

import pytest

import model_launch_parity as parity

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = [
    pytest.mark.provenance,
    pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"),
                       reason="legacy SQL capture needs a disposable LOOPER_TEST_DATABASE_URL"),
]


def _old_checkout(tmp_path: Path, revision: str) -> Path:
    repository = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "legacy"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(repository), str(checkout)],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", revision],
                   check=True, capture_output=True, timeout=60)
    # Reuse only the test interpreter's installed dependencies, offline. The
    # isolated child verifies that Looper and the driver import from v2.
    venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
    packages = checkout / ".venv/lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    (packages / "test-dependencies.pth").write_text("\n".join(site.getsitepackages()) + "\n")
    return checkout


@pytest.mark.parametrize("runtime,generate,revision", [
    ("codex", parity.generate_codex_baseline, parity.CODEX_LEGACY_REVISION),
    ("omp", parity.generate_omp_baseline, parity.OMP_LEGACY_REVISION),
    ("grok", parity.generate_grok_baseline, parity.GROK_LEGACY_REVISION),
])
def test_old_checkout_reproduces_pinned_full_contract(
    runtime: str, generate: Any, revision: str, tmp_path: Path,
) -> None:
    fixture = FIXTURES / f"model_launch_parity_v2_{runtime}.json"
    checkout = _old_checkout(tmp_path, revision)
    original = fixture.read_bytes()
    output = tmp_path / "captured.json"
    generate(checkout, output)
    assert json.loads(output.read_text()) == json.loads(original)
    assert fixture.read_bytes() == original


def test_old_checkout_captures_sql_sessions_without_refreshing_the_fixture(tmp_path: Path) -> None:
    fixture = FIXTURES / "model_launch_parity_v2_claude.json"
    baseline = json.loads(fixture.read_text("utf-8"))
    checkout = _old_checkout(tmp_path, parity.CLAUDE_LEGACY_REVISION)
    original = fixture.read_bytes()
    output = tmp_path / "captured.json"
    parity.generate_claude_baseline(checkout, output)
    captured = json.loads(output.read_text("utf-8"))
    assert captured["source"] == baseline["source"]
    assert captured["captures"].keys() == parity.CLAUDE_CASES.keys()
    for case, records in captured["captures"].items():
        assert len(records) == 4
        for before, after in zip(baseline["captures"][case], records):
            assert after.keys() == parity.FIELDS
            # The full argv/settings, including ancestor exclusions that name
            # this host's directories, remain in the capture. They are never
            # discarded or rewritten to make the pinned file pass.
            for field in parity.FIELDS - {"argv", "config", "isolation"}:
                assert after[field] == before[field], (case, field)
            for field in ("protocol", "model", "effort", "catalog"):
                assert after["config"][field] == before["config"][field]
            assert json.loads(after["config"]["settings"]) == after["isolation"]
    assert fixture.read_bytes() == original
