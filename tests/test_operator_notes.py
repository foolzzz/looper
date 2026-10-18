"""The operator's notes for a directory: where Claude Code keeps them, what a run gets, and that Looper only reads.

The notes root is a temporary directory standing in for ``~/.claude/projects``
(``LOOPER_OPERATOR_NOTES_ROOT``); the directories are real git repositories.
The directory names were checked against Claude Code 2.1.285, which reports
the auto-memory path it uses in its init event (``memory_paths.auto``).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path

import pytest

from looper.agents import operator_notes as notes
from looper.agents.knowledge import AgentKnowledge, load_knowledge
from looper.agents.operator_notes import NOTES_MAX_BYTES, OperatorNotes, directory_name, notes_key, read_notes


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "claude-projects"
    root.mkdir()
    monkeypatch.setenv(notes.NOTES_ROOT_ENV, str(root))
    return root


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=U", "-c", "user.email=u@x", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "sub").mkdir()
    (path / "sub" / "a.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


def memory(root: Path, key: str, files: dict[str, str]) -> Path:
    directory = root / directory_name(key) / "memory"
    directory.mkdir(parents=True)
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


LONG = ("/private/tmp/claude-501/-Users-bytedance-persona-myloopx/79a1ad79-ca6f-4d93-a6e3-9702fd857d03/scratchpad/"
        "lane-workdir-i2/ccmem/plain/" + "a" * 120 + "/" + "b" * 60)


@pytest.mark.parametrize(("key", "name"), [
    ("/Users/me/my.repo_x", "-Users-me-my-repo-x"),
    ("/private/tmp/claude-501/-Users-me-proj", "-private-tmp-claude-501--Users-me-proj"),
    # Past 200 characters: the first 200, then the key's djb2 hash in base 36, as Claude Code 2.1.285 named it.
    (LONG, re.sub(r"[^a-zA-Z0-9]", "-", LONG)[:200] + "-hkty31"),
])
def test_a_directory_is_named_as_claude_code_names_it(key: str, name: str) -> None:
    assert directory_name(key) == name


def test_the_key_is_the_main_checkout_for_a_subdirectory_and_every_worktree(tmp_path: Path) -> None:
    main = repo(tmp_path / "main")
    git(main, "worktree", "add", "-q", "-b", "side", str(tmp_path / "other"))
    (tmp_path / "link").symlink_to(main)
    plain = tmp_path / "plain"
    plain.mkdir()
    real = os.path.realpath(main)
    assert {notes_key(str(d)) for d in (main, main / "sub", tmp_path / "other", tmp_path / "other" / "sub",
                                        tmp_path / "link")} == {real}
    assert notes_key(str(plain)) == os.path.realpath(plain)


def test_a_run_gets_the_index_and_the_notes_it_links_in_its_order(tmp_path: Path, _env: Path) -> None:
    main = repo(tmp_path / "main")
    memory(_env, os.path.realpath(main), {
        "MEMORY.md": "- [Deploy](deploy.md) — the staging port\n- [Style](style.md) — reviews\n"
                     "- [Gone](missing.md) — no such file\n- [Out](../../secret.md) — outside\n- [Text](notes.txt)\n",
        "deploy.md": "---\nname: deploy\n---\nStaging listens on 59322.\n",
        "style.md": "Keep functions short.\n",
        "unlinked.md": "Not in the index.\n",
    })
    found = read_notes(str(main / "sub"))
    assert found.files == ("MEMORY.md", "deploy.md", "style.md") and found.omitted == ("missing.md",)
    assert found.text.startswith(notes.HEADING + "\n" + notes.LEAD)
    assert found.text.index("### deploy.md") < found.text.index("### style.md")
    assert "Staging listens on 59322." in found.text and "Not in the index" not in found.text
    assert found.text.rstrip().endswith("not readable): missing.md")
    assert found.record() == {"sha256": found.sha256, "files": ["MEMORY.md", "deploy.md", "style.md"],
                              "omitted": ["missing.md"]}


def test_the_notes_stay_within_their_limit(tmp_path: Path, _env: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    index = "\n".join(f"- [Note {i}](n{i}.md)" for i in range(300))
    memory(_env, os.path.realpath(plain), {"MEMORY.md": index,
                                           **{f"n{i}.md": "x" * 3000 for i in range(300)}})
    found = read_notes(str(plain))
    assert len(found.text.encode("utf-8")) <= NOTES_MAX_BYTES
    assert "[Note 199]" in found.text and "[Note 200]" not in found.text  # as much of the index as Claude Code loads
    assert found.files[0] == "MEMORY.md" and found.omitted and set(found.files[1:]).isdisjoint(found.omitted)


def test_a_note_that_fits_only_without_its_heading_is_left_out(tmp_path: Path, _env: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    index = "- [A](a.md)"
    used = len("\n".join([notes.HEADING, notes.LEAD, "", f"### {notes.INDEX}", index]).encode("utf-8"))
    memory(_env, os.path.realpath(plain), {"MEMORY.md": index, "a.md": "y" * (NOTES_MAX_BYTES - used)})
    found = read_notes(str(plain))
    assert found.files == ("MEMORY.md",) and found.omitted == ("a.md",)
    assert len(found.text.split("\nNot included")[0].encode("utf-8")) <= NOTES_MAX_BYTES


def test_a_directory_without_notes_has_none(tmp_path: Path, _env: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert read_notes(str(plain)) == OperatorNotes()
    memory(_env, os.path.realpath(plain), {"MEMORY.md": "\n\n"})
    assert read_notes(str(plain)) == OperatorNotes()


def test_looper_only_reads_the_notes(tmp_path: Path, _env: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    directory = memory(_env, os.path.realpath(plain), {"MEMORY.md": "- [A](a.md)\n", "a.md": "alpha\n"})
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in directory.iterdir()}
    subprocess.run(["chmod", "-R", "a-w", str(_env)], check=True)
    try:
        assert read_notes(str(plain)).files == ("MEMORY.md", "a.md")
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(_env)], check=True)
    assert {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in directory.iterdir()} == before


def test_the_notes_come_with_the_knowledge_only_when_the_project_gives_them(tmp_path: Path, _env: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import project_documents

    async def no_documents(project_id: str) -> list[dict]:
        return []

    monkeypatch.setattr(project_documents, "list_documents", no_documents)
    plain = tmp_path / "plain"
    plain.mkdir()
    memory(_env, os.path.realpath(plain), {"MEMORY.md": "Staging listens on 59322.\n"})
    project = {"id": "proj-1", "work_dir": str(plain)}
    off = asyncio.run(load_knowledge(tmp_path, project, str(plain)))
    on = asyncio.run(load_knowledge(tmp_path, project, str(plain), notes=True))
    assert off == AgentKnowledge() and off.prompt_section == ""
    assert on.notes is not None and on.prompt_section == on.notes.text and "59322" in on.prompt_section
    assert asyncio.run(load_knowledge(tmp_path, None, str(plain), notes=True)) == AgentKnowledge()


# --- The agents get them only from a project that gives them ---

NOTES = OperatorNotes(text=f"{notes.HEADING}\nNOTE-MARK-59322", files=("MEMORY.md",))


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The directories the notes were read for; every read answers ``NOTES``."""
    from looper.agents import knowledge

    seen: list[str] = []

    async def load(work_dir: str) -> OperatorNotes:
        seen.append(work_dir)
        return NOTES

    monkeypatch.setattr(knowledge, "load_notes", load)
    return seen


@pytest.mark.usefixtures("agent_db")
@pytest.mark.parametrize(("setting", "given"), [("on", True), ("off", False), (None, True)])
def test_the_planner_gets_them_unless_its_project_turns_them_off(planner_claude: object, agent_db: object,
                                                              asked: list[str], setting: str | None,
                                                              given: bool) -> None:
    from test_planner_agent import WORK_OK, decide, drive, plan, step

    agent_db.get_task_project.return_value = {  # type: ignore[attr-defined]
        "id": "proj-1", "name": "calc", "description": "", "work_dir": "/srv/calc", "background": "",
        "operator_notes": setting}
    claude = planner_claude(plan(step("s1")), decide("accept"))  # type: ignore[operator]
    drive(claude, lambda envelope: WORK_OK)
    prompts = claude.prompts()
    assert prompts and all(("NOTE-MARK-59322" in prompt) is given for prompt in prompts)
    assert bool(asked) is given


@pytest.mark.usefixtures("agent_db")
@pytest.mark.parametrize(("setting", "given"), [("on", True), ("off", False), (None, True)])
def test_a_worker_gets_them_unless_its_project_turns_them_off(agent_db: object, asked: list[str],
                                                            monkeypatch: pytest.MonkeyPatch, setting: str | None,
                                                            given: bool) -> None:
    from tests.test_agent_planner_worker import _run_step, _worker_row

    _worker_row(monkeypatch, {"system_prompt": "ROLE\n", "timeout_seconds": 0, "max_turns": 0})
    agent_db.get_task_project.return_value = {  # type: ignore[attr-defined]
        "id": "proj-1", "name": "calc", "background": "", "operator_notes": setting}
    result, seen = _run_step()
    assert result["status"] == "completed"
    assert ("NOTE-MARK-59322" in seen["prompt"]) is given and (asked == [seen["work_dir"]]) is given


@pytest.mark.parametrize("content", ["x" * 30000, "记" * 10000, "[Missing](missing.md)\n" + "x" * 30000])
def test_the_index_and_footer_share_the_notes_byte_budget(tmp_path: Path, _env: Path, content: str) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    memory(_env, os.path.realpath(plain), {"MEMORY.md": content})
    found = read_notes(str(plain))
    assert found.files == ("MEMORY.md",)
    assert len(found.text.encode("utf-8")) <= NOTES_MAX_BYTES
    assert "�" not in found.text
    if "missing.md" in content:
        assert found.omitted == ("missing.md",) and "Not included" in found.text


def test_a_symlinked_index_cannot_escape_the_memory_directory(tmp_path: Path, _env: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    outside = tmp_path / "private.md"
    outside.write_text("private content")
    directory = memory(_env, os.path.realpath(plain), {})
    (directory / "MEMORY.md").symlink_to(outside)
    assert read_notes(str(plain)) == OperatorNotes()
    (directory / "MEMORY.md").unlink()
    (directory / "inside.md").write_text("contained notes")
    (directory / "MEMORY.md").symlink_to(directory / "inside.md")
    assert "contained notes" in read_notes(str(plain)).text
