"""Write notifications survive restoration without treating reads as writes."""

import sys

import pytest

from looper.agents.verification_writes import VerificationWrites


@pytest.mark.parametrize("change", ["restore", "replace", "delete_restore", "chmod_restore", "hardlink_restore"])
def test_transient_tracked_writes_are_recorded(tmp_path, change):
    source = tmp_path / "nested" / "code.py"
    source.parent.mkdir()
    source.write_text("original")
    alias = tmp_path / "ignored" / "alias.py"
    if change == "hardlink_restore":
        alias.parent.mkdir()
        alias.hardlink_to(source)
    monitor = VerificationWrites(tmp_path, ["nested/code.py"])
    monitor.start()
    try:
        if change == "restore":
            source.write_text("changed")
            source.write_text("original")
        elif change == "replace":
            replacement = source.with_suffix(".new")
            replacement.write_text("original")
            replacement.replace(source)
        elif change == "delete_restore":
            source.unlink()
            source.write_text("original")
        elif change == "chmod_restore":
            before = source.stat().st_mode
            source.chmod(0o400)
            source.chmod(before)
        else:
            alias.write_text("changed through alias")
            alias.write_text("original")
        assert monitor.finish() == ["nested/code.py"]
        assert monitor.finish() == ["nested/code.py"]
    finally:
        monitor.close()


def test_reads_and_ignored_build_output_are_not_code_writes(tmp_path):
    source = tmp_path / "code.py"
    source.write_text("original")
    monitor = VerificationWrites(tmp_path, ["code.py"])
    monitor.start()
    try:
        assert source.read_text() == "original"
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "code.pyc").write_bytes(b"compiled")
        (tmp_path / ".env").write_text("STAGING=true")
        assert monitor.finish() == []
    finally:
        monitor.close()


def test_renaming_tracked_parent_and_restoring_it_is_recorded(tmp_path):
    folder = tmp_path / "nested"
    folder.mkdir()
    (folder / "code.py").write_text("original")
    monitor = VerificationWrites(tmp_path, ["nested/code.py"])
    monitor.start()
    try:
        moved = tmp_path / "moved"
        folder.rename(moved)
        moved.rename(folder)
        assert monitor.finish() == ["nested/code.py"]
    finally:
        monitor.close()


def test_missing_tracked_path_create_then_delete_is_recorded(tmp_path):
    monitor = VerificationWrites(tmp_path, ["missing/code.py"])
    monitor.start()
    try:
        folder = tmp_path / "missing"
        folder.mkdir()
        (folder / "code.py").write_text("temporary code")
        (folder / "code.py").unlink()
        folder.rmdir()
        assert monitor.finish() == ["missing/code.py"]
    finally:
        monitor.close()


def test_monitor_watches_links_without_counting_ignored_target_writes(tmp_path):
    target = tmp_path / "ignored"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target, target_is_directory=True)
    monitor = VerificationWrites(tmp_path, ["alias"])
    monitor.start()
    try:
        (target / "output").write_text("build output")
        assert monitor.finish() == []
    finally:
        monitor.close()


def test_monitor_closes_descriptors_when_start_fails(tmp_path, monkeypatch):
    (tmp_path / "code.py").write_text("original")
    monitor = VerificationWrites(tmp_path, ["code.py"])
    method = "_start_inotify" if sys.platform == "linux" else "_start_kqueue"
    initialize = getattr(monitor, method)

    def refused():
        initialize()
        raise OSError("file descriptor limit reached")

    monkeypatch.setattr(monitor, method, refused)
    with pytest.raises(OSError):
        monitor.start()
    assert monitor._queue is None
    assert not monitor._fds
    assert monitor._inotify is None


@pytest.mark.parametrize("name", ["../outside", "/outside"])
def test_monitor_rejects_paths_outside_checkout(tmp_path, name):
    monitor = VerificationWrites(tmp_path, [name])
    with pytest.raises(OSError, match="leaves checkout"):
        monitor.start()
