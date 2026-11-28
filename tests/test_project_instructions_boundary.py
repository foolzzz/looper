"""Project instruction files that Looper reads itself (the ``INJECTED`` ones of
``looper/agents/runtimes/instructions.py``) are read only from inside the repository, or from inside the work
directory outside a repository, and a link is followed only to a file with an instruction file name.

Every "outside" or private file here is a fake one under ``tmp_path``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import tracemalloc
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from looper.agents import task_manager_sandbox
from looper.agents.agent_settings import AgentType
from looper.agents.runtimes import adapter, dsh, dsh_runner, instructions, omp, pi, pi_runner
from looper.agents.runtimes.instructions import INJECTED_MAX_BYTES, record
from tests.test_project_instructions import LOOPER_PROMPT, _launch, _runtime_prompt

OUTSIDE_TEXT = "FAKE-OUTSIDE-CONTENT-0123456789"
# Every place a system prompt is assembled with injected files, and the runtime whose files it takes. omp injects
# only outside a repository.
ASSEMBLERS = [(dsh, "dsh", True), (dsh_runner, "dsh", True), (pi, "pi", True), (pi_runner, "pi", True),
              (dsh, "dsh", False), (pi, "pi", False), (omp, "omp", False)]
ASSEMBLER_IDS = [f"{module.__name__.rsplit('.', 1)[1]}-{'repository' if repo else 'directory'}"
                 for module, _, repo in ASSEMBLERS]


def _work(tmp_path: Path, repository: bool = True) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    if repository:
        (work / ".git").mkdir()
    return work


def _outside(tmp_path: Path, text: str = OUTSIDE_TEXT) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    target = outside / "fixture-target"
    target.write_text(text)
    return target


def _relative_link(work: Path, target: Path) -> None:
    (work / "AGENTS.md").symlink_to(os.path.relpath(target, work))


def _absolute_link(work: Path, target: Path) -> None:
    (work / "AGENTS.md").symlink_to(target)


def _hard_link(work: Path, target: Path) -> None:
    os.link(target, work / "AGENTS.md")


LINKS = [(_relative_link, "outside"), (_absolute_link, "outside"), (_hard_link, "hard_linked")]
LINK_IDS = ["relative symlink", "absolute symlink", "hard link"]


def _prompt(module: Any, runtime: str, cwd: Path, **launch: Any) -> str:
    return module._prompt_text(LOOPER_PROMPT, replace(_launch(runtime), **launch), cwd=cwd)


def _entries(runtime: str, cwd: Path) -> list[dict[str, Any]]:
    return record(adapter(runtime).instruction_files(cwd), cwd)


def _skipped(path: str, reason: str) -> dict[str, Any]:
    return {"path": path, "delivery": "injected", "skipped": reason, "error": instructions.SKIPPED[reason]}


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


@pytest.mark.parametrize("module, runtime, repository", ASSEMBLERS, ids=ASSEMBLER_IDS)
@pytest.mark.parametrize("link, reason", LINKS, ids=LINK_IDS)
def test_a_link_to_a_file_outside_is_not_given_to_the_runtime(
    tmp_path: Path, module: Any, runtime: str, repository: bool, link: Any, reason: str,
) -> None:
    work = _work(tmp_path, repository)
    link(work, _outside(tmp_path))
    before = _open_fds()

    prompt = _prompt(module, runtime, work)
    entries = _entries(runtime, work)

    assert OUTSIDE_TEXT not in prompt and "# Project instructions" not in prompt
    assert LOOPER_PROMPT in prompt
    assert entries == [_skipped("AGENTS.md", reason)]
    assert _open_fds() == before


@pytest.mark.parametrize("runtime", ["pi", "omp"])
def test_the_launched_prompt_file_does_not_carry_a_linked_outside_file(tmp_path: Path, runtime: str) -> None:
    work = _work(tmp_path, repository=False)
    _absolute_link(work, _outside(tmp_path))
    assert OUTSIDE_TEXT not in _runtime_prompt(runtime, work, tmp_path / "rt")


@pytest.mark.parametrize("module, runtime", [(dsh, "dsh"), (pi, "pi")], ids=["dsh", "pi"])
def test_a_directory_on_the_way_that_links_outside_is_not_followed(tmp_path: Path, module: Any, runtime: str) -> None:
    repo = _work(tmp_path)
    (repo / "AGENTS.md").write_text("ROOT RULES")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "AGENTS.md").write_text(OUTSIDE_TEXT)
    (repo / "pkg").symlink_to(elsewhere)

    prompt = _prompt(module, runtime, repo / "pkg")

    assert "ROOT RULES" in prompt and OUTSIDE_TEXT not in prompt
    assert _entries(runtime, repo / "pkg")[1] == _skipped("AGENTS.md", "outside")


@pytest.mark.parametrize("module, runtime, repository", ASSEMBLERS, ids=ASSEMBLER_IDS)
def test_a_link_to_another_file_inside_is_still_given(
    tmp_path: Path, module: Any, runtime: str, repository: bool,
) -> None:
    work = _work(tmp_path, repository)
    (work / "CLAUDE.md").write_text("ALIASED RULES")
    (work / "AGENTS.md").symlink_to("CLAUDE.md")

    assert "ALIASED RULES" in _prompt(module, runtime, work)
    (entry,) = _entries(runtime, work)
    assert entry["path"] == "AGENTS.md" and entry["bytes"] == len("ALIASED RULES") and "skipped" not in entry


@pytest.mark.parametrize("name, target, given", [
    ("AGENTS.md", "../docs/AGENTS.md", 1),
    ("CLAUDE.md", "../AGENTS.md", 2),  # the root's own file, and again through the link below it
], ids=["a directory's AGENTS.md", "the parent's AGENTS.md"])
@pytest.mark.parametrize("module, runtime", [(dsh, "dsh"), (pi, "pi")], ids=["dsh", "pi"])
def test_below_the_root_a_link_up_to_an_instruction_file_of_the_repository_is_still_given(
    tmp_path: Path, module: Any, runtime: str, name: str, target: str, given: int,
) -> None:
    repo = _work(tmp_path)
    (repo / "pkg").mkdir()
    shared = repo / "pkg" / target
    shared.parent.mkdir(exist_ok=True)
    shared.write_text("SHARED RULES")
    (repo / "pkg" / name).symlink_to(target)

    assert _prompt(module, runtime, repo / "pkg").count("SHARED RULES") == given
    entry = _entries(runtime, repo / "pkg")[-1]
    assert entry["path"] == name and entry["bytes"] == len("SHARED RULES") and "skipped" not in entry


def _assert_not_followed(module: Any, runtime: str, cwd: Path, private: str) -> None:
    before = _open_fds()

    prompt = _prompt(module, runtime, cwd)
    entries = _entries(runtime, cwd)

    assert private not in prompt and "# Project instructions" not in prompt
    assert LOOPER_PROMPT in prompt
    assert private not in repr(entries)
    assert entries == [_skipped("AGENTS.md", "not_an_instruction_file")]
    assert _open_fds() == before


@pytest.mark.parametrize("module, runtime, repository", [case for case in ASSEMBLERS if case[2]],
                         ids=[name for name, case in zip(ASSEMBLER_IDS, ASSEMBLERS) if case[2]])
def test_a_link_to_the_repositorys_git_config_is_not_followed(
    tmp_path: Path, module: Any, runtime: str, repository: bool,
) -> None:
    work = _work(tmp_path, repository)
    (work / ".git" / "config").write_text("FAKE-GIT-CONFIG-CONTENT")
    (work / "AGENTS.md").symlink_to(".git/config")

    _assert_not_followed(module, runtime, work, "FAKE-GIT-CONFIG-CONTENT")


@pytest.mark.parametrize("module, runtime, repository", ASSEMBLERS, ids=ASSEMBLER_IDS)
def test_a_link_to_an_env_file_inside_is_not_followed(
    tmp_path: Path, module: Any, runtime: str, repository: bool,
) -> None:
    work = _work(tmp_path, repository)
    (work / ".env").write_text("FAKE-ENV-CONTENT")
    (work / "AGENTS.md").symlink_to(".env")

    _assert_not_followed(module, runtime, work, "FAKE-ENV-CONTENT")


@pytest.mark.parametrize("module, runtime", [(dsh, "dsh"), (pi, "pi")], ids=["dsh", "pi"])
def test_below_a_parent_repository_a_link_up_to_another_file_of_it_is_not_followed(
    tmp_path: Path, module: Any, runtime: str,
) -> None:
    parent = _work(tmp_path)  # the nearest .git above the work directory, so the boundary is this whole directory
    (parent / "fixture-private").write_text("FAKE-PARENT-CONTENT")
    work = parent / "tasks" / "T1"
    work.mkdir(parents=True)
    (work / "AGENTS.md").symlink_to("../../fixture-private")

    _assert_not_followed(module, runtime, work, "FAKE-PARENT-CONTENT")


@pytest.mark.parametrize("module, runtime", [(dsh, "dsh"), (pi, "pi")], ids=["dsh", "pi"])
def test_a_chain_through_an_instruction_name_is_judged_by_where_it_ends(
    tmp_path: Path, module: Any, runtime: str,
) -> None:
    work = _work(tmp_path)
    (work / ".env").write_text("FAKE-ENV-CONTENT")
    (work / "CLAUDE.md").symlink_to(".env")
    (work / "AGENTS.md").symlink_to("CLAUDE.md")

    _assert_not_followed(module, runtime, work, "FAKE-ENV-CONTENT")


def test_a_work_directory_reached_through_a_link_still_gives_its_file(tmp_path: Path) -> None:
    work = _work(tmp_path, repository=False)
    (work / "AGENTS.md").write_text("DIRECTORY RULES")
    (tmp_path / "alias").symlink_to(work)
    assert "DIRECTORY RULES" in _prompt(pi, "pi", tmp_path / "alias")


def test_no_outside_file_is_opened_for_the_prompt_or_the_record(tmp_path: Path) -> None:
    work = _work(tmp_path)
    target = _outside(tmp_path)
    _absolute_link(work, target)
    opened: list[str] = []
    watching = True

    def watch(event: str, args: tuple[Any, ...]) -> None:
        if watching and event == "open" and isinstance(args[0], str):
            opened.append(args[0])

    sys.addaudithook(watch)  # a hook cannot be removed; it stops collecting below
    try:
        _prompt(pi, "pi", work)
        _entries("pi", work)
    finally:
        watching = False

    assert [path for path in opened if os.path.realpath(path) == os.path.realpath(target)] == []


def test_a_large_file_is_not_read_whole(tmp_path: Path) -> None:
    work = _work(tmp_path)
    size = 24 * 1024 * 1024
    with open(work / "AGENTS.md", "wb") as handle:
        handle.write(b"LARGE RULES\n")
        handle.truncate(size)

    tracemalloc.start()
    try:
        prompt = _prompt(pi, "pi", work)
        (entry,) = _entries("pi", work)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 4 * 1024 * 1024
    assert "LARGE RULES" in prompt and f"[clipped: {size - INJECTED_MAX_BYTES} more bytes" in prompt
    assert entry["bytes"] == size and entry["clipped_bytes"] == size - INJECTED_MAX_BYTES


def test_a_file_replaced_by_a_fifo_does_not_block(tmp_path: Path) -> None:
    work = _work(tmp_path)
    (work / "AGENTS.md").write_text("RULES")
    files = adapter("pi").instruction_files(work)
    (work / "AGENTS.md").unlink()
    os.mkfifo(work / "AGENTS.md")
    before = _open_fds()

    with _deadline():
        entries = record(files, work)

    assert entries == [_skipped("AGENTS.md", "not_regular")]
    assert _open_fds() == before


def test_a_skipped_file_is_logged_by_path_and_reason_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    work = _work(tmp_path)
    _hard_link(work, _outside(tmp_path))

    with caplog.at_level(logging.WARNING, logger=instructions.__name__):
        _prompt(pi, "pi", work)

    assert "AGENTS.md" in caplog.text and "hard_linked" in caplog.text
    assert OUTSIDE_TEXT not in caplog.text


@pytest.mark.parametrize("repository", [True, False], ids=["repository", "directory"])
@pytest.mark.parametrize("module, runtime", [(dsh, "dsh"), (dsh_runner, "dsh"), (pi, "pi"), (pi_runner, "pi")],
                         ids=["dsh", "dsh runner", "pi", "pi runner"])
@pytest.mark.parametrize("role", [AgentType.PLANNER, AgentType.WORKER], ids=["planner", "worker"])
def test_what_the_sandboxed_task_manager_may_not_read_does_not_reach_another_role_through_the_task_directory(
    tmp_path: Path, role: AgentType, module: Any, runtime: str, repository: bool,
) -> None:
    home, runtime_root = tmp_path / "home", tmp_path / "runtime"
    key = home / ".ssh" / "fixture-key"
    ops = runtime_root / "fixture-ops"
    for path, text in ((key, "FAKE-KEY-FIXTURE"), (ops, "FAKE-OPS-FIXTURE")):
        path.parent.mkdir(parents=True)
        path.write_text(text)
    denied = task_manager_sandbox.read_deny_paths({"HOME": str(home)}, runtime_root)
    for path in (key, ops):
        assert any(Path(os.path.realpath(path)).is_relative_to(entry) for entry in denied), path
    work = _work(tmp_path, repository)
    # What a turn that may write only the task directory can leave there for the next role to read.
    (work / "AGENTS.md").symlink_to(key)
    (work / "pkg").mkdir()
    (work / "pkg" / "AGENTS.md").symlink_to(ops)

    prompt = _prompt(module, runtime, work / "pkg", role=role) + _prompt(module, runtime, work, role=role)

    assert "FAKE-KEY-FIXTURE" not in prompt and "FAKE-OPS-FIXTURE" not in prompt
    assert _entries(runtime, work) == [_skipped("AGENTS.md", "outside")]
    assert all(entry["skipped"] == "outside" for entry in _entries(runtime, work / "pkg"))
