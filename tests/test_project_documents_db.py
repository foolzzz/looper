"""Project knowledge against a real PostgreSQL: the table, the API and what a run loads.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _fresh_schema() -> AsyncIterator[Any]:
    import asyncpg

    schema = f"looper_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": schema})
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _run(test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``test(pool)`` on a new schema that is also the server's pool."""
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)

    asyncio.run(_main())


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


async def _call(root: Path, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), task_router=TaskRouter(runtime_root=root),
                             supervisor=SimpleNamespace(), runtime_root=root)
    writer = _Writer()
    await server._route_api(method, path, {}, b"" if body is None else json.dumps(body).encode(), writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "calc"
    (directory / "docs").mkdir(parents=True)
    (directory / "docs" / "runbook.md").write_text("Deploy with MARKER-BETA.\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside\n", encoding="utf-8")
    return directory


def test_the_table_keeps_each_kind_consistent_and_goes_with_its_project(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.db import connection, projects, tasks

    async def _test(pool: Any) -> None:
        await connection.ensure_schema(pool)  # applying the schema again changes nothing
        project = await projects.create_project(name="calc", pool=pool)
        insert = ("INSERT INTO project_documents (id, project_id, title, summary, source_kind, filename, content, path,"
                  " size_bytes, sha256) VALUES ($1, $2, 't', 's', $3, $4, $5, $6, 0, 'x')")
        await pool.execute(insert, "doc-text", project["id"], "text", "t.md", "body", None)
        await pool.execute(insert, "doc-path", project["id"], "path", "", None, "docs/a.md")
        for kind, filename, content, path in [
            ("path", "", "body", "docs/a.md"),  # a path document holds no content
            ("text", "t.md", None, None),  # a stored document holds its content
            ("upload", "", "body", None),  # ... and its file name
            ("text", "t.md", "body", "docs/a.md"),  # ... and no path
            ("url", "", None, None),  # no URLs
        ]:
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(insert, f"doc-{uuid.uuid4().hex[:8]}", project["id"], kind, filename, content, path)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute(insert, "doc-orphan", "proj-nope", "text", "t.md", "body", None)
        # A draft outlives the task that wrote it, and goes with its project.
        await tasks.create_task(task_id="task-1", requirement="r", work_dir="/w", project_id=project["id"], pool=pool)
        await pool.execute("INSERT INTO project_document_drafts (id, project_id, task_id, title, summary, content) "
                           "VALUES ('draft-1', $1, 'task-1', 't', 's', 'c')", project["id"])
        assert await tasks.delete_task("task-1", pool=pool)
        assert await pool.fetchval("SELECT task_id FROM project_document_drafts") is None
        assert await projects.delete_project(project["id"], pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM project_documents") == 0
        assert await pool.fetchval("SELECT count(*) FROM project_document_drafts") == 0

    _run(_test, monkeypatch)


def test_the_api_adds_lists_changes_and_deletes_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_dir: Path,
) -> None:
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(project_dir), pool=pool)
        base = f"/api/projects/{project['id']}/knowledge"
        status, text = await _call(tmp_path, "POST", base, {
            "source_kind": "text", "title": "Glossary", "summary": "Terms", "content": "MARKER-ALPHA · 中文"})
        assert status == 201
        status, upload = await _call(tmp_path, "POST", base, {
            "source_kind": "upload", "title": "Schema", "summary": "Tables", "filename": "schema.sql",
            "content_base64": base64.b64encode(b"CREATE TABLE t();\n").decode()})
        assert status == 201
        status, path = await _call(tmp_path, "POST", base, {
            "source_kind": "path", "title": "Runbook", "summary": "Deploys", "path": "./docs/runbook.md"})
        assert status == 201

        status, view = await _call(tmp_path, "GET", base)
        assert status == 200
        assert [(d["title"], d["source_kind"]) for d in view["documents"]] == [
            ("Glossary", "text"), ("Schema", "upload"), ("Runbook", "path")]
        assert view["stored_bytes"] == len("MARKER-ALPHA · 中文".encode()) + len("CREATE TABLE t();\n")
        rows = {r["title"]: dict(r) for r in await pool.fetch("SELECT * FROM project_documents")}
        assert rows["Glossary"]["content"] == "MARKER-ALPHA · 中文" and rows["Glossary"]["path"] is None
        assert rows["Glossary"]["sha256"] == _sha("MARKER-ALPHA · 中文")
        assert rows["Schema"]["filename"] == "schema.sql"
        assert (rows["Runbook"]["content"], rows["Runbook"]["path"]) == (None, "docs/runbook.md")

        # Refused: a path out of the project, a binary upload; nothing is written.
        assert (await _call(tmp_path, "POST", base, {
            "source_kind": "path", "title": "x", "summary": "y", "path": "../secret.txt"}))[0] == 400
        assert (await _call(tmp_path, "POST", base, {
            "source_kind": "upload", "title": "x", "summary": "y", "filename": "a.bin",
            "content_base64": base64.b64encode(b"\x00\x01\x02").decode()}))[0] == 400
        assert await pool.fetchval("SELECT count(*) FROM project_documents") == 3

        doc_id = text["document"]["document_id"]
        status, changed = await _call(tmp_path, "PUT", f"{base}/{doc_id}", {"content": "MARKER-GAMMA"})
        assert status == 200 and changed["document"]["sha256"] == _sha("MARKER-GAMMA")
        row = await pool.fetchrow("SELECT content, size_bytes, updated_at, created_at FROM project_documents "
                                  "WHERE id = $1", doc_id)
        assert (row["content"], row["size_bytes"]) == ("MARKER-GAMMA", 12) and row["updated_at"] > row["created_at"]

        status, after = await _call(tmp_path, "DELETE", f"{base}/{upload['document']['document_id']}")
        assert status == 200 and after["count"] == 2
        assert (await _call(tmp_path, "DELETE", f"{base}/{upload['document']['document_id']}"))[0] == 404
        assert path["document"]["read_path"] == str(project_dir / "docs" / "runbook.md")

    _run(_test, monkeypatch)


def test_the_api_shows_one_document_with_its_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_dir: Path,
) -> None:
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(project_dir), pool=pool)
        other = await projects.create_project(name="other", pool=pool)
        base = f"/api/projects/{project['id']}/knowledge"
        _, text = await _call(tmp_path, "POST", base, {
            "source_kind": "text", "title": "Glossary", "summary": "Terms", "content": "MARKER-ALPHA · 中文"})
        _, path = await _call(tmp_path, "POST", base, {
            "source_kind": "path", "title": "Runbook", "summary": "Deploys", "path": "docs/runbook.md"})
        text_id, path_id = text["document"]["document_id"], path["document"]["document_id"]

        status, shown = await _call(tmp_path, "GET", f"{base}/{text_id}")
        assert status == 200
        assert (shown["document"]["title"], shown["content"]) == ("Glossary", "MARKER-ALPHA · 中文")
        # A path document shows its file as it is now.
        (project_dir / "docs" / "runbook.md").write_text("Deploy with MARKER-DELTA.\n", encoding="utf-8")
        status, shown = await _call(tmp_path, "GET", f"{base}/{path_id}")
        assert (status, shown["content"]) == (200, "Deploy with MARKER-DELTA.\n")
        (project_dir / "docs" / "runbook.md").unlink()
        status, shown = await _call(tmp_path, "GET", f"{base}/{path_id}")
        assert status == 200 and shown["content"] is None and shown["document"]["missing"]
        assert "missing" in shown["content_error"]

        assert (await _call(tmp_path, "GET", f"{base}/doc-nope"))[0] == 404
        assert (await _call(tmp_path, "GET", f"/api/projects/{other['id']}/knowledge/{text_id}"))[0] == 404

    _run(_test, monkeypatch)


def test_concurrent_adds_cannot_both_pass_the_count_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import knowledge
    from looper.db import projects

    monkeypatch.setattr(knowledge, "PROJECT_MAX_DOCUMENTS", 1)

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)

        async def add(n: int) -> Any:
            return await knowledge.add_document(project["id"], {
                "source_kind": "text", "title": f"D{n}", "summary": "s", "content": f"c{n}"}, tmp_path)

        results = await asyncio.gather(add(1), add(2), return_exceptions=True)
        refused = [r for r in results if isinstance(r, knowledge.KnowledgeError)]
        assert len(refused) == 1 and "at most 1 document" in str(refused[0])
        assert await pool.fetchval("SELECT count(*) FROM project_documents") == 1

    _run(_test, monkeypatch)


def test_a_run_loads_the_documents_from_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_dir: Path,
) -> None:
    from looper.agents import knowledge
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(project_dir), pool=pool)
        await knowledge.add_document(project["id"], {
            "source_kind": "text", "title": "Glossary", "summary": "Terms", "content": "MARKER-ALPHA"}, tmp_path)
        await knowledge.add_document(project["id"], {
            "source_kind": "path", "title": "Runbook", "summary": "Deploys", "path": "docs/runbook.md"}, tmp_path)
        task_dir = tmp_path / "task"
        task_dir.mkdir()

        loaded = await knowledge.load_knowledge(tmp_path, project, str(task_dir))

        set_dir = Path(loaded.add_dirs[0])
        assert set_dir.parent == tmp_path / ".local" / "projects" / project["id"] / "knowledge"
        assert (set_dir / "Glossary.md").read_text() == "MARKER-ALPHA"
        assert loaded.add_dirs == (str(set_dir), str(project_dir)) and loaded.documents == 2
        assert loaded.index.splitlines()[2:] == [
            f"In {set_dir}:", "- Glossary: Terms (Glossary.md)",
            f"In {project_dir}:", "- Runbook: Deploys (docs/runbook.md)"]
        # The view the API gives shows the same index the run got.
        assert (await knowledge.knowledge_view(project["id"], tmp_path))["index"] == loaded.index

    _run(_test, monkeypatch)


def test_a_draft_stays_out_of_runs_until_it_is_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_dir: Path,
) -> None:
    """A draft is in no run's index or mounts; confirmed, it is a text document every run gets; discarded, gone."""
    from looper.agents import knowledge
    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(project_dir), pool=pool)
        await tasks.create_task(task_id="task-1", requirement="r", work_dir="/w", project_id=project["id"], pool=pool)
        base = f"/api/projects/{project['id']}/knowledge/drafts"
        lessons = await knowledge.add_draft(project["id"], task_id="task-1", title="Lessons: calc",
                                            summary="What task-1 learned", content="- MARKER-LESSON")
        other = await knowledge.add_draft(project["id"], task_id=None, title="Lessons: other", summary="s",
                                          content="- other")

        assert await knowledge.load_knowledge(tmp_path, project, str(project_dir)) == knowledge.EMPTY_KNOWLEDGE
        status, listed = await _call(tmp_path, "GET", base)
        assert status == 200 and [(d["title"], d["task_id"], d["content"]) for d in listed["drafts"]] == [
            ("Lessons: calc", "task-1", "- MARKER-LESSON"), ("Lessons: other", None, "- other")]

        # Confirmed with an edited title and content.
        status, confirmed = await _call(tmp_path, "POST", f"{base}/{lessons['draft_id']}/confirm",
                                        {"title": "Lessons: build", "content": "- MARKER-EDITED"})
        assert status == 200 and confirmed["document"]["source_kind"] == "text"
        loaded = await knowledge.load_knowledge(tmp_path, project, str(project_dir))
        assert "- Lessons: build: What task-1 learned (Lessons-build.md)" in loaded.index.splitlines()
        assert (Path(loaded.add_dirs[0]) / "Lessons-build.md").read_text() == "- MARKER-EDITED"

        status, discarded = await _call(tmp_path, "DELETE", f"{base}/{other['draft_id']}")
        assert status == 200 and discarded["drafts"] == []
        view = await knowledge.knowledge_view(project["id"], tmp_path)
        assert [d["title"] for d in view["documents"]] == ["Lessons: build"]

    _run(_test, monkeypatch)


def test_a_refused_confirm_or_discard_leaves_the_draft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import knowledge
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        other = await projects.create_project(name="other", pool=pool)
        draft = (await knowledge.add_draft(project["id"], task_id=None, title="Lessons: calc", summary="s",
                                           content="- c"))["draft_id"]
        base, elsewhere = (f"/api/projects/{p['id']}/knowledge/drafts" for p in (project, other))
        for method, path, body, status, says in [
            ("POST", f"{base}/{draft}/confirm", {"title": "two\nlines"}, 400, "one line"),
            ("POST", f"{base}/{draft}/confirm", {"content": "  "}, 400, "needs its content"),
            ("POST", f"{base}/{draft}/confirm", {"path": "a.md"}, 400, "unknown field 'path'"),
            ("POST", f"{base}/{draft}/confirm", [], 400, "JSON object"),
            ("POST", f"{base}/draft-nope/confirm", {}, 404, "no draft"),
            ("DELETE", f"{base}/draft-nope", None, 404, "no draft"),
            ("POST", f"{elsewhere}/{draft}/confirm", {}, 404, "no draft"),
            ("DELETE", f"{elsewhere}/{draft}", None, 404, "no draft"),
            ("GET", "/api/projects/proj-nope/knowledge/drafts", None, 404, "not found"),
        ]:
            answer = await _call(tmp_path, method, path, body)
            assert answer[0] == status and says in answer[1]["error"], (method, path, answer)
        # A confirm over a project cap is refused too.
        monkeypatch.setattr(knowledge, "PROJECT_MAX_DOCUMENTS", 0)
        status, refused = await _call(tmp_path, "POST", f"{base}/{draft}/confirm", {})
        assert status == 400 and "at most 0 documents" in refused["error"]

        assert [d["draft_id"] for d in (await knowledge.drafts_view(project["id"]))["drafts"]] == [draft]
        assert await pool.fetchval("SELECT count(*) FROM project_documents") == 0

    _run(_test, monkeypatch)
