"""``looper-edit`` on a direct session's PATH: a private link to the installed console script, never in a place the
sandbox can write or cannot read."""
import os
import stat
import sys

import pytest

from looper.agents import task_manager_edit, task_manager_safety as safety
from tests.test_task_manager_launch import launch


@pytest.fixture
def interpreter(tmp_path, monkeypatch):
    """A scratch interpreter directory holding an executable ``looper-edit`` console script."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "looper-edit"
    script.write_text("#!/bin/sh\necho edit\n")
    script.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
    return script


def test_edit_bin_links_the_console_script_idempotently_and_repairs_a_wrong_target(tmp_path, interpreter):
    environment = {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    directory = task_manager_edit.edit_bin(environment, forbidden=[str(tmp_path / "task")])
    assert directory == tmp_path / "cache" / "looper" / "task-manager-bin"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert os.listdir(directory) == ["looper-edit"] and os.readlink(directory / "looper-edit") == str(interpreter)
    before = (directory / "looper-edit").lstat().st_ino
    assert task_manager_edit.edit_bin(environment, forbidden=[]) == directory
    assert (directory / "looper-edit").lstat().st_ino == before  # unchanged link: not replaced
    (directory / "looper-edit").unlink()
    (directory / "looper-edit").symlink_to(tmp_path / "elsewhere")
    task_manager_edit.edit_bin(environment, forbidden=[])
    assert os.readlink(directory / "looper-edit") == str(interpreter) and os.listdir(directory) == ["looper-edit"]
    # Without XDG_CACHE_HOME the directory is under HOME/.cache.
    assert task_manager_edit.edit_bin({"HOME": str(tmp_path / "home")}, forbidden=[]) == (
        tmp_path / "home" / ".cache" / "looper" / "task-manager-bin")


def test_a_missing_console_script_is_an_installation_defect(tmp_path, interpreter):
    interpreter.unlink()
    with pytest.raises(RuntimeError) as failure:
        task_manager_edit.edit_bin({"HOME": str(tmp_path)}, forbidden=[])
    assert str(failure.value) == f"looper-edit is not installed next to {sys.executable}; reinstall Looper"
    assert not (tmp_path / ".cache").exists()


@pytest.mark.parametrize("place", ["task", "tmpdir", "read_deny"])
def test_a_directory_the_sandbox_can_write_or_cannot_read_is_refused(tmp_path, interpreter, place):
    roots = {"task": tmp_path / "task", "tmpdir": tmp_path / "tmp", "read_deny": tmp_path / "runtime"}
    for root in roots.values():
        root.mkdir()
    with pytest.raises(RuntimeError, match="which a direct session can write or cannot read"):
        task_manager_edit.edit_bin({"HOME": str(tmp_path), "XDG_CACHE_HOME": str(roots[place] / "cache")},
                                   forbidden=[str(root) for root in roots.values()])
    assert not (roots[place] / "cache").exists()


def test_environment_puts_the_edit_dir_first_only_when_given(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    run = launch()
    plain = safety.environment(run, "task-1")
    assert plain["PATH"] == run.runtime.environment(run)["PATH"]
    edited = safety.environment(run, "task-1", edit_dir=tmp_path / "edit-bin")
    assert edited["PATH"] == f"{tmp_path / 'edit-bin'}{os.pathsep}{plain['PATH']}"
    assert {key: value for key, value in edited.items() if key != "PATH"} == {
        key: value for key, value in plain.items() if key != "PATH"}


def test_a_console_script_inside_a_forbidden_root_is_refused_before_anything_is_created(tmp_path, interpreter):
    environment = {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    with pytest.raises(RuntimeError, match="console script .* is under"):  # the interpreter's own tree
        task_manager_edit.edit_bin(environment, forbidden=[str(tmp_path / "venv")])
    link = tmp_path / "linked-venv"
    link.symlink_to(tmp_path / "venv")  # a forbidden root reached through a symlink is the same place
    with pytest.raises(RuntimeError, match="console script"):
        task_manager_edit.edit_bin(environment, forbidden=[str(link)])
    assert not (tmp_path / "cache").exists()
    task_manager_edit.edit_bin(environment, forbidden=[str(tmp_path / "task")])  # a root elsewhere is fine
