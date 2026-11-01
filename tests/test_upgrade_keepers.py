"""Upgrade planning discovers only keepers with a matching process identity."""

import json

import pytest

from looper.agents import keeper, proc_tree
from looper.upgrade import Keeper, scan_keepers


def _record(root, name, *, pid=101, started="start", protocols=("1.0",), state="running"):
    path = root / keeper.RUNS_DIR / name / keeper.STATE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "format": keeper.STATE_FORMAT, "keeper_pid": pid, "keeper_started": started,
        "child_pid": 501, "child_started": "child-start", "state": state,
        "end_reason": None, "controller_epoch": None, "socket": None,
        "protocol_versions": protocols,
    }))
    return path


def test_scan_keepers_matches_pid_and_start_time_in_one_snapshot(tmp_path, monkeypatch):
    _record(tmp_path, "live", protocols=("1.0", "1.1"))
    _record(tmp_path, "live-too", pid=102, protocols=("2.0",))
    _record(tmp_path, "dead", pid=103)
    _record(tmp_path, "reused", pid=104, started="old-start")
    _record(tmp_path, "exited", pid=105, state="exited")
    malformed = tmp_path / keeper.RUNS_DIR / "broken" / keeper.STATE
    malformed.parent.mkdir()
    malformed.write_text("{}")
    (tmp_path / keeper.RUNS_DIR / "file").touch()
    calls = []

    def start_times(pids):
        calls.append(pids)
        return {101: "start", 102: "start", 104: "new-start", 105: "start"}

    monkeypatch.setattr(proc_tree, "_start_times", start_times)

    assert scan_keepers(tmp_path) == (Keeper(("1.0", "1.1")), Keeper(("2.0",)))
    assert len(calls) == 1
    assert set(calls[0]) >= {101, 102, 103, 104}
    assert malformed.exists()


@pytest.mark.parametrize("protocols", [None, "1.0", [1], ["1.0", None], []])
def test_live_keeper_with_bad_protocols_remains_unsupported(tmp_path, monkeypatch, protocols):
    _record(tmp_path, "live", protocols=protocols)
    monkeypatch.setattr(proc_tree, "_start_times", lambda pids: {101: "start"})

    assert scan_keepers(tmp_path) == (Keeper(()),)


def test_scan_keepers_refuses_when_process_identity_is_unknown(tmp_path, monkeypatch):
    path = _record(tmp_path, "live")
    monkeypatch.setattr(proc_tree, "_start_times", lambda pids: None)

    with pytest.raises(ValueError, match="^cannot tell which keepers are running$"):
        scan_keepers(tmp_path)

    assert path.exists()


def test_scan_keepers_without_runs_creates_nothing(tmp_path, monkeypatch):
    def unexpected(pids):
        pytest.fail("no keeper processes need a liveness check")

    monkeypatch.setattr(proc_tree, "_start_times", unexpected)
    assert scan_keepers(None) == ()
    assert scan_keepers(tmp_path) == ()
    assert not (tmp_path / keeper.RUNS_DIR).exists()
