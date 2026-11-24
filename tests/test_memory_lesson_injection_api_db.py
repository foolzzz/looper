"""The operator can inspect and stop lessons without enabling the feature."""
from types import SimpleNamespace

import pytest

from tests.db_support import run_db, v1_api
from tests.memory_lessons_support import lesson


def test_lesson_cli_exposes_controls_without_activation():
    from looper.cli import build_parser

    parser = build_parser()
    listing = parser.parse_args(["memory", "lessons", "--project", "synthetic-project", "--state", "held", "--json"])
    assert (listing.memory_command, listing.project, listing.state, listing.json) == (
        "lessons", "synthetic-project", "held", True)
    revoke = parser.parse_args(["memory", "revoke", "M-0123", "--reason", "Contradicted by evidence"])
    assert revoke.candidate == "M-0123" and revoke.reason == "Contradicted by evidence"
    assert parser.parse_args(["memory", "renew", "M-0123"]).memory_command == "renew"
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["memory", "activate", "M-0123"])
    assert refused.value.code == 2


def test_operator_controls_refuse_client_and_agent_tokens(monkeypatch, tmp_path):
    async def check(pool):
        number = await lesson(pool, "synthetic-project", "Check the configuration")
        await pool.execute("UPDATE memory_candidates SET state='in_effect',reason=NULL WHERE id=$1", int(number[2:]))
        async with v1_api(tmp_path, pool) as call:
            for operation, body in (("memory.task_lessons", {"task_id": "task-Check the configuration"}),
                                    ("memory.revoke", {"candidate": number}),
                                    ("memory.renew", {"candidate": number})):
                assert (await call(operation, body, client=True))[0] == 403
            status, result = await call("memory.task_lessons", {"task_id": "task-Check the configuration", "limit": 1})
            assert status == 200, result
            assert result["result"]["initialized"] is False
            assert result["result"]["injections"] == []
            assert (await call("memory.task_lessons", {"task_id": "missing"}))[0] == 404
            assert (await call("memory.task_lessons", {"task_id": "task-Check the configuration", "limit": 0}))[0] == 400
            assert (await call("memory.task_lessons", {"task_id": "task-Check the configuration", "before": 2**63}))[0] == 400
            assert (await call("memory.revoke", {"candidate": number, "via": "cli"}))[0] == 400
            status, result = await call("memory.renew", {"candidate": number})
            assert status == 200, result
            assert result["result"]["candidate"]["state"] == "in_effect"
            status, result = await call("memory.revoke", {"candidate": number, "reason": "Contradicted by evidence"})
            assert status == 200, result
            assert result["result"]["candidate"]["state"] == "revoked"
            assert await pool.fetchval("SELECT count(*) FROM memory_tombstones") == 1
            event = await pool.fetchrow("SELECT * FROM memory_candidate_events WHERE action='revoke'")
            assert (event["actor_kind"], event["via"], event["note"]) == ("user", "dashboard", "Contradicted by evidence")
            assert (await call("memory.renew", {"candidate": number}))[0] == 409
            from tests.memory_support import candidate
            vault_id = await candidate(pool)
            for operation in ("memory.revoke", "memory.renew"):
                assert (await call(operation, {"candidate": f"M-{vault_id:04d}"}))[0] == 409
        from looper.agents import memory_commands
        code, output = await memory_commands._command(pool, SimpleNamespace(
            memory_command="lessons", project="synthetic-project", state="revoked", limit=1, before=None, json=True))
        assert code == 0 and number in output
    run_db(check, monkeypatch, timeout=60)


def test_task_readback_pages_delivery_items_with_verifiable_text(monkeypatch, tmp_path):
    import hashlib
    from looper.agents import memory_lesson_injection as service
    from looper.db import memory_lesson_injection as store

    async def check(pool):
        number = int((await lesson(pool, "synthetic-project", "Read history"))[2:])
        await pool.execute("UPDATE memory_candidates SET state='in_effect',reason=NULL WHERE id=$1", number)
        await pool.execute("INSERT INTO memory_candidate_events(candidate_id,action,actor_kind,via) "
                           "VALUES ($1,'activate','system','system')", number)
        await pool.execute("UPDATE projects SET memory_lessons='on'")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('memory_lessons','{\"enabled\":true}')")
        task_id = "task-Read history"
        block = await service.ensure_snapshot(task_id, "synthetic-project", db=pool)
        prepared = []
        for key in ("first", "second"):
            item = await store.prepare_injection(task_id, "worker", key, block, db=pool)
            prepared.append(item["id"])
            await store.mark_delivered(item["id"], db=pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.task_lessons", {"task_id": task_id, "limit": 1})
            assert status == 200, answer
            page = answer["result"]
            assert page["initialized"] and page["more"]
            row = page["injections"][0]
            assert row["id"] == prepared[-1] and row["state"] == "delivered"
            assert row["block_sha256"] == hashlib.sha256(row["block_text"].encode()).hexdigest()
            assert row["block_chars"] == len(row["block_text"])
            assert row["items"][0]["candidate_id"] == f"M-{number:04d}"
            status, answer = await call("memory.task_lessons", {"task_id": task_id, "limit": 1, "before": row["id"]})
            assert status == 200, answer
            assert not answer["result"]["more"]
            assert answer["result"]["injections"][0]["id"] == prepared[0]
    run_db(check, monkeypatch, timeout=60)


def test_lesson_cli_uses_operator_provenance_and_refuses_vault(monkeypatch):
    async def check(pool):
        from looper.agents import memory_commands
        number = await lesson(pool, "synthetic-project", "Keep evidence")
        await pool.execute("UPDATE memory_candidates SET state='in_effect',reason=NULL WHERE id=$1", int(number[2:]))
        code, _ = await memory_commands._command(pool, SimpleNamespace(memory_command="renew", candidate=number))
        assert code == 0
        assert await pool.fetchval("SELECT via FROM memory_candidate_events WHERE action='renew'") == "cli"
        code, _ = await memory_commands._command(pool, SimpleNamespace(memory_command="revoke", candidate=number, reason=None))
        assert code == 0
        code, _ = await memory_commands._command(pool, SimpleNamespace(memory_command="renew", candidate=number))
        assert code == memory_commands.WRONG_STATE
    run_db(check, monkeypatch, timeout=60)


def test_housekeeping_expires_only_when_enabled(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from looper.agents import housekeeping

    async def check(pool):
        number = await lesson(pool, "synthetic-project", "Expiring evidence")
        await pool.execute("UPDATE memory_candidates SET state='in_effect',reason=NULL,valid_until=now()-interval '1 day' "
                           "WHERE id=$1", int(number[2:]))
        await housekeeping.run_once(tmp_path, datetime.now(timezone.utc), current_runner=None, commands=None)
        assert await pool.fetchval("SELECT state FROM memory_candidates WHERE id=$1", int(number[2:])) == "in_effect"
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('memory_lessons','{\"enabled\":true}')")
        await housekeeping.run_once(tmp_path, datetime.now(timezone.utc), current_runner=None, commands=None)
        assert await pool.fetchval("SELECT state FROM memory_candidates WHERE id=$1", int(number[2:])) == "expired"
        assert await pool.fetchval("SELECT count(*) FROM memory_tombstones") == 0
    run_db(check, monkeypatch, timeout=60)
