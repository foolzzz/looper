"""A task runs in the directory the user chose; there is no default."""

from __future__ import annotations

import os

import pytest

from loopx.agents.tiers.task import resolve_work_dir


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("~/projects/tetris", os.path.join(os.path.expanduser("~"), "projects", "tetris")),
        ("/tmp/proj/", "/tmp/proj"),
        ("  /tmp/a/../b  ", "/tmp/b"),
    ],
)
def test_resolves_to_an_absolute_path(raw: str, expected: str) -> None:
    assert resolve_work_dir(raw) == expected


@pytest.mark.parametrize(("raw", "message"), [
    (None, "required"),
    ("", "required"),
    ("   ", "required"),
    ("relative/dir", "absolute"),
    ("./here", "absolute"),
])
def test_rejects_missing_or_relative(raw: str | None, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_work_dir(raw)
