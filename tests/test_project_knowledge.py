"""A project's knowledge: documents its task agents read on demand, with the database stubbed.

A document is text written on the dashboard, an uploaded text file or a file
in the project's default directory. Agents get an index in their system
prompt (title, one-line summary, the path to read) and the directories to
read from as ``--add-dir``; the documents themselves stay out of the prompt.
The real-PostgreSQL side is ``test_project_documents_db.py``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from loopx.agents import knowledge
from loopx.agents.http_server import AgentHTTPServer
from loopx.agents.knowledge import (
    EMPTY_KNOWLEDGE,
    INDEX_HEADING,
    INDEX_INTRO,
    INDEX_MAX_CHARS,
    AgentKnowledge,
    IndexEntry,
    clipped_index,
    compose_index,
    load_knowledge,
    safe_filename,
)
from loopx.agents.tiers.task import TaskRouter
from loopx.db import project_documents as db_documents, projects as db_projects

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _Store:
    """An in-memory stand-in for the projects and project_documents tables."""

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}  # with their content
        self.locks = 0

    def add_project(self, project_id: str = "proj-1", work_dir: str = "") -> None:
        self.projects[project_id] = {"id": project_id, "name": "calc", "work_dir": work_dir, "description": "",
                                     "background": "", "created_at": NOW, "updated_at": NOW, "task_count": 0,
                                     "active_count": 0}

    async def get_project(self, project_id: str, **_: Any) -> dict[str, Any] | None:
        row = self.projects.get(project_id)
        return dict(row) if row else None

    @asynccontextmanager
    async def locked_project(self, project_id: str, **_: Any) -> Any:
        self.locks += 1
        project = self.projects.get(project_id)
        yield "conn", ({"id": project_id, "name": project["name"], "work_dir": project["work_dir"]}
                       if project else None)

    async def list_documents(self, project_id: str, **_: Any) -> list[dict[str, Any]]:
        return [{k: v for k, v in row.items() if k != "content"}
                for row in self.documents.values() if row["project_id"] == project_id]

    async def get_content(self, document_id: str, **_: Any) -> str | None:
        return self.documents[document_id]["content"]

    async def insert_document(self, conn: Any, *, project_id: str, **fields: Any) -> dict[str, Any]:
        assert conn == "conn"
        document_id = f"doc-{len(self.documents) + 1:012d}"
        row = {"id": document_id, "project_id": project_id, "filename": "", "content": None, "path": None,
               "created_at": NOW, "updated_at": NOW, **fields}
        self.documents[document_id] = row
        return {k: v for k, v in row.items() if k != "content"}

    async def update_document(self, conn: Any, project_id: str, document_id: str, **fields: Any) -> dict[str, Any] | None:
        row = self.documents.get(document_id)
        if row is None or row["project_id"] != project_id:
            return None
        row.update(fields)
        return {k: v for k, v in row.items() if k != "content"}

    async def delete_document(self, project_id: str, document_id: str, **_: Any) -> bool:
        row = self.documents.get(document_id)
        if row is None or row["project_id"] != project_id:
            return False
        del self.documents[document_id]
        return True


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fake = _Store()
    monkeypatch.setattr(db_projects, "get_project", fake.get_project)
    for name in ("locked_project", "list_documents", "get_content", "insert_document", "update_document",
                 "delete_document"):
        monkeypatch.setattr(db_documents, name, getattr(fake, name))
    return fake


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


def _call(root: Path, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    """Route one API request on a server whose runtime root is ``root``; return the status and the JSON body."""
    raw = body if isinstance(body, bytes) else (b"" if body is None else json.dumps(body).encode())
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), task_router=TaskRouter(runtime_root=root),
                             supervisor=SimpleNamespace(), runtime_root=root)
    writer = _Writer()
    asyncio.run(server._route_api(method, path, {}, raw, writer))
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def _text(title: str = "Glossary", summary: str = "Terms the code uses", content: str = "MARKER-ALPHA") -> dict:
    return {"source_kind": "text", "title": title, "summary": summary, "content": content}


def _path(path: str, title: str = "Runbook", summary: str = "How to deploy") -> dict:
    return {"source_kind": "path", "title": title, "summary": summary, "path": path}


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    (directory / "docs").mkdir(parents=True)
    (directory / "docs" / "runbook.md").write_text("Deploy with MARKER-BETA.\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the project\n", encoding="utf-8")
    return directory


# --- Adding documents and their validation ---


class TestAdd:
    def test_a_text_document_is_stored_and_listed_with_its_read_path(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text())
        assert status == 201
        doc = body["document"]
        expected = tmp_path / ".local" / "projects" / "proj-1" / "knowledge" / _sha("MARKER-ALPHA") / "Glossary.md"
        assert (doc["title"], doc["summary"], doc["source_kind"], doc["filename"]) == (
            "Glossary", "Terms the code uses", "text", "Glossary.md")
        assert doc["read_path"] == str(expected) and doc["missing"] is False
        assert (doc["size_bytes"], doc["sha256"]) == (len("MARKER-ALPHA"), _sha("MARKER-ALPHA"))
        assert "content" not in doc
        (stored,) = store.documents.values()
        assert stored["content"] == "MARKER-ALPHA" and stored["path"] is None
        assert (body["count"], body["stored_bytes"]) == (1, len("MARKER-ALPHA"))
        assert body["index"] == f"{INDEX_HEADING}\n{INDEX_INTRO}\n- Glossary: Terms the code uses (read: {expected})"
        assert body["index_chars"] == len(body["index"])
        assert body["caps"] == {"document_max_bytes": 2 * 1024 * 1024, "project_max_documents": 100,
                                "project_max_bytes": 50 * 1024 * 1024, "index_max_chars": 4000}

    def test_an_upload_keeps_a_safe_file_name(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", {
            "source_kind": "upload", "title": "Schema", "summary": "The tables",
            "filename": "../../etc/My Schema (v2).sql", "content_base64": _b64("CREATE TABLE t();\n".encode()),
        })
        assert status == 201
        assert body["document"]["filename"] == "My-Schema-v2.sql"
        assert body["document"]["read_path"].endswith(f"/{_sha('CREATE TABLE t();' + chr(10))}/My-Schema-v2.sql")

    @pytest.mark.parametrize(("data", "reason"), [
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00IHDR", "not a text file"),
        (b"caf\xe9 au lait", "not a text file"),
        (b"text with a \x00 NUL", "not a text file"),
    ])
    def test_a_binary_upload_is_refused(self, store: _Store, tmp_path: Path, data: bytes, reason: str) -> None:
        store.add_project()
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", {
            "source_kind": "upload", "title": "Logo", "summary": "The logo", "filename": "logo.png",
            "content_base64": _b64(data),
        })
        assert status == 400 and reason in body["error"] and "logo.png" in body["error"]
        assert store.documents == {}

    def test_an_upload_that_is_not_base64_is_refused(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", {
            "source_kind": "upload", "title": "Notes", "summary": "s", "filename": "notes.txt",
            "content_base64": "not base64!",
        })
        assert status == 400 and "base64" in body["error"]

    @pytest.mark.parametrize("kind", ["text", "upload"])
    def test_a_document_over_2_mib_is_refused_naming_the_cap(self, store: _Store, tmp_path: Path, kind: str) -> None:
        store.add_project()
        big = "x" * (2 * 1024 * 1024 + 1)
        body = _text(content=big) if kind == "text" else {
            "source_kind": "upload", "title": "Big", "summary": "s", "filename": "big.txt",
            "content_base64": _b64(big.encode()),
        }
        status, answer = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", body)
        assert status == 400 and "2 MiB" in answer["error"]
        assert store.documents == {}

    def test_a_document_of_exactly_2_mib_is_accepted(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        assert _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text(content="x" * 2 * 1024 * 1024))[0] == 201

    @pytest.mark.parametrize(("body", "reason"), [
        ({"source_kind": "url", "title": "T", "summary": "S", "url": "https://example.com"}, "URLs are not supported"),
        ({**_text(), "title": ""}, "title"),
        ({**_text(), "title": "x" * 121}, "at most 120"),
        ({**_text(), "summary": ""}, "summary"),
        ({k: v for k, v in _text().items() if k != "summary"}, "summary"),
        ({**_text(), "summary": "two\nlines"}, "one line"),
        ({**_text(), "content": "   "}, "content"),
        ({**_text(), "path": "docs/runbook.md"}, "unknown field 'path'"),
        ([], "JSON object"),
    ])
    def test_a_bad_request_is_refused(self, store: _Store, tmp_path: Path, body: Any, reason: str) -> None:
        store.add_project()
        status, answer = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", body)
        assert status == 400 and reason in answer["error"]
        assert store.documents == {}

    def test_an_unknown_project_is_404(self, store: _Store, tmp_path: Path) -> None:
        assert _call(tmp_path, "POST", "/api/projects/proj-nope/knowledge", _text())[0] == 404
        assert _call(tmp_path, "GET", "/api/projects/proj-nope/knowledge")[0] == 404


class TestPathDocuments:
    def test_a_file_in_the_project_directory_is_referenced_by_its_relative_path(
        self, store: _Store, tmp_path: Path, project_dir: Path,
    ) -> None:
        store.add_project(work_dir=str(project_dir))
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _path("docs/../docs/./runbook.md"))
        assert status == 201
        doc = body["document"]
        assert (doc["path"], doc["filename"], doc["missing"]) == ("docs/runbook.md", None, False)
        assert doc["read_path"] == str(project_dir / "docs" / "runbook.md")
        assert doc["size_bytes"] == len("Deploy with MARKER-BETA.\n")
        (stored,) = store.documents.values()
        assert stored["content"] is None  # read live, never copied
        assert body["stored_bytes"] == 0

    def test_an_absolute_path_inside_the_project_is_stored_relative(
        self, store: _Store, tmp_path: Path, project_dir: Path,
    ) -> None:
        store.add_project(work_dir=str(project_dir))
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge",
                             _path(str(project_dir / "docs" / "runbook.md")))
        assert status == 201 and body["document"]["path"] == "docs/runbook.md"

    @pytest.mark.parametrize(("path", "reason"), [
        ("../secret.txt", "outside the project's default directory"),
        ("docs/../../secret.txt", "outside the project's default directory"),
        ("/etc/hosts", "outside the project's default directory"),
        (".", "outside the project's default directory"),
        ("docs/missing.md", "does not exist"),
        ("docs", "is not a file"),
    ])
    def test_a_path_outside_the_project_or_not_a_file_is_refused(
        self, store: _Store, tmp_path: Path, project_dir: Path, path: str, reason: str,
    ) -> None:
        store.add_project(work_dir=str(project_dir))
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _path(path))
        assert status == 400 and reason in body["error"]
        assert store.documents == {}

    def test_a_link_that_leads_out_of_the_project_is_refused(
        self, store: _Store, tmp_path: Path, project_dir: Path,
    ) -> None:
        os.symlink(tmp_path / "secret.txt", project_dir / "docs" / "innocent.md")
        store.add_project(work_dir=str(project_dir))
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _path("docs/innocent.md"))
        assert status == 400 and "leads outside" in body["error"]

    def test_a_binary_file_is_refused(self, store: _Store, tmp_path: Path, project_dir: Path) -> None:
        (project_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        store.add_project(work_dir=str(project_dir))
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _path("logo.png"))
        assert status == 400 and "not a text file" in body["error"]

    def test_a_project_without_a_directory_cannot_reference_a_path(self, store: _Store, tmp_path: Path) -> None:
        store.add_project(work_dir="")
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _path("docs/runbook.md"))
        assert status == 400 and "no default directory" in body["error"]

    def test_a_file_removed_later_is_listed_as_missing(
        self, store: _Store, tmp_path: Path, project_dir: Path,
    ) -> None:
        store.add_project(work_dir=str(project_dir))
        _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _path("docs/runbook.md"))
        (project_dir / "docs" / "runbook.md").unlink()
        status, body = _call(tmp_path, "GET", "/api/projects/proj-1/knowledge")
        assert status == 200 and body["documents"][0]["missing"] is True
        assert body["index"].endswith(f"- Runbook: How to deploy (missing: {project_dir / 'docs' / 'runbook.md'})")


class TestCaps:
    def test_the_caps_are_2_mib_100_documents_50_mib_and_4000_characters(self) -> None:
        assert (knowledge.DOCUMENT_MAX_BYTES, knowledge.PROJECT_MAX_DOCUMENTS, knowledge.PROJECT_MAX_BYTES,
                knowledge.INDEX_MAX_CHARS) == (2 * 1024 * 1024, 100, 50 * 1024 * 1024, 4000)

    def test_one_document_over_the_count_cap_is_refused(
        self, store: _Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(knowledge, "PROJECT_MAX_DOCUMENTS", 2)
        store.add_project()
        for n in range(2):
            assert _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text(title=f"D{n}", content=f"c{n}"))[0] == 201
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text(title="D2", content="c2"))
        assert status == 400 and "at most 2 documents" in body["error"]
        assert len(store.documents) == 2

    def test_stored_content_over_the_project_cap_is_refused(
        self, store: _Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_dir: Path,
    ) -> None:
        monkeypatch.setattr(knowledge, "PROJECT_MAX_BYTES", 10 * 1024 * 1024)
        monkeypatch.setattr(knowledge, "DOCUMENT_MAX_BYTES", 6 * 1024 * 1024)
        store.add_project(work_dir=str(project_dir))
        assert _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text(title="A", content="a" * 6_000_000))[0] == 201
        status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text(title="B", content="b" * 5_000_000))
        assert status == 400 and "10 MiB" in body["error"]
        # A path document is not stored, so it does not count.
        assert _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _path("docs/runbook.md"))[0] == 201

    def test_a_document_that_would_make_the_index_too_long_is_refused(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        added = 0
        while True:
            status, body = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge",
                                 _text(title=f"Document {added}", summary="s" * 250, content=f"content {added}"))
            if status != 201:
                break
            added += 1
            assert body["index_chars"] <= INDEX_MAX_CHARS
        assert status == 400 and "at most 4000 characters" in body["error"]
        assert added >= 5 and len(store.documents) == added
        assert _call(tmp_path, "GET", "/api/projects/proj-1/knowledge")[1]["index_chars"] <= INDEX_MAX_CHARS


class TestChangeAndDelete:
    def test_update_changes_the_title_summary_and_content(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        doc_id = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text())[1]["document"]["document_id"]
        status, body = _call(tmp_path, "PUT", f"/api/projects/proj-1/knowledge/{doc_id}",
                             {"title": "Terms", "summary": "Every term", "content": "MARKER-GAMMA"})
        assert status == 200
        doc = body["document"]
        assert (doc["title"], doc["summary"], doc["filename"], doc["sha256"]) == (
            "Terms", "Every term", "Terms.md", _sha("MARKER-GAMMA"))
        assert store.documents[doc_id]["content"] == "MARKER-GAMMA"

    @pytest.mark.parametrize(("body", "reason"), [
        ({"source_kind": "path"}, "source cannot change"),
        ({"path": "docs/runbook.md"}, "unknown field"),
        ({}, "fields to change"),
        ({"summary": "a\nb"}, "one line"),
    ])
    def test_a_bad_update_is_refused_and_changes_nothing(
        self, store: _Store, tmp_path: Path, body: dict, reason: str,
    ) -> None:
        store.add_project()
        doc_id = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text())[1]["document"]["document_id"]
        before = dict(store.documents[doc_id])
        status, answer = _call(tmp_path, "PUT", f"/api/projects/proj-1/knowledge/{doc_id}", body)
        assert status == 400 and reason in answer["error"]
        assert store.documents[doc_id] == before

    def test_an_update_that_would_make_the_index_too_long_is_refused(
        self, store: _Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store.add_project()
        doc_id = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text())[1]["document"]["document_id"]
        _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text(title="Style", content="other"))
        cap = _call(tmp_path, "GET", "/api/projects/proj-1/knowledge")[1]["index_chars"] + 10
        monkeypatch.setattr(knowledge, "INDEX_MAX_CHARS", cap)  # 10 characters to spare
        status, body = _call(tmp_path, "PUT", f"/api/projects/proj-1/knowledge/{doc_id}", {"summary": "s" * 40})
        assert status == 400 and f"at most {cap} characters" in body["error"]
        assert store.documents[doc_id]["summary"] == "Terms the code uses"
        assert _call(tmp_path, "PUT", f"/api/projects/proj-1/knowledge/{doc_id}", {"summary": "Terms"})[0] == 200

    def test_delete_removes_the_document(self, store: _Store, tmp_path: Path) -> None:
        store.add_project()
        doc_id = _call(tmp_path, "POST", "/api/projects/proj-1/knowledge", _text())[1]["document"]["document_id"]
        status, body = _call(tmp_path, "DELETE", f"/api/projects/proj-1/knowledge/{doc_id}")
        assert status == 200 and body["deleted"] == doc_id
        assert (body["count"], body["documents"], body["index"]) == (0, [], "")
        assert _call(tmp_path, "DELETE", f"/api/projects/proj-1/knowledge/{doc_id}")[0] == 404
        assert _call(tmp_path, "PUT", f"/api/projects/proj-1/knowledge/{doc_id}", {"title": "x"})[0] == 404


# --- The index and what a run gets ---


def _row(kind: str, title: str, summary: str, source: str, *, number: int = 1) -> dict[str, Any]:
    stored = kind != "path"
    data = source.encode("utf-8") if stored else b""
    return {"id": f"doc-{number:012d}", "project_id": "proj-1", "title": title, "summary": summary,
            "source_kind": kind, "filename": f"{title}.md" if stored else "", "path": None if stored else source,
            "size_bytes": len(data), "sha256": _sha(data), "created_at": NOW, "updated_at": NOW}


@pytest.fixture
def documents(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub the documents ``load_knowledge`` reads: ``documents(rows, contents)``."""
    state: dict[str, Any] = {"rows": [], "contents": {}, "content_reads": []}

    async def _list(project_id: str, **_: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in state["rows"]]

    async def _content(document_id: str, **_: Any) -> str | None:
        state["content_reads"].append(document_id)
        return state["contents"].get(document_id)

    monkeypatch.setattr(db_documents, "list_documents", _list)
    monkeypatch.setattr(db_documents, "get_content", _content)

    def set_rows(*rows: dict[str, Any], contents: dict[str, str] | None = None) -> dict[str, Any]:
        state["rows"] = list(rows)
        state["contents"] = contents or {}
        return state

    return set_rows


def _load(root: Path, project: dict[str, Any] | None, work_dir: str) -> AgentKnowledge:
    return asyncio.run(load_knowledge(root, project, work_dir))


class TestIndex:
    def test_one_line_per_document_after_the_heading(self) -> None:
        index = compose_index([IndexEntry("Glossary", "Terms", "/k/a/Glossary.md"),
                               IndexEntry("Runbook", "Deploys", "/p/docs/runbook.md", missing=True)])
        assert index.split("\n") == [
            "## Project knowledge", INDEX_INTRO, "- Glossary: Terms (read: /k/a/Glossary.md)",
            "- Runbook: Deploys (missing: /p/docs/runbook.md)"]
        assert compose_index([]) == ""

    def test_an_index_over_its_cap_is_clipped_with_a_note(self) -> None:
        entries = [IndexEntry(f"Document {n}", "s" * 200, f"/k/{n}/d.md") for n in range(40)]
        index = clipped_index(entries)
        assert len(index) <= INDEX_MAX_CHARS
        assert index.startswith(f"{INDEX_HEADING}\n{INDEX_INTRO}\n- Document 0: ")
        assert index.endswith("more documents are not listed: the index is over its limit of 4000 characters)")
        assert clipped_index(entries[:3]) == compose_index(entries[:3])

    @pytest.mark.parametrize(("raw", "safe"), [
        ("notes.md", "notes.md"), ("../../etc/passwd", "passwd"), ("C:\\docs\\Plan v2.txt", "Plan-v2.txt"),
        ("..", "d.txt"), (".hidden", "hidden"), ("设计 说明.md", "设计-说明.md"), ("a/b/", "d.txt"),
    ])
    def test_file_names_are_safe(self, raw: str, safe: str) -> None:
        assert safe_filename(raw, default="d.txt") == safe


class TestLoad:
    def test_stored_documents_are_materialized_read_only_and_their_directory_mounted(
        self, tmp_path: Path, documents: Any, project_dir: Path,
    ) -> None:
        state = documents(_row("text", "Glossary", "Terms", "MARKER-ALPHA", number=1),
                          _row("upload", "Schema", "Tables", "CREATE TABLE t();", number=2),
                          contents={"doc-000000000001": "MARKER-ALPHA", "doc-000000000002": "CREATE TABLE t();"})
        loaded = _load(tmp_path, {"id": "proj-1", "work_dir": str(project_dir)}, str(project_dir))

        root = tmp_path / ".local" / "projects" / "proj-1" / "knowledge"
        glossary = root / _sha("MARKER-ALPHA") / "Glossary.md"
        assert glossary.read_text() == "MARKER-ALPHA"
        assert stat.S_IMODE(glossary.stat().st_mode) == 0o444
        assert (root / _sha("CREATE TABLE t();") / "Schema.md").read_text() == "CREATE TABLE t();"
        assert loaded.add_dirs == (str(root),)  # no path document: the project directory is not mounted
        assert loaded.documents == 2 and loaded.error is None
        assert loaded.index == compose_index([IndexEntry("Glossary", "Terms", str(glossary)),
                                              IndexEntry("Schema", "Tables", str(root / _sha("CREATE TABLE t();") / "Schema.md"))])
        assert loaded.sha256 == _sha(loaded.index)
        # A second run reuses the copies without reading the content again.
        state["content_reads"].clear()
        assert _load(tmp_path, {"id": "proj-1", "work_dir": str(project_dir)}, str(project_dir)) == loaded
        assert state["content_reads"] == []

    def test_a_damaged_copy_is_written_again(self, tmp_path: Path, documents: Any) -> None:
        documents(_row("text", "Glossary", "Terms", "MARKER-ALPHA"), contents={"doc-000000000001": "MARKER-ALPHA"})
        _load(tmp_path, {"id": "proj-1", "work_dir": ""}, str(tmp_path))
        copy = tmp_path / ".local" / "projects" / "proj-1" / "knowledge" / _sha("MARKER-ALPHA") / "Glossary.md"
        os.chmod(copy, 0o644)
        copy.write_text("tampered")
        _load(tmp_path, {"id": "proj-1", "work_dir": ""}, str(tmp_path))
        assert copy.read_text() == "MARKER-ALPHA"

    def test_path_documents_are_read_live_and_mount_the_project_when_the_task_runs_elsewhere(
        self, tmp_path: Path, documents: Any, project_dir: Path,
    ) -> None:
        documents(_row("path", "Runbook", "Deploys", "docs/runbook.md"))
        elsewhere = tmp_path / "task-dir"
        elsewhere.mkdir()
        loaded = _load(tmp_path, {"id": "proj-1", "work_dir": str(project_dir)}, str(elsewhere))
        assert loaded.add_dirs == (str(project_dir),)
        assert loaded.index.endswith(f"- Runbook: Deploys (read: {project_dir / 'docs' / 'runbook.md'})")
        assert not (tmp_path / ".local" / "projects").exists()  # nothing copied
        # In the project's own directory there is nothing more to mount.
        assert _load(tmp_path, {"id": "proj-1", "work_dir": str(project_dir)}, str(project_dir)).add_dirs == ()

    def test_a_missing_path_document_is_listed_as_missing(self, tmp_path: Path, documents: Any, project_dir: Path) -> None:
        documents(_row("path", "Runbook", "Deploys", "docs/gone.md"))
        loaded = _load(tmp_path, {"id": "proj-1", "work_dir": str(project_dir)}, str(project_dir))
        assert loaded.index.endswith(f"- Runbook: Deploys (missing: {project_dir / 'docs' / 'gone.md'})")
        assert loaded.documents == 1 and loaded.error is None

    def test_without_documents_or_a_project_there_is_nothing(self, tmp_path: Path, documents: Any) -> None:
        documents()
        assert _load(tmp_path, {"id": "proj-1", "work_dir": ""}, str(tmp_path)) == EMPTY_KNOWLEDGE
        assert _load(tmp_path, None, str(tmp_path)) == EMPTY_KNOWLEDGE
        assert EMPTY_KNOWLEDGE.sha256 is None and EMPTY_KNOWLEDGE.add_dirs == ()

    def test_an_unreadable_database_leaves_the_run_without_knowledge_and_says_why(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _broken(*_: Any, **__: Any) -> list[dict[str, Any]]:
            raise ConnectionError("database is down")

        monkeypatch.setattr(db_documents, "list_documents", _broken)
        loaded = _load(tmp_path, {"id": "proj-1", "work_dir": ""}, str(tmp_path))
        assert (loaded.index, loaded.add_dirs) == ("", ())
        assert loaded.error == "the project's knowledge could not be read"

