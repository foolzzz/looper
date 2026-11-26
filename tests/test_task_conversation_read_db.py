"""The operator's backward history and task state contract, without a model call."""

import json
from types import SimpleNamespace

import pytest

from looper.agents import api_v1
from looper.agents.http_server import AgentHTTPServer
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import _plain
from looper.agents.tiers import task_manager
from looper.db import chat
from tests.db_support import run_db


def api():
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
        task_router=SimpleNamespace(), client_ops=ClientOps(SimpleNamespace()), operator_token="synthetic-token")

    async def call(**body):
        path = "/api/v1/operator_chat/messages"
        op = api_v1.find("POST", path)
        status, answer = await server._answer_v1("POST", path, op, {}, json.dumps(body).encode(), api_v1.OPERATOR)
        if answer["status"] == "ok":
            api_v1.validate_arguments(op.result, answer["result"])
        return int(status.split()[0]), answer
    return call


async def seed(pool, session="web-history", count=450):
    # Every timestamp ties, including both boundaries: the row id must break ties.
    await pool.execute("""
        INSERT INTO chat_messages (session_id, message_id, role, content, channel, created_at)
        SELECT $1, 'message-' || n, 'user', 'text-' || n, 'web', '2026-01-01'::timestamptz
        FROM generate_series(1, $2::int) n ORDER BY n
        """, session, count)


def test_backward_pages_cover_every_row_once_and_freeze_legacy_messages(monkeypatch):
    async def check(pool):
        await seed(pool)
        call = api()
        legacy = json.dumps(_plain(await chat.list_messages("web-history")), separators=(",", ":"))
        status, answer = await call(session_id="web-history")
        assert status == 200
        assert json.dumps(answer["result"]["messages"], separators=(",", ":")) == legacy
        pages = []
        before = None
        for size, has_more in [(200, True), (200, True), (50, False)]:
            status, answer = await call(session_id="web-history", limit=200, **({"before": before} if before else {}))
            assert status == 200, answer
            page = answer["result"]
            assert len(page["messages"]) == size and page["has_more"] is has_more
            before = page["next_before"]
            assert (isinstance(before, str) and before not in {m["message_id"] for m in page["messages"]}) if has_more else before is None
            pages.insert(0, page["messages"])
        assert [m["message_id"] for page in pages for m in page] == [f"message-{n}" for n in range(1, 451)]
        assert "turn_state" not in answer["result"]
    run_db(check, monkeypatch)


@pytest.mark.parametrize("limit", [1, 200])
def test_page_size_boundaries(monkeypatch, limit):
    async def check(pool):
        await seed(pool, count=201)
        status, answer = await api()(session_id="web-history", limit=limit)
        assert status == 200 and len(answer["result"]["messages"]) == limit
        assert answer["result"]["has_more"] is True
        assert answer["result"]["messages"][-1]["message_id"] == "message-201"
        status, older = await api()(session_id="web-history", before=answer["result"]["next_before"], limit=limit)
        assert status == 200 and older["result"]["messages"][-1]["message_id"] == f"message-{201 - limit}"
    run_db(check, monkeypatch)


@pytest.mark.parametrize("limit", [0, 201, True, "1"])
def test_invalid_page_sizes_are_rejected(monkeypatch, limit):
    async def check(pool):
        status, answer = await api()(session_id="web-history", limit=limit)
        assert status == 400 and answer["error"]["code"] == "invalid"
    run_db(check, monkeypatch)


def test_cursors_are_opaque_validated_and_bound_to_the_session(monkeypatch):
    async def check(pool):
        await seed(pool, count=3)
        await seed(pool, session="other-history", count=3)
        call = api()
        status, answer = await call(session_id="web-history", limit=1)
        assert status == 200
        cursor = answer["result"]["next_before"]
        for before in ["not-a-cursor", "message-2", cursor[:-4] + "aaaa"]:
            status, answer = await call(session_id="web-history", before=before)
            assert status == 400 and answer["error"]["code"] == "invalid_cursor"
        status, answer = await call(session_id="other-history", before=cursor)
        assert status == 400 and answer["error"]["code"] == "cursor_session_mismatch"
        status, answer = await call(session_id="web-history", before=cursor, limit=1)
        assert status == 200 and answer["result"]["messages"][0]["message_id"] == "message-2"
    run_db(check, monkeypatch)


def test_turn_state_reads_idle_interrupted_and_running_through_operation(monkeypatch):
    busy = set()
    monkeypatch.setattr(task_manager, "current_pool", lambda: SimpleNamespace(in_turn=lambda task: task in busy))
    async def check(pool):
        call = api()
        async def state(expected):
            status, answer = await call(session_id=chat.task_thread("example-task"))
            assert status == 200 and answer["result"]["turn_state"] == expected
        await state("idle")
        await chat.save_message(session_id=chat.task_thread("example-task"), message_id="u", role="user", content="hi")
        await state("interrupted")
        busy.add("example-task")
        await state("running")
        busy.clear()
        await chat.save_message(session_id=chat.task_thread("example-task"), message_id="a", role="assistant", content="ok")
        await state("idle")
    run_db(check, monkeypatch)
