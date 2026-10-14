"""Project knowledge against a real PostgreSQL: the table, the API and what a run loads.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database. Each
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

DSN = os.environ.get("LOOPX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _fresh_schema() -> AsyncIterator[Any]:
    import asyncpg

    schema = f"loopx_test_{uuid.uuid4().hex[:12]}"
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
    from loopx.db import connection

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
    from loopx.agents.http_server import AgentHTTPServer
    from loopx.agents.tiers.task import TaskRouter

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

    from loopx.db import connection, projects

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
        assert await projects.delete_project(project["id"], pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM project_documents") == 0

    _run(_test, monkeypatch)


def test_the_api_adds_lists_changes_and_deletes_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_dir: Path,
) -> None:
    from loopx.db import projects

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


def test_concurrent_adds_cannot_both_pass_the_count_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from loopx.agents import knowledge
    from loopx.db import projects

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
    from loopx.agents import knowledge
    from loopx.db import projects

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="calc", work_dir=str(project_dir), pool=pool)
        await knowledge.add_document(project["id"], {
            "source_kind": "text", "title": "Glossary", "summary": "Terms", "content": "MARKER-ALPHA"}, tmp_path)
        await knowledge.add_document(project["id"], {
            "source_kind": "path", "title": "Runbook", "summary": "Deploys", "path": "docs/runbook.md"}, tmp_path)
        task_dir = tmp_path / "task"
        task_dir.mkdir()

        loaded = await knowledge.load_knowledge(tmp_path, project, str(task_dir))

        root = tmp_path / ".local" / "projects" / project["id"] / "knowledge"
        glossary = root / _sha("MARKER-ALPHA") / "Glossary.md"
        assert glossary.read_text() == "MARKER-ALPHA"
        assert loaded.add_dirs == (str(root), str(project_dir)) and loaded.documents == 2
        assert loaded.index.splitlines()[2:] == [
            f"- Glossary: Terms (read: {glossary})",
            f"- Runbook: Deploys (read: {project_dir / 'docs' / 'runbook.md'})"]
        # The view the API gives shows the same index the run got.
        assert (await knowledge.knowledge_view(project["id"], tmp_path))["index"] == loaded.index

    _run(_test, monkeypatch)
