"""``looper/agents/safe_read.py``: a file is read only from inside a boundary directory.

Every "outside" file here is a fake one under ``tmp_path``.
"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from looper.agents import safe_read
from looper.agents.safe_read import Content, Skip, Skipped, read_inside

LIMIT = 64


@pytest.fixture(autouse=True)
def no_descriptor_is_left_open() -> Iterator[None]:
    before = _open_fds()
    yield
    assert _open_fds() == before


def _open_fds() -> set[int]:
    found = set()
    for fd in range(1024):
        with contextlib.suppress(OSError):
            os.fstat(fd)
            found.add(fd)
    return found


@contextlib.contextmanager
def _deadline(seconds: int = 5) -> Iterator[None]:
    """Fail, rather than hang, when something blocks."""
    def expired(*_: Any) -> None:
        raise AssertionError(f"still blocked after {seconds} s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    (tmp_path / "base" / "dir").mkdir(parents=True)
    (tmp_path / "base" / "dir" / "file").write_text("INSIDE")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "file").write_text("FAKE-OUTSIDE")
    return tmp_path / "base"


def test_a_file_inside_is_read(base: Path) -> None:
    assert read_inside(base, base / "dir" / "file", LIMIT) == Content(b"INSIDE", 6)


def test_a_link_to_a_file_inside_is_read_as_that_file(base: Path) -> None:
    (base / "alias").symlink_to("dir/file")
    (base / "dir-alias").symlink_to("dir")
    assert read_inside(base, base / "alias", LIMIT) == Content(b"INSIDE", 6)
    assert read_inside(base, base / "dir-alias" / "file", LIMIT) == Content(b"INSIDE", 6)


def test_a_boundary_named_through_a_link_holds_its_files(base: Path, tmp_path: Path) -> None:
    (tmp_path / "base-alias").symlink_to(base)
    assert read_inside(tmp_path / "base-alias", tmp_path / "base-alias" / "dir" / "file", LIMIT) == Content(b"INSIDE", 6)


@pytest.mark.parametrize("target", ["relative", "absolute"])
def test_a_link_to_a_file_outside_is_skipped(base: Path, tmp_path: Path, target: str) -> None:
    outside = tmp_path / "outside" / "file"
    (base / "link").symlink_to("../outside/file" if target == "relative" else outside)
    assert read_inside(base, base / "link", LIMIT) == Skipped(Skip.OUTSIDE)


def test_a_directory_on_the_way_that_links_outside_is_skipped(base: Path, tmp_path: Path) -> None:
    (base / "away").symlink_to(tmp_path / "outside")
    assert read_inside(base, base / "away" / "file", LIMIT) == Skipped(Skip.OUTSIDE)


@pytest.mark.parametrize("path", ["outside/file", "base/../outside/file", "base-sibling/file"])
def test_a_path_outside_the_boundary_is_skipped(base: Path, tmp_path: Path, path: str) -> None:
    (tmp_path / "base-sibling").mkdir()
    (tmp_path / "base-sibling" / "file").write_text("FAKE-SIBLING")
    assert read_inside(base, f"{tmp_path}/{path}", LIMIT) == Skipped(Skip.OUTSIDE)


def _after_the_check(monkeypatch: pytest.MonkeyPatch, swap: Callable[[], None]) -> None:
    """Run ``swap`` after the path was resolved and found inside, before it is opened."""
    opened = safe_read._open_inside

    def raced(*args: Any) -> Any:
        swap()
        return opened(*args)

    monkeypatch.setattr(safe_read, "_open_inside", raced)


@pytest.mark.parametrize("swapped", ["the file", "a directory on the way", "the boundary"])
def test_a_link_swapped_in_after_the_check_is_not_followed(
    base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swapped: str,
) -> None:
    def swap() -> None:
        if swapped == "the file":
            (base / "dir" / "file").unlink()
            (base / "dir" / "file").symlink_to(tmp_path / "outside" / "file")
        elif swapped == "a directory on the way":
            (base / "dir").rename(base / "moved")
            (base / "dir").symlink_to(tmp_path / "outside")
        else:
            (tmp_path / "outside" / "dir").mkdir()
            (tmp_path / "outside" / "dir" / "file").write_text("FAKE-OUTSIDE")
            base.rename(tmp_path / "moved")
            base.symlink_to(tmp_path / "outside")

    _after_the_check(monkeypatch, swap)
    assert read_inside(base, base / "dir" / "file", LIMIT) == Skipped(Skip.SYMLINK)


def test_a_file_with_another_hard_link_is_skipped(base: Path, tmp_path: Path) -> None:
    os.link(tmp_path / "outside" / "file", base / "linked")
    assert read_inside(base, base / "linked", LIMIT) == Skipped(Skip.HARD_LINKED)


@pytest.mark.parametrize("replaced", [False, True], ids=["removed", "replaced by another file"])
def test_removing_the_name_inside_after_the_open_does_not_hide_a_hard_link(
    base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: bool,
) -> None:
    os.link(tmp_path / "outside" / "file", base / "linked")
    fstat = os.fstat

    def raced(fd: int) -> os.stat_result:
        monkeypatch.setattr(os, "fstat", fstat)
        (base / "linked").unlink()  # one link is left: the file outside
        if replaced:
            (base / "linked").write_text("DECOY")
        return fstat(fd)

    monkeypatch.setattr(os, "fstat", raced)
    result = read_inside(base, base / "linked", LIMIT)

    assert isinstance(result, Skipped) and result.reason is Skip.IO_ERROR


def test_a_fifo_is_skipped_without_waiting_for_a_writer(base: Path) -> None:
    os.mkfifo(base / "fifo")
    with _deadline():
        assert read_inside(base, base / "fifo", LIMIT) == Skipped(Skip.NOT_REGULAR)


@pytest.mark.parametrize("device", ["/dev/null", "/dev/zero"])
def test_a_device_is_skipped_without_being_read(device: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "read", lambda *_: pytest.fail("a device was read"))
    with _deadline():
        assert read_inside("/dev", device, LIMIT, clip=True) == Skipped(Skip.NOT_REGULAR)


@pytest.mark.parametrize("path", ["dir", "."])
def test_a_directory_is_skipped(base: Path, path: str) -> None:
    assert read_inside(base, base / path, LIMIT) == Skipped(Skip.NOT_REGULAR)


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    got: list[int] = []
    read = os.read

    def counted(fd: int, size: int) -> bytes:
        data = read(fd, size)
        got.append(len(data))
        return data

    monkeypatch.setattr(os, "read", counted)
    return got


def test_a_file_over_the_limit_is_skipped_unread_or_clipped_without_reading_it_whole(
    base: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (base / "large").write_bytes(b"x" * (LIMIT * 100))
    got = _count_reads(monkeypatch)

    assert read_inside(base, base / "large", LIMIT) == Skipped(Skip.TOO_LARGE)
    assert got == []
    assert read_inside(base, base / "large", LIMIT, clip=True) == Content(b"x" * LIMIT, LIMIT * 100)
    assert sum(got) == LIMIT + 1


@pytest.mark.parametrize("size, skipped", [(LIMIT, False), (LIMIT + 1, True)])
def test_the_limit_itself_is_read(base: Path, size: int, skipped: bool) -> None:
    (base / "edge").write_bytes(b"x" * size)
    assert read_inside(base, base / "edge", LIMIT) == (Skipped(Skip.TOO_LARGE) if skipped
                                                       else Content(b"x" * LIMIT, LIMIT))


def test_a_file_that_grows_after_it_was_measured_is_still_too_large(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (base / "grows").write_bytes(b"x")
    fstat = os.fstat

    def raced(fd: int) -> os.stat_result:
        monkeypatch.setattr(os, "fstat", fstat)
        measured = fstat(fd)
        (base / "grows").write_bytes(b"x" * (LIMIT * 2))
        return measured

    monkeypatch.setattr(os, "fstat", raced)
    assert read_inside(base, base / "grows", LIMIT) == Skipped(Skip.TOO_LARGE)


@pytest.mark.parametrize("path", ["missing", "dir/file/below"])
def test_a_path_that_cannot_be_opened_is_an_io_error(base: Path, path: str) -> None:
    result = read_inside(base, base / path, LIMIT)
    assert isinstance(result, Skipped) and result.reason in (Skip.IO_ERROR, Skip.SYMLINK)
    assert "INSIDE" not in result.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_file_without_permission_is_an_io_error_naming_the_cause(base: Path) -> None:
    os.chmod(base / "dir" / "file", 0)
    try:
        result = read_inside(base, base / "dir" / "file", LIMIT)
    finally:
        os.chmod(base / "dir" / "file", 0o600)
    assert result == Skipped(Skip.IO_ERROR, "PermissionError: Permission denied")


def test_a_failed_read_is_an_io_error(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "read", broken)
    assert read_inside(base, base / "dir" / "file", LIMIT) == Skipped(Skip.IO_ERROR, "OSError: Input/output error")
