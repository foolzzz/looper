"""The private temporary directory of a direct launch: the length guard, ownership and mode, and removal."""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from pathlib import Path

import pytest

from looper.agents import task_manager_tmpdir as tmpdir


@pytest.fixture
def base():
    """A short scratch base: pytest's own temporary paths are longer than the guard allows."""
    path = tempfile.mkdtemp(prefix="lt-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path)


def _mode(path) -> int:
    return os.lstat(path).st_mode & 0o7777


def test_create_makes_two_private_levels_and_a_new_directory_each_time(base):
    old = os.umask(0o277)
    try:
        first, second = tmpdir.create(base=base), tmpdir.create(base=base)
    finally:
        os.umask(old)
    parent = Path(base) / f"lpr-{os.getuid()}"
    assert first.path != second.path and Path(first.path).parent == parent
    assert [_mode(path) for path in (parent, first.path, second.path)] == [0o700] * 3
    assert first.write_root == f"{os.path.realpath(first.path)}/claude-{os.getuid()}/"
    first.remove()
    second.remove()


def test_the_default_location_is_within_the_limit():
    made = tmpdir.create()
    try:
        assert len(f"{made.path}/claude-{os.getuid()}".encode()) <= tmpdir.MAX_ROOT_BYTES
    finally:
        made.remove()


@pytest.fixture
def bases():
    """Directories under ``/tmp`` whose launch root is exactly the limit, and one byte longer."""
    uid = os.getuid()
    rest = len(f"/tmp//lpr-{uid}/{'0' * 8}/claude-{uid}")
    made = []
    for length in (tmpdir.MAX_ROOT_BYTES, tmpdir.MAX_ROOT_BYTES + 1):
        path = f"/tmp/{secrets.token_hex(16)[:length - rest]}"
        os.mkdir(path, 0o700)
        made.append(path)
    try:
        yield made
    finally:
        for path in made:
            shutil.rmtree(path)


def test_a_root_of_exactly_the_limit_is_used_and_one_byte_more_is_refused_before_anything_is_made(bases):
    fits, too_long = bases
    made = tmpdir.create(base=fits)
    assert len(f"{made.path}/claude-{os.getuid()}".encode()) == 44
    made.remove()
    with pytest.raises(tmpdir.Unusable, match="longer than 44 bytes"):
        tmpdir.create(base=too_long)
    assert os.listdir(too_long) == []


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o600])
def test_a_parent_with_another_mode_is_refused_and_not_repaired(base, mode):
    parent = Path(base) / f"lpr-{os.getuid()}"
    parent.mkdir()
    parent.chmod(mode)
    try:
        with pytest.raises(tmpdir.Unusable):
            tmpdir.create(base=base)
        assert _mode(parent) == mode
    finally:
        parent.chmod(0o700)
    assert os.listdir(parent) == []


def test_a_parent_owned_by_another_user_is_refused(base):
    other = os.getuid() + 1  # the directory this process makes is not that user's
    parent = Path(base) / f"lpr-{other}"
    parent.mkdir(mode=0o700)
    with pytest.raises(tmpdir.Unusable, match="owned by user"):
        tmpdir.create(uid=other, base=base)
    assert os.listdir(parent) == []


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_a_parent_that_is_a_link_or_a_file_is_refused(base, kind):
    parent = Path(base) / f"lpr-{os.getuid()}"
    target = Path(base) / "elsewhere"
    target.mkdir(mode=0o700)
    if kind == "symlink":
        parent.symlink_to(target)
    else:
        parent.write_text("")
    with pytest.raises(tmpdir.Unusable):
        tmpdir.create(base=base)
    assert os.listdir(target) == []


def test_an_existing_entry_is_never_adopted(base, monkeypatch):
    monkeypatch.setattr(tmpdir.secrets, "token_hex", lambda n: "0" * 8)
    first = tmpdir.create(base=base)
    with pytest.raises(tmpdir.Unusable, match="FileExistsError"):
        tmpdir.create(base=base)
    assert os.path.isdir(first.path)
    first.remove()


def test_remove_deletes_the_directory_without_following_links_out_of_it(base):
    outside = Path(base) / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    made = tmpdir.create(base=base)
    inner = Path(made.path) / "claude-x"
    inner.mkdir()
    (inner / "link").symlink_to(outside)
    made.remove()
    made.remove()  # twice is harmless
    assert not os.path.exists(made.path) and (outside / "keep").read_text() == "keep"


def test_remove_leaves_a_directory_that_replaced_the_one_it_made(base):
    made = tmpdir.create(base=base)
    os.rename(made.path, f"{made.path}-moved")
    os.mkdir(made.path)
    (Path(made.path) / "other").write_text("other")
    made.remove()
    assert (Path(made.path) / "other").exists()
