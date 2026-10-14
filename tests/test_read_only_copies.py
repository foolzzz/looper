"""Reclaiming the read-only copies under the runtime root when ``looper serve`` starts.

The catalog's items and generated skill plugins and a project's stored
knowledge are written once under a content hash; a change writes a new
directory. At server start the ones neither the current content (what every
role in every project loads) nor a running process uses are removed, and so
are the session directories of resident agents whose saved session is not of
that runtime. The database is stubbed; the running processes are real.
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

from looper.agents import library, mcp, read_only_copies
from looper.agents.knowledge import load_knowledge
from looper.agents.project_skills import skill_file
from looper.agents.read_only_copies import hashed_directories, reclaim_unused
from looper.db import agent_sessions as db_agent_sessions
from looper.db import library as db_library
from looper.db import project_documents as db_documents
from looper.db import projects as db_projects


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Content:
    """The database's current content: projects, their documents and the skills they wrote, installed there."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.projects: list[dict[str, Any]] = []
        self.documents: dict[str, list[tuple[str, str]]] = {}  # project id -> (title, content)
        self.skills: dict[str, list[tuple[str, str]]] = {}  # project id -> (name, body)
        self.servers: dict[str, mcp.Server] = {}  # MCP servers installed globally for Workers, by name
        self.sessions: list[tuple[str, str | None]] = []  # each agent with a saved session, and its runtime type
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

        def files(project_id: str, n: int) -> list[dict[str, Any]]:
            name, body = self.skills[project_id][n]
            text = skill_file(name, "d", body)
            return [{"path": "SKILL.md", "content": text, "sha256": _sha(text), "executable": False}]

        async def list_installed(role: str, project_id: str | None, **_: Any) -> list[dict[str, Any]]:
            servers = [{"id": f"mcp-{name}", "kind": "mcp", "name": name, "origin": "ui", "owner_project_id": None,
                        "sha256": server.sha256, "scope": "global"}
                       for name, server in self.servers.items()] if role == "worker" else []
            if role == "manager" or project_id is None:
                return servers
            return [*servers, *({"id": f"skill-{project_id}-{n}", "kind": "skill", "name": name, "origin": "project",
                                 "owner_project_id": project_id, "sha256": library.item_sha256(files(project_id, n)),
                                 "scope": "project"} for n, (name, _) in enumerate(self.skills.get(project_id, [])))]

        async def get_mcp_servers(item_ids: list[str], **_: Any) -> dict[str, dict[str, Any]]:
            return {f"mcp-{name}": server.row() for name, server in self.servers.items()
                    if f"mcp-{name}" in item_ids}

        async def get_item_files(item_id: str, **_: Any) -> list[dict[str, Any]]:
            project_id, n = item_id.removeprefix("skill-").rsplit("-", 1)
            return files(project_id, int(n))

        monkeypatch.setattr(db_projects, "list_projects", list_projects)
        monkeypatch.setattr(db_documents, "list_documents", list_documents)
        monkeypatch.setattr(db_documents, "get_content", get_content)
        monkeypatch.setattr(db_library, "list_installed", list_installed)
        monkeypatch.setattr(db_library, "get_item_files", get_item_files)
        monkeypatch.setattr(db_library, "get_mcp_servers", get_mcp_servers)

        async def saved_sessions(**_: Any) -> list[tuple[str, str | None]]:
            return list(self.sessions)

        monkeypatch.setattr(db_agent_sessions, "saved_sessions", saved_sessions)

    def project(self, project_id: str, *, documents: list[tuple[str, str]], skills: list[tuple[str, str]]) -> dict:
        project = {"id": project_id, "name": project_id, "work_dir": ""}
        self.projects = [p for p in self.projects if p["id"] != project_id] + [project]
        self.documents[project_id], self.skills[project_id] = documents, skills
        return project

    def run(self, root: Path, project: dict[str, Any]) -> tuple[str, ...]:
        """What one task agent run of the project mounts now: its knowledge set, its skills (each the catalog
        item's own copy) and the project plugin linking them."""
        knowledge = asyncio.run(load_knowledge(root, project, str(root)))
        installed = asyncio.run(library.load_installed(root, "worker", project["id"]))
        (plugin,) = installed.plugin_dirs
        skills = [str(library.item_directory(root, item)) for item in installed.items]
        return knowledge.add_dirs[0], *skills, plugin


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
    old_copies = content.run(tmp_path, calc)
    gone = content.project("proj-gone", documents=[("Notes", "n")], skills=[("lint", "Lint.")])
    gone_copies = content.run(tmp_path, gone)
    unused_item = _library_item(tmp_path, "retired", "Retired skill.")
    # A project's skills plugin as versions before the catalog wrote it.
    old_layout = tmp_path / ".local" / "projects" / "proj-calc" / "skills" / ("e" * 64)
    (old_layout / "project").mkdir(parents=True)
    # The calc documents and skills change, the other project is deleted, the catalog item uninstalled.
    content.project("proj-calc", documents=[("Glossary", "v2")], skills=[("deploy", "Deploy v2.")])
    content.projects = [calc]

    removed = asyncio.run(reclaim_unused(tmp_path))

    current = content.run(tmp_path, calc)  # nothing to write again: what the next run mounts is there
    assert sorted(hashed_directories(tmp_path)) == sorted({_hashed(path) for path in current})
    assert sorted(removed) == sorted({*map(_hashed, old_copies), *map(_hashed, gone_copies), old_layout.absolute(),
                                      tmp_path.absolute() / ".local" / "library" / unused_item.sha256})
    assert not (tmp_path / ".local" / "projects" / "proj-gone").exists()  # its emptied directories go too
    assert not (tmp_path / ".local" / "projects" / "proj-calc" / "skills").exists()


def test_a_copy_a_running_process_mounts_or_links_into_is_kept_until_it_exits(
    tmp_path: Path, content: _Content,
) -> None:
    """The proof that a reclaim never removes a directory a live agent has mounted."""
    calc = content.project("proj-calc", documents=[("Glossary", "v1")], skills=[("deploy", "Deploy v1.")])
    knowledge_set, deploy, skills_plugin = content.run(tmp_path, calc)
    skill = _library_item(tmp_path, "alpha", "Alpha skill.")
    personal = library.skills_plugin(tmp_path, [skill])  # links into the skill's own directory
    unrelated = _library_item(tmp_path, "beta", "Beta skill.")
    content.projects = []  # nothing is current: only the process keeps them
    mounted = {_hashed(knowledge_set), _hashed(skills_plugin), _hashed(deploy), _hashed(personal),
               tmp_path.absolute() / ".local" / "library" / skill.sha256}

    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--plugin-dir", str(personal),
                              "--plugin-dir", skills_plugin, "--add-dir", knowledge_set])
    try:
        deadline = time.monotonic() + 10
        while not any(str(personal) in line for line in read_only_copies.command_lines()):
            assert time.monotonic() < deadline, "the process did not show up"
            time.sleep(0.05)
        removed = asyncio.run(reclaim_unused(tmp_path))
        assert removed == [tmp_path.absolute() / ".local" / "library" / unrelated.sha256]
        assert set(hashed_directories(tmp_path)) == mounted
        assert (Path(personal) / "skills" / "alpha" / "SKILL.md").read_text() == "Alpha skill."
    finally:
        agent.kill()
        agent.wait()
    assert set(asyncio.run(reclaim_unused(tmp_path))) == mounted
    assert hashed_directories(tmp_path) == []


def test_an_mcp_config_a_running_process_mounts_or_the_next_run_uses_is_kept(
    tmp_path: Path, content: _Content,
) -> None:
    def server(marker: str) -> mcp.Server:
        return mcp.Server(mcp.Transport.STDIO, command="python3", args=("server.py",), env=(("MARKER", marker),))

    content.servers = {"marker": server("v1")}
    mounted = Path(asyncio.run(library.load_installed(tmp_path, "worker")).mcp_config or "")
    content.servers = {"marker": server("v2")}  # a new value: the next run's file is another one
    current = Path(asyncio.run(library.load_installed(tmp_path, "worker")).mcp_config or "")
    unused = mcp.write_config(tmp_path, [("old", server("v0").config())])
    assert mounted.parent == current.parent == unused.parent == tmp_path.absolute() / ".local" / "mcp"
    assert len({mounted, current, unused}) == 3

    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--mcp-config", str(mounted),
                              "--strict-mcp-config"])
    try:
        deadline = time.monotonic() + 10
        while not any(str(mounted) in line for line in read_only_copies.command_lines()):
            assert time.monotonic() < deadline, "the process did not show up"
            time.sleep(0.05)
        assert asyncio.run(reclaim_unused(tmp_path)) == [unused]
        assert set(hashed_directories(tmp_path)) == {mounted, current}
        assert '"MARKER": "v1"' in mounted.read_text()
    finally:
        agent.kill()
        agent.wait()
    assert asyncio.run(reclaim_unused(tmp_path)) == [mounted]
    assert hashed_directories(tmp_path) == [current]


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


def test_an_omp_home_is_kept_while_a_running_process_names_it(tmp_path: Path, content: _Content) -> None:
    from looper.agents.runtimes.omp import write_home

    local = tmp_path.absolute() / ".local"
    used, idle = write_home(local, {"providers": {}}, {"a": 1}, None), write_home(local, {"providers": {}}, {"b": 2}, None)
    assert set(hashed_directories(tmp_path)) == {used, idle}
    natives = local / "omp" / "natives"
    (natives / "18.4.3").mkdir(parents=True)
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-p", "--config",
                              str(used / "looper.yml")])
    try:
        deadline = time.monotonic() + 10
        while not any(str(used) in line for line in read_only_copies.command_lines()):
            assert time.monotonic() < deadline, "the process did not show up"
            time.sleep(0.05)
        assert asyncio.run(reclaim_unused(tmp_path)) == [idle]
    finally:
        agent.kill()
        agent.wait()
    assert asyncio.run(reclaim_unused(tmp_path)) == [used]
    assert (natives / "18.4.3").is_dir()  # the native library the homes share stays


def test_a_session_directory_stays_while_its_agent_saved_a_session_of_that_runtime(
    tmp_path: Path, content: _Content,
) -> None:
    local = tmp_path.absolute() / ".local"
    directories = {name: local / runtime / "sessions" / owner for name, (runtime, owner) in {
        "omp-manager": ("omp", "manager"), "omp-planner": ("omp", "planner-task-1"),
        "omp-gone": ("omp", "planner-task-9"), "codex-planner": ("codex", "planner-task-2"),
        "switched": ("codex", "planner-task-1"), "running": ("omp", "planner-task-3"),
    }.items()}
    for directory in directories.values():
        (directory / "state").mkdir(parents=True)
    # The Manager and task-1's Planner resume OMP sessions, task-2's Planner a Codex one; task-9 is gone, and
    # task-1's Planner moved from Codex to OMP. Task-3's Planner has no saved session, but a turn is running.
    content.sessions = [("manager", "omp"), ("planner-task-1", "omp"), ("planner-task-2", "codex")]
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--session-dir",
                              str(directories["running"])])
    try:
        deadline = time.monotonic() + 10
        while not any(str(directories["running"]) in line for line in read_only_copies.command_lines()):
            assert time.monotonic() < deadline, "the process did not show up"
            time.sleep(0.05)
        removed = asyncio.run(reclaim_unused(tmp_path))
    finally:
        agent.kill()
        agent.wait()
    assert sorted(removed) == sorted([directories["omp-gone"], directories["switched"]])
    assert all(directories[name].is_dir() for name in ("omp-manager", "omp-planner", "codex-planner", "running"))


def test_removing_a_session_directory_unlinks_its_login_link_and_leaves_the_login_file(
    tmp_path: Path, content: _Content,
) -> None:
    login = tmp_path / "operator" / "auth.json"
    login.parent.mkdir()
    login.write_text("stand-in login")
    session = tmp_path.absolute() / ".local" / "codex" / "sessions" / "planner-task-7"
    (session / "sessions").mkdir(parents=True)
    (session / "auth.json").symlink_to(login)
    (session / "linked-dir").symlink_to(login.parent, target_is_directory=True)
    content.sessions = []  # the task is gone
    assert asyncio.run(reclaim_unused(tmp_path)) == [session]
    # The links went with the directory; what they pointed at is untouched.
    assert not session.exists() and login.read_text() == "stand-in login"
    assert sorted(p.name for p in login.parent.iterdir()) == ["auth.json"]
