"""API clients and who did what through one, on a real PostgreSQL: the client's token (only its hash is stored), its
name, revoking, the chat threads, tasks and Manager calls recorded against it, and the chat messages' cursor pages.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _save(pool: Any, session_id: str, message_id: str, content: str | None = None) -> None:
    from looper.db import chat

    await chat.save_message(session_id=session_id, message_id=message_id, role="user",
                            content=content or f"text of {message_id}", pool=pool)


def test_a_token_is_stored_only_as_its_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import api_clients

    async def test(pool: Any) -> None:
        view, token = await api_clients.create("agent", pool=pool)
        assert token.startswith("lpc_")
        assert set(view) == {"id", "name", "created_at", "last_used_at", "revoked_at"}
        row = await pool.fetchrow("SELECT * FROM api_clients WHERE id = $1", view["id"])
        assert row["token_sha256"] == api_clients.token_hash(token)
        assert token not in [str(value) for value in row.values()]
        found = await api_clients.find(token, pool=pool)
        assert found and found["id"] == view["id"] and found["name"] == "agent"
        assert await api_clients.find("lpc_wrong", pool=pool) is None
        assert await api_clients.find("not-a-token", pool=pool) is None

    _run(test, monkeypatch)


def test_names_are_checked_and_unique_among_the_active_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import api_clients

    async def test(pool: Any) -> None:
        view, _ = await api_clients.create(" ok ", pool=pool)
        assert view["name"] == "ok"
        for bad in ("", "a" * 41, "-x", "x\ny", "名字", None):
            with pytest.raises(api_clients.ClientNameError):
                await api_clients.create(bad, pool=pool)
        with pytest.raises(api_clients.ClientNameTaken):
            await api_clients.create("OK", pool=pool)
        assert await api_clients.revoke(view["id"], pool=pool)
        again, _ = await api_clients.create("OK", pool=pool)
        assert again["id"] != view["id"]

    _run(test, monkeypatch)


def test_a_revoked_client_cannot_be_found_but_is_still_named(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import api_clients

    async def test(pool: Any) -> None:
        view, token = await api_clients.create("agent", pool=pool)
        other, _ = await api_clients.create("other", pool=pool)
        assert [c["id"] for c in await api_clients.list_active(pool=pool)] == [other["id"], view["id"]]
        assert await api_clients.revoke(view["id"], pool=pool) is True
        assert await api_clients.find(token, pool=pool) is None
        assert [c["id"] for c in await api_clients.list_active(pool=pool)] == [other["id"]]
        assert await api_clients.names([view["id"], "client-none"], pool=pool) == {view["id"]: "agent"}
        assert await api_clients.names([], pool=pool) == {}
        assert await api_clients.revoke(view["id"], pool=pool) is False

    _run(test, monkeypatch)


def test_touch_moves_last_used_at_once_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import api_clients

    async def test(pool: Any) -> None:
        view, _ = await api_clients.create("agent", pool=pool)
        assert view["last_used_at"] is None
        await api_clients.touch(view["id"], pool=pool)
        first = await pool.fetchval("SELECT last_used_at FROM api_clients WHERE id = $1", view["id"])
        assert first is not None
        await api_clients.touch(view["id"], pool=pool)
        assert await pool.fetchval("SELECT last_used_at FROM api_clients WHERE id = $1", view["id"]) == first
        await pool.execute("UPDATE api_clients SET last_used_at = now() - interval '2 minutes' WHERE id = $1",
                           view["id"])
        await api_clients.touch(view["id"], pool=pool)
        assert await pool.fetchval(
            "SELECT last_used_at > now() - interval '1 minute' FROM api_clients WHERE id = $1", view["id"])

    _run(test, monkeypatch)


def test_a_thread_keeps_the_client_that_claimed_it_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import api_clients, chat

    async def test(pool: Any) -> None:
        one, _ = await api_clients.create("one", pool=pool)
        two, _ = await api_clients.create("two", pool=pool)
        assert await chat.thread_client("cli-1", pool=pool) is None
        assert await chat.claim_thread("cli-1", "manager", client_id=one["id"], pool=pool) == "manager"
        assert await chat.thread_client("cli-1", pool=pool) == one["id"]
        await chat.claim_thread("cli-1", "manager", client_id=two["id"], pool=pool)
        assert await chat.thread_client("cli-1", pool=pool) == one["id"]
        await chat.claim_thread("web-1", "manager", pool=pool)
        assert await chat.thread_client("web-1", pool=pool) is None

        threads = await chat.client_threads(one["id"], pool=pool)
        assert [t["session_id"] for t in threads] == ["cli-1"]
        assert threads[0]["manager_id"] == "manager" and threads[0]["created_at"]
        assert threads[0]["last_content"] is None and threads[0]["last_message_at"] is None
        await _save(pool, "cli-1", "m1", "first")
        await _save(pool, "cli-1", "m2", "x" * 300)
        threads = await chat.client_threads(one["id"], pool=pool)
        assert threads[0]["last_content"] == "x" * 200 and threads[0]["last_message_at"] is not None
        assert await chat.client_threads(two["id"], pool=pool) == []

    _run(test, monkeypatch)


def test_client_threads_list_the_most_recently_active_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import api_clients, chat

    async def test(pool: Any) -> None:
        one, _ = await api_clients.create("one", pool=pool)
        for session in ("quiet", "old", "new"):
            await chat.claim_thread(session, "manager", client_id=one["id"], pool=pool)
        await _save(pool, "old", "o1")
        await _save(pool, "new", "n1")
        assert [t["session_id"] for t in await chat.client_threads(one["id"], pool=pool)] == ["new", "old", "quiet"]
        assert [t["session_id"] for t in await chat.client_threads(one["id"], limit=2, pool=pool)] == ["new", "old"]

    _run(test, monkeypatch)


def test_messages_are_paged_forward_by_a_message_id_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import chat

    async def test(pool: Any) -> None:
        await _save(pool, "other", "x0")
        for n in (1, 2, 3):
            await _save(pool, "cli-1", f"m{n}")
            await _save(pool, "other", f"x{n}")
        for n in (4, 5):
            await _save(pool, "cli-1", f"m{n}")
        await _save(pool, "other", "x9")

        async def page(**kwargs: Any) -> tuple[list[str], bool]:
            messages, more = await chat.messages_page("cli-1", pool=pool, **kwargs)
            return [m["message_id"] for m in messages], more

        assert await page(limit=2) == (["m1", "m2"], True)
        assert await page(after="m2", limit=2) == (["m3", "m4"], True)
        assert await page(after="m4", limit=2) == (["m5"], False)
        assert await page(after="m5", limit=2) == ([], False)
        assert await page() == (["m1", "m2", "m3", "m4", "m5"], False)
        messages, _ = await chat.messages_page("cli-1", limit=1, pool=pool)
        assert set(messages[0]) == {"message_id", "role", "content", "created_at"}
        for cursor in ("nope", "x1"):
            with pytest.raises(chat.UnknownCursor):
                await page(after=cursor)
        await _save(pool, "cli-1", "m3", "edited")
        assert await page(after="m2", limit=3) == (["m3", "m4", "m5"], False)
        assert await page(after="m3") == (["m4", "m5"], False)

    _run(test, monkeypatch)


def test_a_task_and_a_manager_call_name_the_client_they_came_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import api_clients, manager_actions, projects, tasks

    async def test(pool: Any) -> None:
        client, _ = await api_clients.create("agent", pool=pool)
        project = await projects.create_project(name="calc", pool=pool)
        await tasks.create_task(task_id="task-a", requirement="a", project_id=project["id"],
                                client_id=client["id"], pool=pool)
        await tasks.create_task(task_id="task-b", requirement="b", project_id=project["id"], pool=pool)
        assert (await tasks.get_task("task-a", pool=pool))["client_id"] == client["id"]
        assert (await tasks.get_task("task-b", pool=pool))["client_id"] is None

        common: dict[str, Any] = dict(channel="web", chat="c", message_id="m", tool="cancel_task", arguments={},
                                      risk="write", status="ok", pool=pool)
        await manager_actions.record(client_id=client["id"], task_id="task-a", **common)
        await manager_actions.record(task_id="task-b", **common)
        [row] = await manager_actions.for_task("task-a", pool=pool)
        assert row["client_id"] == client["id"] and row["client_name"] == "agent"
        [row] = await manager_actions.for_task("task-b", pool=pool)
        assert row["client_id"] is None and row["client_name"] is None
        await api_clients.revoke(client["id"], pool=pool)
        [row] = await manager_actions.for_task("task-a", pool=pool)
        assert row["client_name"] == "agent"

    _run(test, monkeypatch)
