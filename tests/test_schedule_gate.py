"""A schedule's gate command: what it printed, how it ended, and that nothing it started outlives it."""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from looper.agents.schedule_gate import STDOUT_MAX_CHARS, run_gate


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import proc_tree

    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.5)


def _gate(command: str, work_dir: Path, timeout: float = 5):
    return asyncio.run(run_gate(command, str(work_dir), timeout))


def _alive(marker: str) -> list[str]:
    return subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()


def test_output_and_a_clean_exit(tmp_path: Path) -> None:
    (tmp_path / "items.txt").write_text("alert-1\nalert-2\n")
    result = _gate("cat items.txt; echo noted >&2", tmp_path)
    assert (result.ok, result.exit_code, result.stdout, result.stderr_tail) == (True, 0, "alert-1\nalert-2\n",
                                                                                 "noted\n")


def test_nothing_printed_is_still_a_success(tmp_path: Path) -> None:
    result = _gate("true", tmp_path)
    assert (result.ok, result.stdout) == (True, "")


def test_a_non_zero_exit_fails_with_the_tail_of_stderr(tmp_path: Path) -> None:
    result = _gate("echo partial; seq 1 5000 >&2; exit 3", tmp_path)
    assert (result.ok, result.exit_code, result.stdout) == (False, 3, "partial\n")
    failure = result.failure(5)
    assert failure.startswith("the gate exited with status 3; stderr: ") and failure.endswith("4999\n5000")
    assert len(result.stderr_tail) <= 2000


def test_a_timeout_ends_the_whole_group_even_what_ignores_sigterm(tmp_path: Path) -> None:
    marker = f"sleep 31{uuid.uuid4().int % 1000:03d}"
    started = time.monotonic()
    result = _gate(f"trap '' TERM; {marker} & echo started; wait", tmp_path, timeout=0.5)
    assert result.timed_out and not result.ok and result.stdout == "started\n"
    assert "did not finish within 0.5 s" in result.failure(0.5)
    assert time.monotonic() - started < 5
    assert _alive(marker) == []


def test_what_it_leaves_behind_does_not_hold_it_up_or_outlive_it(tmp_path: Path) -> None:
    marker = f"sleep 32{uuid.uuid4().int % 1000:03d}"
    started = time.monotonic()
    result = _gate(f"{marker} & echo started", tmp_path)
    assert (result.ok, result.stdout) == (True, "started\n")
    assert time.monotonic() - started < 2
    assert _alive(marker) == []


def test_output_is_clipped_and_the_rest_drained(tmp_path: Path) -> None:
    result = _gate("yes | head -c 1000000", tmp_path)
    assert result.ok and len(result.stdout) == STDOUT_MAX_CHARS and result.stdout_clipped


def test_a_missing_work_directory_is_a_failure(tmp_path: Path) -> None:
    result = _gate("true", tmp_path / "gone")
    assert not result.ok and "could not start" in result.failure(5)


def test_cancelling_it_ends_the_group(tmp_path: Path) -> None:
    marker = f"sleep 33{uuid.uuid4().int % 1000:03d}"

    async def main() -> None:
        job = asyncio.create_task(run_gate(f"{marker}", str(tmp_path), 30))
        await asyncio.sleep(0.3)
        assert _alive(marker)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job

    asyncio.run(main())
    assert _alive(marker) == []
