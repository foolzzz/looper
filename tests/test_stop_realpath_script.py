"""``evals/stop_realpath.py``: the pure parts of the by-hand real-path stop script."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import stop_realpath as sr


def test_arguments_default_and_choices() -> None:
    args = sr.parse_args(["--runtime", "claude"])
    assert (args.runtime, args.repeat, args.out) == ("claude", 3, None)
    args = sr.parse_args(["--runtime", "omp", "--repeat", "2", "--out", "r.md"])
    assert (args.runtime, args.repeat, args.out) == ("omp", 2, Path("r.md"))
    with pytest.raises(SystemExit):
        sr.parse_args(["--runtime", "codex"])
    with pytest.raises(SystemExit):
        sr.parse_args([])


def test_cells_by_runtime() -> None:
    assert [c.key for c in sr.cells_for("claude")] == ["a", "b", "c", "d", "e"]
    assert [c.key for c in sr.cells_for("omp")] == ["a", "b", "c", "d"]


def test_the_port_is_never_8767(monkeypatch: pytest.MonkeyPatch) -> None:
    ports = iter([8767, 8767, 45123])
    monkeypatch.setattr(sr, "_ask_os_for_port", lambda: next(ports))
    assert sr.pick_port() == 45123


def test_the_report_redacts_home_and_tmp() -> None:
    home = str(Path.home())
    text = sr.redact(f"{home}/x and /tmp/stop-abc/f.txt", tmp="/tmp/stop-abc")
    assert text == "~/x and <tmp>/f.txt"


def test_the_report_has_one_row_per_cell_and_repetition() -> None:
    results = [sr.CellResult("a", 1, []), sr.CellResult("a", 2, ["answer not stopped"])]
    report = sr.render_report("claude", results, claude_version="2.1.0", meta_keys=[["x/y"]], tmp="/tmp/t")
    rows = [line for line in report.splitlines() if line.startswith("| a ")]
    assert len(rows) == 2
    assert "pass" in rows[0] and "FAIL" in rows[1] and "answer not stopped" in rows[1]
    assert "2.1.0" in report and "x/y" in report


def _good(**over):
    base = dict(
        final={"text": "partial", "outcome": "stopped"}, final_after_s=1.0, pids=[101, 102], alive=[],
        record={"status": "interrupted", "metadata": {"user_stop": {"phase": "activity", "method": "control"}}},
        actions=[{"status": "error", "error_code": "stopped"}], confirmations=0,
        next_text="hi", session_before="s1", session_after="s1", expect_pids=True)
    base.update(over)
    return sr.Observation(**base)


def test_a_clean_cell_passes() -> None:
    assert sr.verdict(_good()) == []


@pytest.mark.parametrize(("over", "needle"), [
    ({"final": None}, "answer.final"),
    ({"final": {"text": "x", "outcome": "answered"}}, "outcome"),
    ({"final_after_s": 20.0}, "15 s"),
    ({"alive": [102]}, "alive"),
    ({"pids": []}, "no pid"),
    ({"record": {"status": "completed", "metadata": {}}}, "interrupted"),
    ({"record": {"status": "interrupted", "metadata": {}}}, "user_stop"),
    ({"actions": [{"status": "ok"}]}, "stopped"),
    ({"confirmations": 1}, "confirmation"),
    ({"next_text": ""}, "next turn"),
    ({"session_after": "s2"}, "session"),
])
def test_a_failing_cell_says_why(over: dict, needle: str) -> None:
    assert any(needle in why for why in sr.verdict(_good(**over)))


def test_a_cell_without_commands_needs_no_pids() -> None:
    assert sr.verdict(_good(pids=[], expect_pids=False)) == []


def test_read_trace_returns_keys_only(tmp_path: Path) -> None:
    trace = tmp_path / "t"
    trace.write_text(json.dumps({"meta_keys": ["a"]}) + "\n" + json.dumps({"meta_keys": None}) + "\nbad\n")
    assert sr.read_trace(trace) == [["a"], []]
    assert sr.read_trace(tmp_path / "missing") == []
