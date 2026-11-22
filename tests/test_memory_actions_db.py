"""Operator memory actions bind confirmations to shown bytes, with temporary synthetic vaults only."""

import json
from datetime import datetime, timezone

import pytest

from looper.agents import memory_actions, memory_model as model, memory_write
from looper.db import memory as db_memory
from tests import memory_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def pending(pool, settings, *, changes=None, statement="Conclusion first"):
    number = await sup.candidate(pool, statement=statement)
    row = (await db_memory.get(number, db=pool))["candidate"]
    changes = changes or [sup.NOTE, sup.INDEX]
    async with db_memory.transaction(pool) as conn:
        row = await db_memory.add_revision(
            conn, number, action=model.Action.DRAFT, actor=model.Actor.SYSTEM, via=model.Via.SYSTEM,
            made_by=model.MadeBy.DRAFT, changes=changes, title="Conclusion first", subject=memory_write.subject(
                settings, {**row, "title": "Conclusion first"}), diff_stats={}, note_path=None,
            pending_days=7, now=datetime.now(timezone.utc))
    return number, row["content_hash"]


def test_confirm_is_bound_to_hash_and_disabled_does_not_write(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        number, digest = await pending(pool, settings)
        with pytest.raises(db_memory.MemoryRefused) as caught:
            await memory_actions.act("confirm", f"M-{number}", content_hash="0" * 64, db=pool)
        assert caught.value.code == "stale"
        with pytest.raises(db_memory.MemoryRefused):
            await memory_actions.act("confirm", f"M-{number}", content_hash=digest, via=model.Via.CLI, db=pool)
        assert (await db_memory.get(number, db=pool))["candidate"]["state"] == "pending"
        result = await memory_actions.act("confirm", f"M-{number}", content_hash=digest, db=pool)
        assert result["candidate"]["state"] == "confirmed"
        assert not (repo / "mem/prefs/weekly.md").exists()
        await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_write'", json.dumps(
            {**settings.to_json(), "enabled": False}))
        before = await pool.fetchval("SELECT count(*) FROM memory_candidate_events")
        with pytest.raises(db_memory.MemoryRefused) as caught:
            await memory_actions.act("delete", f"M-{number}", db=pool)
        assert caught.value.code == "disabled"
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events") == before
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 1

    run_db(check, monkeypatch)


def test_dm_confirmation_requires_operator_and_complete_current_post(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        number, digest = await pending(pool, settings)
        post = await pool.fetchval(
            "INSERT INTO memory_posts(candidate_id,kind,revision,content_hash,complete,channel_id,chat,text,state) "
            "VALUES($1,'push',1,$2,false,'synthetic-bot','synthetic-dm','shown','sent') RETURNING id", number, digest)
        kwargs = {"content_hash": digest, "via": model.Via.LARK, "db": pool, "post_id": post,
                  "channel_id": "synthetic-bot", "chat": "synthetic-dm", "message_id": "reply-synthetic"}
        for verified in (False, True):
            with pytest.raises(db_memory.MemoryRefused):
                await memory_actions.act("confirm", f"M-{number}", operator_verified=verified, **kwargs)
        await pool.execute("UPDATE memory_posts SET complete=true WHERE id=$1", post)
        await pool.execute("UPDATE memory_posts SET content_hash=$2 WHERE id=$1", post, "f" * 64)
        with pytest.raises(db_memory.MemoryRefused) as caught:
            await memory_actions.act("confirm", f"M-{number}", operator_verified=True, **kwargs)
        assert caught.value.code == "stale"
        await pool.execute("UPDATE memory_posts SET content_hash=$2 WHERE id=$1", post, digest)
        result = await memory_actions.act("confirm", f"M-{number}", operator_verified=True, **kwargs)
        assert result["candidate"]["state"] == "confirmed"

    run_db(check, monkeypatch)


@pytest.mark.parametrize("confirm", [False, True])
def test_edit_revalidates_and_save_confirm_is_atomic(monkeypatch, tmp_path, confirm):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        number, digest = await pending(pool, settings)
        for changes in ([{**sup.NOTE, "path": "mine/escape.md"}, sup.INDEX],
                        [{**sup.NOTE, "content": "missing frontmatter"}, sup.INDEX],
                        [sup.NOTE]):
            with pytest.raises((ValueError, db_memory.MemoryRefused, memory_write.WriteFailure)):
                await memory_actions.act("edit", f"M-{number}", content_hash=digest, changes=changes, db=pool)
        unchanged = await db_memory.get(number, db=pool)
        assert unchanged["candidate"]["revision"] == 1
        changes = [{**sup.NOTE, "content": sup.NOTE["content"] + "Details second.\n"}, sup.INDEX]
        result = await memory_actions.act("edit", f"M-{number}", content_hash=digest, changes=changes,
                                          confirm=confirm, db=pool)
        assert result["candidate"]["state"] == ("confirmed" if confirm else "pending")
        assert result["candidate"]["revision"] == 2
        assert result["candidate"]["content_hash"] != digest
        found = await db_memory.get(number, db=pool)
        assert [event["action"] for event in found["events"]][-2:] == (["edit", "confirm"] if confirm else ["draft", "edit"])
        assert found["candidate"]["confirmed_hash"] == (found["candidate"]["content_hash"] if confirm else None)
        assert not (repo / "mem/prefs/weekly.md").exists()

    run_db(check, monkeypatch)


def test_reject_retry_delete_preserve_contracts(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        number, digest = await pending(pool, settings)
        await memory_actions.act("confirm", f"M-{number}", content_hash=digest, db=pool)
        await sup.move(pool, number, "writing", "claim")
        with pytest.raises(db_memory.MemoryRefused):
            await memory_actions.act("delete", f"M-{number}", db=pool)
        await sup.move(pool, number, "blocked", "block", reason="dirty_target")
        retried = await memory_actions.act("retry", f"M-{number}", db=pool, via=model.Via.CLI)
        assert retried["candidate"]["state"] == "confirmed"
        await sup.move(pool, number, "writing", "claim")
        await sup.move(pool, number, "blocked", "block", reason="dirty_target")
        rejected = await memory_actions.act("reject", f"M-{number}", reason="not useful", db=pool)
        assert rejected["candidate"]["state"] == "rejected"
        result = await memory_actions.act("delete", f"M-{number}", db=pool)
        assert result == {"candidate": model.format_id(number), "deleted": True}
        assert await db_memory.get(number, db=pool) is None
        assert await db_memory.tombstone_count(db=pool) == 1
        assert not (repo / "mem/prefs/weekly.md").exists()

    run_db(check, monkeypatch)


@pytest.mark.parametrize("changed", [False, True])
def test_revive_replays_or_returns_to_drafting(monkeypatch, tmp_path, changed):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        number, digest = await pending(pool, settings)
        await sup.move(pool, number, "expired", "expire")
        if changed:
            (repo / "mem/prefs/weekly.md").write_text("written independently\n")
        result = await memory_actions.act("revive", f"M-{number}", db=pool)
        assert result["candidate"]["state"] == ("drafting" if changed else "pending")
        assert result["candidate"]["content_hash"] == digest
        assert (await db_memory.get(number, db=pool))["candidate"]["confirmed_hash"] is None

    run_db(check, monkeypatch)
