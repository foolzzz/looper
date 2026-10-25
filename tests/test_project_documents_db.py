"""Project knowledge against a real PostgreSQL: the table, the API and what a run loads.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


async def _call(root: Path, action: str, project_id: str, body: Any = None, **ids) -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter
    from tests.db_support import call_v1

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), task_router=TaskRouter(runtime_root=root),
                             supervisor=SimpleNamespace(bus=None), runtime_root=root)
    data = {"project_id": project_id, **ids}
    if action == "add":
        data["document"] = body
    elif action in ("update", "confirm_draft") and body is not None:
        data["fields"] = body
    return await call_v1(server, "/api/v1/project_knowledge/" + action, data)


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

    run_db(_test, monkeypatch)


def test_the_api_adds_lists_changes_and_deletes_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_dir: Path,
) -> None:
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(project_dir), pool=pool)
        project_id = project["id"]
        status, text = await _call(tmp_path, "add", project_id, {
            "source_kind": "text", "title": "Glossary", "summary": "Terms", "content": "MARKER-ALPHA · 中文"})
        assert status == 200
        status, upload = await _call(tmp_path, "add", project_id, {
            "source_kind": "upload", "title": "Schema", "summary": "Tables", "filename": "schema.sql",
            "content_base64": base64.b64encode(b"CREATE TABLE t();\n").decode()})
        assert status == 200
        status, path = await _call(tmp_path, "add", project_id, {
            "source_kind": "path", "title": "Runbook", "summary": "Deploys", "path": "./docs/runbook.md"})
        assert status == 200

        status, view = await _call(tmp_path, "list", project_id)
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
        assert (await _call(tmp_path, "add", project_id, {
            "source_kind": "path", "title": "x", "summary": "y", "path": "../secret.txt"}))[0] == 400
        assert (await _call(tmp_path, "add", project_id, {
            "source_kind": "upload", "title": "x", "summary": "y", "filename": "a.bin",
            "content_base64": base64.b64encode(b"\x00\x01\x02").decode()}))[0] == 400
        assert await pool.fetchval("SELECT count(*) FROM project_documents") == 3

        doc_id = text["document"]["document_id"]
        status, changed = await _call(tmp_path, "update", project_id, {"content": "MARKER-GAMMA"}, document_id=doc_id)
        assert status == 200 and changed["document"]["sha256"] == _sha("MARKER-GAMMA")
        row = await pool.fetchrow("SELECT content, size_bytes, updated_at, created_at FROM project_documents "
                                  "WHERE id = $1", doc_id)
        assert (row["content"], row["size_bytes"]) == ("MARKER-GAMMA", 12) and row["updated_at"] > row["created_at"]

        status, after = await _call(tmp_path, "delete", project_id, document_id=upload["document"]["document_id"])
        assert status == 200 and after["count"] == 2
        assert (await _call(tmp_path, "delete", project_id, document_id=upload["document"]["document_id"]))[0] == 404
        assert path["document"]["read_path"] == str(project_dir / "docs" / "runbook.md")

    run_db(_test, monkeypatch)


def test_the_api_shows_one_document_with_its_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_dir: Path,
) -> None:
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(project_dir), pool=pool)
        other = await projects.create_project(name="other", pool=pool)
        project_id = project["id"]
        _, text = await _call(tmp_path, "add", project_id, {
            "source_kind": "text", "title": "Glossary", "summary": "Terms", "content": "MARKER-ALPHA · 中文"})
        _, path = await _call(tmp_path, "add", project_id, {
            "source_kind": "path", "title": "Runbook", "summary": "Deploys", "path": "docs/runbook.md"})
        text_id, path_id = text["document"]["document_id"], path["document"]["document_id"]

        status, shown = await _call(tmp_path, "get", project_id, document_id=text_id)
        assert status == 200
        assert (shown["document"]["title"], shown["content"]) == ("Glossary", "MARKER-ALPHA · 中文")
        # A path document shows its file as it is now.
        (project_dir / "docs" / "runbook.md").write_text("Deploy with MARKER-DELTA.\n", encoding="utf-8")
        status, shown = await _call(tmp_path, "get", project_id, document_id=path_id)
        assert (status, shown["content"]) == (200, "Deploy with MARKER-DELTA.\n")
        (project_dir / "docs" / "runbook.md").unlink()
        status, shown = await _call(tmp_path, "get", project_id, document_id=path_id)
        assert status == 200 and shown["content"] is None and shown["document"]["missing"]
        assert "missing" in shown["content_error"]

        assert (await _call(tmp_path, "get", project_id, document_id="doc-nope"))[0] == 404
        assert (await _call(tmp_path, "get", other["id"], document_id=text_id))[0] == 404

    run_db(_test, monkeypatch)


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

    run_db(_test, monkeypatch)


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

    run_db(_test, monkeypatch)


def test_a_draft_stays_out_of_runs_until_it_is_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_dir: Path,
) -> None:
    """A draft is in no run's index or mounts; confirmed, it is a text document every run gets; discarded, gone."""
    from looper.agents import knowledge
    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(project_dir), pool=pool)
        await tasks.create_task(task_id="task-1", requirement="r", work_dir="/w", project_id=project["id"], pool=pool)
        project_id = project["id"]
        lessons = await knowledge.add_draft(project["id"], task_id="task-1", title="Lessons: calc",
                                            summary="What task-1 learned", content="- MARKER-LESSON")
        other = await knowledge.add_draft(project["id"], task_id=None, title="Lessons: other", summary="s",
                                          content="- other")

        assert await knowledge.load_knowledge(tmp_path, project, str(project_dir)) == knowledge.EMPTY_KNOWLEDGE
        status, listed = await _call(tmp_path, "drafts", project_id)
        assert status == 200 and [(d["title"], d["task_id"], d["content"]) for d in listed["drafts"]] == [
            ("Lessons: calc", "task-1", "- MARKER-LESSON"), ("Lessons: other", None, "- other")]

        # Confirmed with an edited title and content.
        status, confirmed = await _call(tmp_path, "confirm_draft", project_id,
                                        {"title": "Lessons: build", "content": "- MARKER-EDITED"}, draft_id=lessons["draft_id"])
        assert status == 200 and confirmed["document"]["source_kind"] == "text"
        loaded = await knowledge.load_knowledge(tmp_path, project, str(project_dir))
        assert "- Lessons: build: What task-1 learned (Lessons-build.md)" in loaded.index.splitlines()
        assert (Path(loaded.add_dirs[0]) / "Lessons-build.md").read_text() == "- MARKER-EDITED"

        status, discarded = await _call(tmp_path, "discard_draft", project_id, draft_id=other["draft_id"])
        assert status == 200 and discarded["drafts"] == []
        view = await knowledge.knowledge_view(project["id"], tmp_path)
        assert [d["title"] for d in view["documents"]] == ["Lessons: build"]

    run_db(_test, monkeypatch)


def test_a_refused_confirm_or_discard_leaves_the_draft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import knowledge
    from looper.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        other = await projects.create_project(name="other", pool=pool)
        draft = (await knowledge.add_draft(project["id"], task_id=None, title="Lessons: calc", summary="s",
                                           content="- c"))["draft_id"]
        project_id = project["id"]
        for action, target_project, target_draft, body, status, says in [
            ("confirm_draft", project_id, draft, {"title": "two\nlines"}, 400, "one line"),
            ("confirm_draft", project_id, draft, {"content": "  "}, 400, "needs its content"),
            ("confirm_draft", project_id, draft, {"path": "a.md"}, 400, "declared schema"),
            ("confirm_draft", project_id, draft, [], 400, "declared schema"),
            ("confirm_draft", project_id, "draft-nope", {}, 404, "no draft"),
            ("discard_draft", project_id, "draft-nope", None, 404, "no draft"),
            ("confirm_draft", other["id"], draft, {}, 404, "no draft"),
            ("discard_draft", other["id"], draft, None, 404, "no draft"),
            ("drafts", "proj-nope", None, None, 404, "not found"),
        ]:
            ids = {"draft_id": target_draft} if target_draft is not None else {}
            answer = await _call(tmp_path, action, target_project, body, **ids)
            assert answer[0] == status and says in answer[1]["error"], (action, answer)
        # A confirm over a project cap is refused too.
        monkeypatch.setattr(knowledge, "PROJECT_MAX_DOCUMENTS", 0)
        status, refused = await _call(tmp_path, "confirm_draft", project_id, {}, draft_id=draft)
        assert status == 400 and "at most 0 documents" in refused["error"]

        assert [d["draft_id"] for d in (await knowledge.drafts_view(project["id"]))["drafts"]] == [draft]
        assert await pool.fetchval("SELECT count(*) FROM project_documents") == 0

    run_db(_test, monkeypatch)
