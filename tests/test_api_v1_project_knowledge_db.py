"""Knowledge HTTP contracts, ownership and transactions against disposable PostgreSQL."""

import base64
import json
from contextlib import asynccontextmanager

import pytest

from looper.agents import api_v1, knowledge
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.v1_domains.project_knowledge import DOCUMENT, DRAFT, DRAFTS_VIEW, KNOWLEDGE_VIEW
from looper.db import api_clients, project_documents, projects
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
TEXT = {"source_kind": "text", "title": "Guide", "summary": "Reference", "content": "Original.\n"}


def _validate(schema, value):
    api_v1.validate_arguments({"type": "object", "properties": {"result": schema}}, {"result": value})


def _source(kind, content, path="guide.md"):
    return {"title": "Guide", "summary": "Reference", "source_kind": kind, **(
        {"content": content} if kind == "text" else {"path": path} if kind == "path" else
        {"filename": "../../Guide Notes.md", "content_base64": base64.b64encode(content.encode()).decode()})}


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("Knowledge validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=None, task_router=server._task_router, runtime_root=tmp_path))

        async def call(action, body=None, *, client=False, method="POST"):
            path = action if action.startswith("/") else "/api/v1/" + (
                "confirm" if action == "confirm" else "project_knowledge/" + action)
            status, _, answer = await _send(server, f"{method} {path}", {
                **_bearer(token if client else TOKEN), "X-Looper-Session": "knowledge"}, json.dumps(body or {}))
            if status == 200 and not action.startswith("/"):
                _validate(api_v1.find(method, path).result, answer["result"])
            return status, answer

        yield call


@pytest.mark.parametrize("kind", ["text", "upload", "path"])
def test_source_add_update_and_readback_preserve_full_document_schemas(tmp_path, monkeypatch, kind):
    (tmp_path / "guide.md").write_text(TEXT["content"])
    (tmp_path / "changed.md").write_text("Changed.\n")

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        ids = {"project_id": project["id"]}
        async with _api(tmp_path, pool) as call:
            status, added = await call("add", {**ids, "document": _source(kind, TEXT["content"])}, client=True)
            assert status == 200, added
            _validate(KNOWLEDGE_VIEW, added["result"])
            doc = added["result"]["document"]
            _validate(DOCUMENT, doc)
            ids["document_id"] = doc["document_id"]
            assert doc["source_kind"] == kind and doc["missing"] is False
            assert doc["size_bytes"] == len(TEXT["content"].encode())
            assert (await call("get", ids, client=True))[1]["result"]["content"] == TEXT["content"]
            fields = {key: value for key, value in _source(kind, "Changed.\n", "changed.md").items()
                      if key not in {"title", "summary"}}
            status, changed = await call("update", {**ids, "fields": {**fields, "title": "Changed"}}, client=True)
            assert status == 200 and changed["result"]["document"]["title"] == "Changed", changed
            assert (await call("get", ids))[1]["result"]["content"] == "Changed.\n"
            view = (await call("list", {"project_id": project["id"]}, client=True))[1]["result"]
            assert view["count"] == 1 and view["index_chars"] == len(view["index"])
            assert view["stored_bytes"] == (0 if kind == "path" else len("Changed.\n"))
            assert (await project_documents.list_documents(project["id"]))[0]["title"] == "Changed"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("kind", "stray"), [
    ("text", {"path": "guide.md"}), ("text", {"filename": "guide.md"}),
    ("upload", {"content": "Wrong"}), ("path", {"content_base64": "WA=="}), ("text", {"unknown": "x"}),
])
@pytest.mark.parametrize("action", ["add", "update"])
def test_source_specific_and_unknown_fields_never_mutate_documents(tmp_path, monkeypatch, kind, stray, action):
    (tmp_path / "guide.md").write_text("Original.\n")

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(tmp_path))
        added = await knowledge.add_document(project["id"], _source(kind, TEXT["content"]), tmp_path)
        before = await project_documents.list_documents(project["id"])
        body = {"project_id": project["id"], **({"document": {**_source(kind, "Changed"), **stray}}
            if action == "add" else {"document_id": added["document"]["document_id"], "fields": stray})}
        async with _api(tmp_path, pool) as call:
            status, answer = await call(action, body, client=True)
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await project_documents.list_documents(project["id"]) == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("kind", ["upload", "path"])
def test_source_kind_cannot_change(tmp_path, monkeypatch, kind):
    async def run(pool):
        project = await projects.create_project(name="Example")
        added = await knowledge.add_document(project["id"], TEXT, tmp_path)
        ids = {"project_id": project["id"], "document_id": added["document"]["document_id"]}
        before = await project_documents.list_documents(project["id"])
        async with _api(tmp_path, pool) as call:
            status, answer = await call("update", {**ids, "fields": {"source_kind": kind, "title": "Changed"}})
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert (await call("get", ids))[1]["result"]["document"]["source_kind"] == "text"
            assert await project_documents.list_documents(project["id"]) == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("fields", "accepted"), [
    ({"title": "Renamed"}, True), ({"summary": "Updated summary"}, True),
    ({"source_kind": "text", "summary": "Same kind"}, True), ({}, False), ({"source_kind": "text"}, False),
])
def test_partial_update_changes_only_explicit_fields_and_requires_a_change(tmp_path, monkeypatch, fields, accepted):
    async def run(pool):
        project = await projects.create_project(name="Example")
        added = await knowledge.add_document(project["id"], TEXT, tmp_path)
        ids = {"project_id": project["id"], "document_id": added["document"]["document_id"]}
        async with _api(tmp_path, pool) as call:
            status, answer = await call("update", {**ids, "fields": fields}, client=True)
            assert status == (200 if accepted else 400), answer
            result = (await call("get", ids))[1]["result"]
            assert result["content"] == TEXT["content"]
            expected = {**TEXT, **(fields if accepted else {})}
            assert {key: result["document"][key] for key in ("title", "summary", "source_kind")} == {
                key: expected[key] for key in ("title", "summary", "source_kind")}
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("case", ["escape", "symlink", "missing", "nontext", "directory"])
def test_unsafe_or_unreadable_path_sources_are_rejected_without_writes(tmp_path, monkeypatch, case):
    work = tmp_path / "project"
    work.mkdir()
    (work / "docs").mkdir()
    (tmp_path / "outside.md").write_text("Private.")
    (work / "link.md").symlink_to(tmp_path / "outside.md")
    (work / "binary.md").write_bytes(b"\xff\x00")
    path = {"escape": "../outside.md", "symlink": "link.md", "missing": "absent.md",
            "nontext": "binary.md", "directory": "docs"}[case]

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(work))
        async with _api(tmp_path, pool) as call:
            status, answer = await call("add", {"project_id": project["id"], "document": _source("path", "", path)})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await project_documents.list_documents(project["id"]) == []
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("case", ["missing", "binary", "symlink"])
def test_live_path_read_errors_preserve_nullable_content_and_missing_warning(tmp_path, monkeypatch, case):
    work = tmp_path / "project"
    work.mkdir()
    target = work / "guide.md"
    target.write_text("Original.")

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(work))
        added = await knowledge.add_document(project["id"], _source("path", ""), tmp_path)
        target.unlink()
        if case == "binary":
            target.write_bytes(b"\x00")
        elif case == "symlink":
            (tmp_path / "outside.md").write_text("Private.")
            target.symlink_to(tmp_path / "outside.md")
        async with _api(tmp_path, pool) as call:
            status, answer = await call("get", {"project_id": project["id"],
                                                 "document_id": added["document"]["document_id"]})
            assert status == 200 and answer["result"]["content"] is None and answer["result"]["content_error"]
            assert answer["result"]["document"]["missing"] is (case != "binary")
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("limit", ["DOCUMENT_MAX_BYTES", "PROJECT_MAX_DOCUMENTS", "PROJECT_MAX_BYTES", "INDEX_MAX_CHARS"])
@pytest.mark.parametrize("action", ["add", "update"])
def test_capacity_overflow_preserves_database_content(tmp_path, monkeypatch, limit, action):
    async def run(pool):
        project = await projects.create_project(name="Example")
        added = await knowledge.add_document(project["id"], TEXT, tmp_path)
        before = await project_documents.list_documents(project["id"])
        monkeypatch.setattr(knowledge, limit, 0)
        async with _api(tmp_path, pool) as call:
            body = {"project_id": project["id"], **({"document": TEXT} if action == "add" else {
                "document_id": added["document"]["document_id"], "fields": {"content": "Changed"}})}
            status, answer = await call(action, body)
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await project_documents.list_documents(project["id"]) == before
            assert await project_documents.get_content(added["document"]["document_id"]) == TEXT["content"]
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["get", "update", "delete", "confirm_draft", "discard_draft"])
def test_document_and_draft_ids_cannot_cross_projects(tmp_path, monkeypatch, action):
    async def run(pool):
        owner, other = [await projects.create_project(name=name) for name in ("Owner", "Other")]
        added = await knowledge.add_document(owner["id"], TEXT, tmp_path)
        draft = await knowledge.add_draft(owner["id"], task_id=None, title="Lesson", summary="Summary", content="Learned.")
        is_draft = action.endswith("draft")
        body = {"project_id": other["id"], **({"draft_id": draft["draft_id"]} if is_draft else {
            "document_id": added["document"]["document_id"]}), **({"fields": {"title": "Changed"}} if action == "update" else {})}
        async with _api(tmp_path, pool) as call:
            status, answer = await call(action, body, client=True)
            assert status == 404 and answer["error"]["code"] == "not_found", answer
            assert (await knowledge.knowledge_view(owner["id"], tmp_path))["documents"] == added["documents"]
            assert (await knowledge.drafts_view(owner["id"]))["drafts"] == [draft]
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("failure", ["validation", "capacity", "insert"])
def test_failed_draft_confirmation_rolls_back_draft_removal(tmp_path, monkeypatch, failure):
    async def run(pool):
        project = await projects.create_project(name="Example")
        draft = await knowledge.add_draft(project["id"], task_id=None, title="Lesson", summary="Summary", content="Learned.")
        if failure == "capacity":
            monkeypatch.setattr(knowledge, "PROJECT_MAX_DOCUMENTS", 0)
        elif failure == "insert":
            await pool.execute("ALTER TABLE project_documents ADD CONSTRAINT refuse_lesson CHECK (title <> 'Lesson')")
        async with _api(tmp_path, pool) as call:
            status, _ = await call("confirm_draft", {"project_id": project["id"], "draft_id": draft["draft_id"],
                **({"fields": {"content": ""}} if failure == "validation" else {})})
            assert status == (503 if failure == "insert" else 400)
            assert (await knowledge.drafts_view(project["id"]))["drafts"] == [draft]
            assert await project_documents.list_documents(project["id"]) == []
    run_db(run, monkeypatch, timeout=30)


def test_confirm_draft_atomically_inserts_edited_document_and_preserves_other_drafts(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example")
        drafts = [await knowledge.add_draft(project["id"], task_id=None, title=title, summary="Summary", content="Learned.")
                  for title in ("Lesson", "Other")]
        async with _api(tmp_path, pool) as call:
            view = (await call("drafts", {"project_id": project["id"]}))[1]["result"]
            _validate(DRAFTS_VIEW, view)
            _validate(DRAFT, view["drafts"][0])
            assert view["count"] == 2
            fields = {"title": "Edited lesson", "summary": "Edited summary", "content": "Edited content."}
            status, answer = await call("confirm_draft", {"project_id": project["id"], "draft_id": drafts[0]["draft_id"],
                                                        "fields": fields}, client=True)
            assert status == 200 and answer["result"]["confirmed"] == drafts[0]["draft_id"], answer
            assert answer["result"]["count"] == 1
            document = answer["result"]["document"]
            assert {key: document[key] for key in ("title", "summary")} == {key: fields[key] for key in ("title", "summary")}
            assert await project_documents.get_content(document["document_id"]) == fields["content"]
            assert (await knowledge.drafts_view(project["id"]))["drafts"] == [drafts[1]]
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["delete", "discard_draft"])
@pytest.mark.parametrize("client", [True, False], ids=["client-confirmation", "operator-single-call"])
def test_destructive_operations_remove_only_target_after_required_confirmation(tmp_path, monkeypatch, action, client):
    async def run(pool):
        project = await projects.create_project(name="Example")
        documents = [await knowledge.add_document(project["id"], {**TEXT, "title": title}, tmp_path) for title in ("Guide", "Other")]
        drafts = [await knowledge.add_draft(project["id"], task_id=None, title=title, summary="Summary", content="Learned.")
                  for title in ("Lesson", "Other")]
        is_draft = action == "discard_draft"
        key = "draft_id" if is_draft else "document_id"
        target = drafts[0][key] if is_draft else documents[0]["document"][key]
        async with _api(tmp_path, pool) as call:
            status, answer = await call(action, {"project_id": project["id"], key: target}, client=client)
            if client:
                assert status == 202 and answer["status"] == "needs_confirmation" and target in answer["impact"]
                assert len(await project_documents.list_documents(project["id"])) == 2
                assert len(await project_documents.list_drafts(project["id"])) == 2
                status, answer = await call("confirm", {"confirmation_id": answer["confirmation_id"]}, client=True)
                _validate(api_v1.find("POST", "/api/v1/project_knowledge/" + action).result, answer["result"])
            assert status == 200 and answer["result"]["discarded" if is_draft else "deleted"] == target, answer
            assert answer["result"]["count"] == 1
            remaining_documents = await project_documents.list_documents(project["id"])
            remaining_drafts = await project_documents.list_drafts(project["id"])
            assert {row["id"] for row in remaining_documents} == {
                item["document"]["document_id"] for item in (documents if is_draft else documents[1:])}
            assert {row["id"] for row in remaining_drafts} == {
                item["draft_id"] for item in (drafts[1:] if is_draft else drafts)}
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("method", "action"), [
    ("GET", "list"), ("POST", "add"), ("GET", "get"), ("PUT", "update"), ("DELETE", "delete"),
    ("GET", "drafts"), ("POST", "confirm_draft"), ("DELETE", "discard_draft"),
])
def test_all_retired_methods_return_404_with_successful_v1_control(tmp_path, monkeypatch, method, action):
    async def run(pool):
        project = await projects.create_project(name="Example")
        added = await knowledge.add_document(project["id"], TEXT, tmp_path)
        draft = await knowledge.add_draft(project["id"], task_id=None, title="Lesson", summary="Summary", content="Learned.")
        path = f"/api/projects/{project['id']}/knowledge"
        body = {"project_id": project["id"]}
        if action in {"get", "update", "delete"}:
            path += "/" + added["document"]["document_id"]
            body["document_id"] = added["document"]["document_id"]
        elif action in {"drafts", "confirm_draft", "discard_draft"}:
            path += "/drafts"
            if action != "drafts":
                path += "/" + draft["draft_id"] + ("/confirm" if action == "confirm_draft" else "")
                body["draft_id"] = draft["draft_id"]
        if action == "add":
            body["document"] = TEXT
        elif action == "update":
            body["fields"] = {"summary": "Changed"}
        async with _api(tmp_path, pool) as call:
            assert (await call(path, method=method))[0] == 404
            status, answer = await call(action, body)
            assert status == 200 and answer["status"] == "ok", answer
    run_db(run, monkeypatch, timeout=30)
