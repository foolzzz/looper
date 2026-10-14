"""A project's own Claude Code skills: what the API refuses, and the SKILL.md a skill is written as.

A project writes skills (a name, a one-line description and the body of their
``SKILL.md``) into the catalog. Every refusal here happens before the
database is touched, so none is needed; writing, installing and loading them
against a real PostgreSQL is ``test_project_skills_db.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import project_skills
from looper.agents.http_server import AgentHTTPServer
from looper.agents.project_skills import skill_body, skill_content, skill_description, skill_file, skill_name
from looper.agents.tiers.task import TaskRouter


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


def _skill(name: Any = "deploy-check", description: Any = "Use before a deploy: checks the release steps.",
           content: Any = "Run `make check`, then reply DEPLOY-OK.\n", **extra: Any) -> dict[str, Any]:
    return {"name": name, "description": description, "content": content, **extra}


BASE = "/api/projects/proj-1/skills"


class TestRefused:
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
    def test_a_name_that_is_not_a_slug(self, tmp_path: Path, name: Any, reason: str) -> None:
        status, body = _call(tmp_path, "POST", BASE, _skill(name=name))
        assert status == 400 and reason in body["error"]

    @pytest.mark.parametrize(("description", "reason"), [
        ("", "a skill needs a description"),
        ("   ", "a skill needs a description"),
        (None, "a skill needs a description"),
        ("two\nlines", "one line"),
        ("tab\there", "control characters"),
        ("x" * 1025, "at most 1024 characters"),
    ])
    def test_a_bad_description(self, tmp_path: Path, description: Any, reason: str) -> None:
        status, body = _call(tmp_path, "POST", BASE, _skill(description=description))
        assert status == 400 and reason in body["error"]

    @pytest.mark.parametrize(("content", "reason"), [
        ("", "a skill needs its content"),
        ("  \n ", "a skill needs its content"),
        (["not", "text"], "a skill needs its content"),
        ("a \x00 NUL", "NUL"),
        ("---\nname: other\n---\nBody", "frontmatter"),
        ("x" * (64 * 1024 + 1), "at most 64 KiB"),
        ("é" * (32 * 1024 + 1), "at most 64 KiB"),  # counted in UTF-8 bytes
    ])
    def test_bad_content(self, tmp_path: Path, content: Any, reason: str) -> None:
        status, body = _call(tmp_path, "POST", BASE, _skill(content=content))
        assert status == 400 and reason in body["error"]

    @pytest.mark.parametrize(("body", "reason"), [
        ({**_skill(), "allowed_tools": "Bash"}, "unknown field 'allowed_tools'"),
        ({**_skill(), "enabled": "yes"}, "enabled must be true or false"),
        ([], "JSON object"),
        (b"{not json", "invalid JSON"),
    ])
    def test_a_bad_request(self, tmp_path: Path, body: Any, reason: str) -> None:
        status, answer = _call(tmp_path, "POST", BASE, body)
        assert status == 400 and reason in answer["error"]

    @pytest.mark.parametrize(("body", "reason"), [
        ({}, "give at least one of"),
        ({"enabled": False}, "where it is installed"),
        ({"name": "Bad Name"}, "lowercase letters, digits and hyphens"),
        ({"content": ""}, "a skill needs its content"),
        ({"description": "a\nb"}, "one line"),
        ({"project_id": "proj-2"}, "unknown field 'project_id'"),
    ])
    def test_a_bad_change(self, tmp_path: Path, body: Any, reason: str) -> None:
        status, answer = _call(tmp_path, "PUT", f"{BASE}/skill-000000000001", body)
        assert status == 400 and reason in answer["error"]


class TestAccepted:
    @pytest.mark.parametrize("name", ["a", "deploy-check", "v2", "x" * 64, "0-9-a"])
    def test_a_slug_is_a_name(self, name: str) -> None:
        assert skill_name(f"  {name} ") == name

    def test_a_description_of_1024_characters_and_content_of_exactly_64_kib(self) -> None:
        assert skill_description("d" * 1024) == "d" * 1024
        assert skill_content("x" * 64 * 1024) == "x" * 64 * 1024


class TestSkillFile:
    def test_the_frontmatter_comes_from_the_fields_and_the_body_follows(self) -> None:
        text = skill_file("deploy-check", "Use before a deploy.", "Run `make check`.\n\nThen reply OK.")
        assert text == ("---\nname: deploy-check\ndescription: \"Use before a deploy.\"\n---\n\n"
                        "Run `make check`.\n\nThen reply OK.\n")

    def test_a_description_with_yaml_syntax_stays_one_quoted_string(self) -> None:
        text = skill_file("q", 'Checks: "deploy" steps # not a comment \\ [x] {y} 中文', "Body\n")
        line = text.splitlines()[2]
        assert line == 'description: "Checks: \\"deploy\\" steps # not a comment \\\\ [x] {y} 中文"'
        assert json.loads(line.removeprefix("description: ")) == 'Checks: "deploy" steps # not a comment \\ [x] {y} 中文'

    @pytest.mark.parametrize("content", ["Reply OK.\n", "Two\n\nparagraphs\n", "---\n inside the body\n"])
    def test_the_body_reads_back_from_the_file(self, content: str) -> None:
        text = skill_file("deploy-check", 'Says "OK": yes', content)
        assert skill_body(text, "deploy-check", 'Says "OK": yes') == content

    def test_one_skill_is_one_file_and_its_version_is_the_items(self) -> None:
        from looper.agents.library import item_sha256

        file, sha = project_skills.skill_md("deploy-check", "Use before a deploy.", "Reply OK.")
        assert (file["path"], file["executable"]) == ("SKILL.md", False)
        assert file["content"] == skill_file("deploy-check", "Use before a deploy.", "Reply OK.")
        assert sha == item_sha256([file])


def test_the_caps_are_the_documented_ones() -> None:
    assert project_skills.skill_caps() == {"name_max_chars": 64, "description_max_chars": 1024,
                                           "content_max_bytes": 64 * 1024, "project_max_skills": 20}
