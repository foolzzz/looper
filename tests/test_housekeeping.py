"""Filesystem retention protects active output and preserves append writers during rotation."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from looper.agents.housekeeping import prune_logs, prune_runner_files, rotate


NOW = datetime(2030, 1, 15, tzinfo=timezone.utc)


def _file(path: Path, size: int, age: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = (NOW - timedelta(days=age)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_rotation_preserves_inode_and_an_existing_append_writer(tmp_path: Path) -> None:
    path = _file(tmp_path / "serve.log", 25 * 1024**2)
    inode = path.stat().st_ino
    with path.open("ab", buffering=0) as writer:
        assert rotate(path, at_bytes=20 * 1024**2, keep=5)
        assert path.stat().st_ino == inode and path.stat().st_size == 0
        assert (tmp_path / "serve.log.1").read_bytes() == b"x" * (25 * 1024**2)
        writer.write(b"next line\n")
    assert path.read_bytes() == b"next line\n"


def test_rotation_keeps_five_copies_and_leaves_small_or_missing_files(tmp_path: Path) -> None:
    path = _file(tmp_path / "serve.log", 20)
    for i in range(1, 6):
        path.with_name(f"serve.log.{i}").write_text(str(i))
    assert rotate(path, at_bytes=20, keep=5)
    assert (tmp_path / "serve.log.1").read_bytes() == b"x" * 20
    assert [(tmp_path / f"serve.log.{i}").read_text() for i in range(2, 6)] == ["1", "2", "3", "4"]
    assert len(list(tmp_path.iterdir())) == 6
    path.write_bytes(b"small")
    assert not rotate(path, at_bytes=20, keep=5)
    assert path.read_bytes() == b"small"
    assert not rotate(tmp_path / "absent.log", at_bytes=20, keep=5)


def test_logs_expire_by_age_and_remove_empty_schedule_directories(tmp_path: Path) -> None:
    old = _file(tmp_path / "expired" / "old.log", 12, age=40)
    fresh = _file(tmp_path / "recent" / "new.log", 13)
    assert prune_logs(tmp_path, NOW - timedelta(days=30), 100, set()) == (1, 12)
    assert not old.parent.exists() and fresh.exists()


def test_logs_drop_oldest_for_total_limit_and_protect_open_logs(tmp_path: Path) -> None:
    protected = _file(tmp_path / "schedule" / "open.log", 20, age=50)
    oldest = _file(tmp_path / "schedule" / "old.log", 20, age=2)
    newest = _file(tmp_path / "schedule" / "new.log", 20, age=1)
    assert prune_logs(tmp_path, NOW - timedelta(days=30), 40, {protected}) == (1, 20)
    assert protected.exists() and newest.exists() and not oldest.exists()
    assert prune_logs(tmp_path, NOW - timedelta(days=30), 1, {protected}) == (1, 20)
    assert protected.exists()  # Active output alone may exceed the cap.


def test_runner_cleanup_keeps_current_and_recent_source(tmp_path: Path) -> None:
    current = _file(tmp_path / "runner-current.py", 1, age=40)
    old = _file(tmp_path / "runner-old.py", 1, age=32)
    recent = _file(tmp_path / "runner-recent.py", 1, age=31)
    other = _file(tmp_path / "unrelated.py", 1, age=40)
    assert prune_runner_files(tmp_path, current, NOW) == 1
    assert current.exists() and recent.exists() and other.exists() and not old.exists()
