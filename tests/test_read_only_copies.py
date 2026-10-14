"""Reclaiming the read-only copies under the runtime root when ``loopx serve`` starts.

The library's items, a project's stored knowledge and a project's skills are
written once under a content hash; a change writes a new directory. At server
start the ones neither the current content nor a running process uses are
removed. The database is stubbed; the running processes are real.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from loopx.agents import library, read_only_copies
from loopx.agents.knowledge import load_knowledge
from loopx.agents.project_skills import load_project_skills
from loopx.agents.read_only_copies import hashed_directories, reclaim_unused
from loopx.db import library as db_library
from loopx.db import project_documents as db_documents
from loopx.db import project_skills as db_skills
from loopx.db import projects as db_projects


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Content:
    """The database's current content: projects, their documents and skills, and the enabled library items."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.projects: list[dict[str, Any]] = []
        self.documents: dict[str, list[tuple[str, str]]] = {}  # project id -> (title, content)
        self.skills: dict[str, list[tuple[str, str]]] = {}  # project id -> (name, body)
        self.fail = False

        async def list_projects(**_: Any) -> list[dict[str, Any]]:
            if self.fail:
                raise ConnectionError("database is down")
            return [dict(project) for project in self.projects]

        async def list_documents(project_id: str, **_: Any) -> list[dict[str, Any]]:
            return [{"id": f"doc-{project_id}-{n}", "project_id": project_id, "title": title, "summary": "s",
                     "source_kind": "text", "filename": f"{title}.md", "path": None, "size_bytes": len(content),
                     "sha256": _sha(content), "created_at": None, "updated_at": None}
                    for n, (title, content) in enumerate(self.documents.get(project_id, []))]

        async def get_content(document_id: str, **_: Any) -> str:
            return next(content for project_id, rows in self.documents.items()
                        for n, (_, content) in enumerate(rows) if f"doc-{project_id}-{n}" == document_id)

        async def list_enabled_skills(project_id: str, **_: Any) -> list[dict[str, Any]]:
            return [{"id": f"skill-{n}", "name": name, "description": "d", "content": body}
                    for n, (name, body) in enumerate(self.skills.get(project_id, []))]

        async def no_items(**_: Any) -> list[dict[str, Any]]:
            return []

        monkeypatch.setattr(db_projects, "list_projects", list_projects)
        monkeypatch.setattr(db_documents, "list_documents", list_documents)
        monkeypatch.setattr(db_documents, "get_content", get_content)
        monkeypatch.setattr(db_skills, "list_enabled_skills", list_enabled_skills)
        monkeypatch.setattr(db_library, "list_enabled_items", no_items)

    def project(self, project_id: str, *, documents: list[tuple[str, str]], skills: list[tuple[str, str]]) -> dict:
        project = {"id": project_id, "name": project_id, "work_dir": ""}
        self.projects = [p for p in self.projects if p["id"] != project_id] + [project]
        self.documents[project_id], self.skills[project_id] = documents, skills
        return project

    def run(self, root: Path, project: dict[str, Any]) -> tuple[str, str]:
        """What one run of the project mounts now: its knowledge set and its skills plugin."""
        knowledge = asyncio.run(load_knowledge(root, project, str(root)))
        skills = asyncio.run(load_project_skills(root, project))
        assert skills.plugin_dir is not None
        return knowledge.add_dirs[0], skills.plugin_dir


@pytest.fixture
def content(monkeypatch: pytest.MonkeyPatch) -> _Content:
    return _Content(monkeypatch)


def _hashed(path: str | Path) -> Path:
    """The content-hashed directory ``path`` is in."""
    path = Path(path)
    while len(path.name) != 64:
        path = path.parent
    return path


def _library_item(root: Path, name: str, body: str) -> library.LoadedItem:
    files = [{"path": "SKILL.md", "content": body, "sha256": _sha(body), "executable": False}]
    item = library.LoadedItem(library.ItemKind.SKILL, name, library.item_sha256(files))
    library.materialize_item(root, item, files)
    return item


def test_server_start_keeps_the_current_copies_and_removes_replaced_and_deleted_ones(
    tmp_path: Path, content: _Content,
) -> None:
    calc = content.project("proj-calc", documents=[("Glossary", "v1")], skills=[("deploy", "Deploy v1.")])
    old_knowledge, old_skills = content.run(tmp_path, calc)
    gone = content.project("proj-gone", documents=[("Notes", "n")], skills=[("lint", "Lint.")])
    gone_copies = content.run(tmp_path, gone)
    unused_item = _library_item(tmp_path, "retired", "Retired skill.")
    # The calc documents and skills change, the other project is deleted, the library item disabled.
    content.project("proj-calc", documents=[("Glossary", "v2")], skills=[("deploy", "Deploy v2.")])
    content.projects = [calc]

    removed = asyncio.run(reclaim_unused(tmp_path))

    current = content.run(tmp_path, calc)  # nothing to write again: what the next run mounts is there
    assert sorted(hashed_directories(tmp_path)) == sorted(_hashed(path) for path in current)
    assert sorted(removed) == sorted([_hashed(old_knowledge), _hashed(old_skills), *map(_hashed, gone_copies),
                                      tmp_path / ".local" / "library" / unused_item.sha256])
    assert not (tmp_path / ".local" / "projects" / "proj-gone").exists()  # its emptied directories go too


def test_a_copy_a_running_process_mounts_or_links_into_is_kept_until_it_exits(
    tmp_path: Path, content: _Content,
) -> None:
    """The proof that a reclaim never removes a directory a live agent has mounted."""
    calc = content.project("proj-calc", documents=[("Glossary", "v1")], skills=[("deploy", "Deploy v1.")])
    knowledge_set, skills_plugin = content.run(tmp_path, calc)
    skill = _library_item(tmp_path, "alpha", "Alpha skill.")
    personal = library.skills_plugin(tmp_path, [skill])  # links into the skill's own directory
    unrelated = _library_item(tmp_path, "beta", "Beta skill.")
    content.projects = []  # nothing is current: only the process keeps them
    mounted = {_hashed(knowledge_set), _hashed(skills_plugin), _hashed(personal),
               tmp_path / ".local" / "library" / skill.sha256}

    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--plugin-dir", str(personal),
                              "--plugin-dir", skills_plugin, "--add-dir", knowledge_set])
    try:
        deadline = time.monotonic() + 10
        while not any(str(personal) in line for line in read_only_copies.command_lines()):
            assert time.monotonic() < deadline, "the process did not show up"
            time.sleep(0.05)
        removed = asyncio.run(reclaim_unused(tmp_path))
        assert removed == [tmp_path / ".local" / "library" / unrelated.sha256]
        assert set(hashed_directories(tmp_path)) == mounted
        assert (Path(personal) / "skills" / "alpha" / "SKILL.md").read_text() == "Alpha skill."
    finally:
        agent.kill()
        agent.wait()
    assert set(asyncio.run(reclaim_unused(tmp_path))) == mounted
    assert hashed_directories(tmp_path) == []


def test_a_process_that_mounts_a_whole_knowledge_directory_keeps_every_set_in_it(
    tmp_path: Path, content: _Content,
) -> None:
    """Before a directory per set, a run mounted the project's whole knowledge directory."""
    calc = content.project("proj-calc", documents=[("Glossary", "v1")], skills=[])
    first = _hashed(asyncio.run(load_knowledge(tmp_path, calc, str(tmp_path))).add_dirs[0])
    content.project("proj-calc", documents=[("Glossary", "v2")], skills=[])
    second = _hashed(asyncio.run(load_knowledge(tmp_path, calc, str(tmp_path))).add_dirs[0])
    content.projects = []
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--add-dir", str(first.parent)])
    try:
        deadline = time.monotonic() + 10
        while not any(str(first.parent) in line for line in read_only_copies.command_lines()):
            assert time.monotonic() < deadline, "the process did not show up"
            time.sleep(0.05)
        assert asyncio.run(reclaim_unused(tmp_path)) == []
        assert set(hashed_directories(tmp_path)) == {first, second}
    finally:
        agent.kill()
        agent.wait()


@pytest.mark.parametrize("unreadable", ["processes", "content"])
def test_nothing_is_reclaimed_when_the_processes_or_the_content_cannot_be_read(
    tmp_path: Path, content: _Content, monkeypatch: pytest.MonkeyPatch, unreadable: str,
) -> None:
    _library_item(tmp_path, "retired", "Retired skill.")
    if unreadable == "processes":
        monkeypatch.setattr(read_only_copies.subprocess, "run", lambda *_, **__: (_ for _ in ()).throw(
            FileNotFoundError("ps")))
    else:
        content.fail = True
    assert asyncio.run(reclaim_unused(tmp_path)) == []
    assert len(hashed_directories(tmp_path)) == 1
