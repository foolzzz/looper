"""A project's own Claude Code skills, with the database stubbed.

A project can carry skills (a name, a one-line description and the body of
their ``SKILL.md``) that only its own Planner, Workers and Reviewers load:
the enabled ones are written, read-only and once per content hash, as one
plugin under the runtime root and mounted with ``--plugin-dir``. Nothing goes
into the project's repository. The real-PostgreSQL side is
``test_project_skills_db.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from loopx.agents import project_skills
from loopx.agents.http_server import AgentHTTPServer
from loopx.agents.tiers.task import TaskRouter
from loopx.db import project_skills as db_skills, projects as db_projects

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
CAPS = {"name_max_chars": 64, "description_max_chars": 1024, "content_max_bytes": 64 * 1024,
        "project_max_skills": 20}


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class _Store:
    """An in-memory stand-in for the projects and project_skills tables."""

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}  # with their content
        self.locks = 0

    def add_project(self, project_id: str = "proj-1") -> None:
        self.projects[project_id] = {"id": project_id, "name": "calc", "work_dir": "", "description": "",
                                     "background": "", "created_at": NOW, "updated_at": NOW, "task_count": 0,
                                     "active_count": 0}

    @staticmethod
    def _listed(row: dict[str, Any]) -> dict[str, Any]:
        return {**{k: v for k, v in row.items() if k != "content"}, "size_bytes": len(row["content"].encode())}

    async def get_project(self, project_id: str, **_: Any) -> dict[str, Any] | None:
        row = self.projects.get(project_id)
        return dict(row) if row else None

    @asynccontextmanager
    async def locked_project(self, project_id: str, **_: Any) -> Any:
        self.locks += 1
        project = self.projects.get(project_id)
        yield "conn", ({"id": project_id, "name": project["name"], "work_dir": ""} if project else None)

    async def list_skills(self, project_id: str, **_: Any) -> list[dict[str, Any]]:
        rows = [row for row in self.skills.values() if row["project_id"] == project_id]
        return [self._listed(row) for row in sorted(rows, key=lambda row: row["name"])]

    async def list_enabled_skills(self, project_id: str, **_: Any) -> list[dict[str, Any]]:
        rows = [row for row in self.skills.values() if row["project_id"] == project_id and row["enabled"]]
        return [{k: row[k] for k in ("id", "name", "description", "content")}
                for row in sorted(rows, key=lambda row: row["name"])]

    async def insert_skill(self, conn: Any, *, project_id: str, **fields: Any) -> dict[str, Any]:
        assert conn == "conn"
        skill_id = f"skill-{len(self.skills) + 1:012d}"
        row = {"id": skill_id, "project_id": project_id, "enabled": True, "created_at": NOW, "updated_at": NOW,
               **fields}
        self.skills[skill_id] = row
        return self._listed(row)

    async def update_skill(self, conn: Any, project_id: str, skill_id: str, **fields: Any) -> dict[str, Any] | None:
        assert conn == "conn"
        row = self.skills.get(skill_id)
        if row is None or row["project_id"] != project_id:
            return None
        row.update(fields)
        return self._listed(row)

    async def delete_skill(self, project_id: str, skill_id: str, **_: Any) -> bool:
        row = self.skills.get(skill_id)
        if row is None or row["project_id"] != project_id:
            return False
        del self.skills[skill_id]
        return True


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fake = _Store()
    monkeypatch.setattr(db_projects, "get_project", fake.get_project)
    for name in ("locked_project", "list_skills", "list_enabled_skills", "insert_skill", "update_skill",
                 "delete_skill"):
        monkeypatch.setattr(db_skills, name, getattr(fake, name))
    return fake


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


def _call(root: Path, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """Route one API request; return the status and the JSON body."""
    raw = body if isinstance(body, bytes) else (b"" if body is None else json.dumps(body).encode())
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), task_router=TaskRouter(runtime_root=root),
                             supervisor=SimpleNamespace(), runtime_root=root)
    writer = _Writer()
    asyncio.run(server._route_api(method, path, {}, raw, writer))
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def _skill(name: str = "deploy-check", description: str = "Use before a deploy: checks the release steps.",
           content: str = "Run `make check`, then reply DEPLOY-OK.\n", **extra: Any) -> dict[str, Any]:
    return {"name": name, "description": description, "content": content, **extra}


BASE = "/api/projects/proj-1/skills"


# --- Adding skills and their validation ---


class TestAdd:
    def test_a_skill_is_stored_enabled_and_listed_without_its_content(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        status, body = _call(tmp_path, "POST", BASE, _skill())
        assert status == 201
        skill = body["skill"]
        assert (skill["name"], skill["qualified_name"], skill["enabled"]) == (
            "deploy-check", "project:deploy-check", True)
        assert skill["description"] == "Use before a deploy: checks the release steps."
        assert skill["size_bytes"] == len("Run `make check`, then reply DEPLOY-OK.\n")
        assert skill["skill_id"].startswith("skill-") and "content" not in skill
        (stored,) = store.skills.values()
        assert stored["content"] == "Run `make check`, then reply DEPLOY-OK.\n"
        assert (body["count"], body["enabled_count"], body["plugin"]) == (1, 1, "project")
        assert body["skills"] == [skill]
        assert body["caps"] == CAPS
        assert store.locks == 1  # under the project's row lock

    def test_a_skill_can_be_added_disabled(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        status, body = _call(tmp_path, "POST", BASE, _skill(enabled=False))
        assert status == 201 and body["skill"]["enabled"] is False
        assert (body["count"], body["enabled_count"]) == (1, 0)

    def test_the_list_is_by_name(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        for name in ("zeta", "alpha", "mid-way"):
            assert _call(tmp_path, "POST", BASE, _skill(name=name))[0] == 201
        status, body = _call(tmp_path, "GET", BASE)
        assert status == 200
        assert [s["name"] for s in body["skills"]] == ["alpha", "mid-way", "zeta"]

    @pytest.mark.parametrize("name", ["a", "deploy-check", "v2", "x" * 64, "0-9-a"])
    def test_a_slug_is_a_name(self, store: _Store, tmp_path: Path, name: str) -> None:
        store.add_project()
        assert _call(tmp_path, "POST", BASE, _skill(name=name))[0] == 201

    @pytest.mark.parametrize(("name", "reason"), [
        ("", "a skill needs a name"),
        ("Deploy", "lowercase letters, digits and hyphens"),
        ("deploy check", "lowercase letters, digits and hyphens"),
        ("deploy_check", "lowercase letters, digits and hyphens"),
        ("-deploy", "lowercase letters, digits and hyphens"),
        ("deploy-", "lowercase letters, digits and hyphens"),
        ("deploy--check", "lowercase letters, digits and hyphens"),
        ("../etc", "lowercase letters, digits and hyphens"),
        ("project:deploy", "lowercase letters, digits and hyphens"),
        ("déploy", "lowercase letters, digits and hyphens"),
        ("x" * 65, "at most 64 characters"),
        (7, "a skill needs a name"),
    ])
    def test_a_name_that_is_not_a_slug_is_refused(self, store: _Store, tmp_path: Path, name: Any, reason: str) -> None:
        store.add_project()
        status, body = _call(tmp_path, "POST", BASE, _skill(name=name))
        assert status == 400 and reason in body["error"]
        assert store.skills == {}

    @pytest.mark.parametrize(("description", "reason"), [
        ("", "a skill needs a description"),
        ("   ", "a skill needs a description"),
        (None, "a skill needs a description"),
        ("two\nlines", "one line"),
        ("tab\there", "control characters"),
        ("x" * 1025, "at most 1024 characters"),
    ])
    def test_a_bad_description_is_refused(
        self, store: _Store, tmp_path: Path, description: Any, reason: str,
    ) -> None:
        store.add_project()
        status, body = _call(tmp_path, "POST", BASE, _skill(description=description))
        assert status == 400 and reason in body["error"]
        assert store.skills == {}

    def test_a_description_of_1024_characters_is_accepted(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        assert _call(tmp_path, "POST", BASE, _skill(description="d" * 1024))[0] == 201

    @pytest.mark.parametrize(("content", "reason"), [
        ("", "a skill needs its content"),
        ("  \n ", "a skill needs its content"),
        (["not", "text"], "a skill needs its content"),
        ("a \x00 NUL", "NUL"),
        ("---\nname: other\n---\nBody", "frontmatter"),
        ("x" * (64 * 1024 + 1), "at most 64 KiB"),
        ("é" * (32 * 1024 + 1), "at most 64 KiB"),  # counted in UTF-8 bytes
    ])
    def test_bad_content_is_refused(self, store: _Store, tmp_path: Path, content: Any, reason: str) -> None:
        store.add_project()
        status, body = _call(tmp_path, "POST", BASE, _skill(content=content))
        assert status == 400 and reason in body["error"]
        assert store.skills == {}

    def test_content_of_exactly_64_kib_is_accepted(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        assert _call(tmp_path, "POST", BASE, _skill(content="x" * 64 * 1024))[0] == 201

    @pytest.mark.parametrize(("body", "reason"), [
        ({**_skill(), "allowed_tools": "Bash"}, "unknown field 'allowed_tools'"),
        ({**_skill(), "enabled": "yes"}, "enabled must be true or false"),
        ([], "JSON object"),
        (b"{not json", "invalid JSON"),
    ])
    def test_a_bad_request_is_refused(self, store: _Store, tmp_path: Path, body: Any, reason: str) -> None:
        store.add_project()
        status, answer = _call(tmp_path, "POST", BASE, body)
        assert status == 400 and reason in answer["error"]
        assert store.skills == {}

    def test_a_name_the_project_already_has_is_a_conflict(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        store.add_project("proj-2")
        assert _call(tmp_path, "POST", BASE, _skill())[0] == 201
        status, body = _call(tmp_path, "POST", BASE, _skill(description="Another one."))
        assert status == 409 and "already has a skill named 'deploy-check'" in body["error"]
        # Another project may use the same name.
        assert _call(tmp_path, "POST", "/api/projects/proj-2/skills", _skill())[0] == 201
        assert len(store.skills) == 2

    def test_a_project_holds_at_most_20_skills(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        for n in range(20):
            assert _call(tmp_path, "POST", BASE, _skill(name=f"skill-{n}"))[0] == 201
        status, body = _call(tmp_path, "POST", BASE, _skill(name="one-more"))
        assert status == 400 and "a project has at most 20 skills" in body["error"]
        assert len(store.skills) == 20

    def test_an_unknown_project_is_404(self, store: _Store, tmp_path: Path) -> None:
        assert _call(tmp_path, "POST", "/api/projects/proj-nope/skills", _skill())[0] == 404
        status, body = _call(tmp_path, "GET", "/api/projects/proj-nope/skills")
        assert status == 404 and "proj-nope" in body["error"]


# --- Changing and deleting ---


class TestChangeAndDelete:
    @pytest.fixture
    def skill_id(self, store: _Store, tmp_path: Path) -> str:
        store.add_project()
        _call(tmp_path, "POST", BASE, _skill(name="release-notes"))
        return _call(tmp_path, "POST", BASE, _skill())[1]["skill"]["skill_id"]

    def test_the_switch_disables_and_enables_a_skill(self, store: _Store, tmp_path: Path, skill_id: str) -> None:
        status, body = _call(tmp_path, "PUT", f"{BASE}/{skill_id}", {"enabled": False})
        assert status == 200 and body["skill"]["enabled"] is False
        assert (body["count"], body["enabled_count"]) == (2, 1)
        assert store.skills[skill_id]["content"] == "Run `make check`, then reply DEPLOY-OK.\n"  # untouched
        assert _call(tmp_path, "PUT", f"{BASE}/{skill_id}", {"enabled": True})[1]["skill"]["enabled"] is True

    def test_name_description_and_content_can_change(self, store: _Store, tmp_path: Path, skill_id: str) -> None:
        status, body = _call(tmp_path, "PUT", f"{BASE}/{skill_id}", {
            "name": "deploy-gate", "description": "Gate a deploy.", "content": "Reply GATE-OK."})
        assert status == 200
        assert (body["skill"]["name"], body["skill"]["qualified_name"]) == ("deploy-gate", "project:deploy-gate")
        assert body["skill"]["size_bytes"] == len("Reply GATE-OK.")
        assert (store.skills[skill_id]["description"], store.skills[skill_id]["content"]) == (
            "Gate a deploy.", "Reply GATE-OK.")

    def test_keeping_its_own_name_is_no_conflict(self, store: _Store, tmp_path: Path, skill_id: str) -> None:
        assert _call(tmp_path, "PUT", f"{BASE}/{skill_id}", {"name": "deploy-check", "enabled": False})[0] == 200

    def test_renaming_onto_another_skill_is_a_conflict(self, store: _Store, tmp_path: Path, skill_id: str) -> None:
        status, body = _call(tmp_path, "PUT", f"{BASE}/{skill_id}", {"name": "release-notes"})
        assert status == 409 and "already has a skill named 'release-notes'" in body["error"]
        assert store.skills[skill_id]["name"] == "deploy-check"

    @pytest.mark.parametrize(("body", "reason"), [
        ({}, "give at least one of"),
        ({"enabled": 1}, "enabled must be true or false"),
        ({"name": "Bad Name"}, "lowercase letters, digits and hyphens"),
        ({"content": ""}, "a skill needs its content"),
        ({"description": "a\nb"}, "one line"),
        ({"project_id": "proj-2"}, "unknown field 'project_id'"),
    ])
    def test_a_bad_change_is_refused(
        self, store: _Store, tmp_path: Path, skill_id: str, body: Any, reason: str,
    ) -> None:
        before = dict(store.skills[skill_id])
        status, answer = _call(tmp_path, "PUT", f"{BASE}/{skill_id}", body)
        assert status == 400 and reason in answer["error"]
        assert store.skills[skill_id] == before

    def test_delete_removes_it_and_a_second_delete_is_404(self, store: _Store, tmp_path: Path, skill_id: str) -> None:
        status, body = _call(tmp_path, "DELETE", f"{BASE}/{skill_id}")
        assert status == 200 and body["deleted"] == skill_id and body["count"] == 1
        assert skill_id not in store.skills
        assert _call(tmp_path, "DELETE", f"{BASE}/{skill_id}")[0] == 404

    def test_unknown_skills_and_projects_are_404(self, store: _Store, tmp_path: Path, skill_id: str) -> None:
        assert _call(tmp_path, "PUT", f"{BASE}/skill-nope", {"enabled": False})[0] == 404
        assert _call(tmp_path, "DELETE", f"{BASE}/skill-nope")[0] == 404
        # A skill is reached through its own project only.
        store.add_project("proj-2")
        assert _call(tmp_path, "PUT", f"/api/projects/proj-2/skills/{skill_id}", {"enabled": False})[0] == 404
        assert _call(tmp_path, "DELETE", f"/api/projects/proj-2/skills/{skill_id}")[0] == 404
        assert _call(tmp_path, "PUT", f"/api/projects/proj-nope/skills/{skill_id}", {"enabled": False})[0] == 404
        assert store.skills[skill_id]["enabled"] is True


def test_the_caps_are_the_documented_ones() -> None:
    assert (project_skills.NAME_MAX_CHARS, project_skills.DESCRIPTION_MAX_CHARS, project_skills.CONTENT_MAX_BYTES,
            project_skills.PROJECT_MAX_SKILLS) == (64, 1024, 65536, 20)
    assert project_skills.PLUGIN_NAME == "project"
