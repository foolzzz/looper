"""``~/.loopx``, the runtime root from before the rename, becomes ``~/.looper`` the first time Looper uses it."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from looper import paths


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("old_is_link", [False, True], ids=["directory", "symlinked-directory"])
def test_only_the_old_root_moves_and_leaves_a_link(tmp_path: Path, old_is_link: bool) -> None:
    home = tmp_path / "home"
    data = tmp_path / "elsewhere" if old_is_link else home / ".loopx"
    (data / "backups").mkdir(parents=True)
    (data / "backups" / "a.dump").write_text("dump")
    if old_is_link:
        home.mkdir()
        (home / ".loopx").symlink_to(data)

    settled = paths.settle_runtime_root(home / ".looper", home=home)

    assert settled.root == home / ".looper" and not settled.warning and "moved the runtime root" in settled.note
    assert os.readlink(home / ".loopx") == ".looper"
    assert _tree(home / ".looper") == _tree(home / ".loopx") == {"backups/a.dump": "dump"}
    assert paths.settle_runtime_root(home / ".looper", home=home) == paths.SettledRoot(home / ".looper")


@pytest.mark.parametrize(
    ("before", "root", "warning"),
    [
        ({".looper", ".loopx"}, ".looper", True),  # both: the new one, the old one left as it is
        ({".looper"}, ".looper", False),
        (set(), ".looper", False),  # nothing to move, nothing created
        ({".loopx"}, "custom", False),  # a root given elsewhere moves nothing
    ],
    ids=["both", "only-new", "neither", "other-root"],
)
def test_nothing_moves_unless_only_the_old_root_exists(
    tmp_path: Path, before: set[str], root: str, warning: bool,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for name in before:
        (home / name).mkdir()
        (home / name / "marker").write_text(name)

    settled = paths.settle_runtime_root(home / root, home=home)

    assert settled.root == home / root and settled.warning is warning and bool(settled.note) is warning
    assert {p.name: p.is_symlink() for p in home.iterdir()} == {name: False for name in before}


def test_a_failed_move_keeps_using_the_old_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / ".loopx").mkdir(parents=True)

    def cross_device(src: object, dst: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(paths.os, "rename", cross_device)
    settled = paths.settle_runtime_root(home / ".looper", home=home)

    assert settled.root == home / ".loopx" and settled.warning and "could not move" in settled.note
    assert sorted(p.name for p in home.iterdir()) == [".loopx"]
