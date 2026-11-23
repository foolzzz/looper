"""The leads' full-text search (assistant search RFC, section 4): ``leads.search_mode`` off (``words``) changes
nothing, ``fulltext`` matches Chinese by character pairs, any word, names and, with pg_trgm, English likeness; the
labelled set's recall; the Manager tool's description; and the pg_trgm migration, which degrades when the extension
cannot be created.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from evals import leads_search as labelled
from looper.agents import leads, leads_model
from looper.agents.ops import leads as ops_leads
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.ops import Turn
from looper.db import connection, message_leads
from looper.db.migrations import leads_trigram
from tests import leads_support as sup
from tests import subscription_support as stream
from tests.db_support import DSN, run_db, run_with_channel_configs
from tests.test_leads_cli_db import add, database, enable, run_cli  # noqa: F401 (database is a fixture)

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _mode(pool: Any, mode: str | None) -> None:
    if mode is None:
        await pool.execute("DELETE FROM system_settings WHERE key = $1", leads_model.LEADS_SEARCH_MODE_KEY)
    else:
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE "
                           "SET value = $2", leads_model.LEADS_SEARCH_MODE_KEY, mode)


async def _leads(pool: Any, *summaries: str, chat_name: str = "Release room") -> list[int]:
    await stream.source(pool)
    await stream.person(pool, "ou_alice", "Alice Example")
    ids = []
    for number, summary in enumerate(summaries):
        seq = await stream.add(pool, summary, chat_id="oc_release", chat_name=chat_name, sender="ou_alice",
                               sent_at=NOW - timedelta(hours=len(summaries) - number))
        ids.append(await sup.lead(pool, [seq], summary=summary, people=[("ou_alice", "sender")]))
    return ids


async def _found(pool: Any, query: str, **kwargs: Any) -> list[str]:
    result = await leads.search(words=query.split(), now=NOW, db=pool, **kwargs)
    return [lead["summary"] for lead in result["leads"]]


def test_the_mode_is_words_unless_it_says_fulltext(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        assert await leads_model.search_mode(pool) is leads_model.SearchMode.WORDS
        for stored, expected in (("fulltext", "fulltext"), ("words", "words"), ("FULLTEXT", "words"), ("", "words")):
            await _mode(pool, stored)
            assert await leads_model.search_mode(pool) == expected

    run_db(test, monkeypatch)


def test_words_mode_matches_every_word_as_written_and_fulltext_any_word_by_its_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        await _leads(pool, "Orion 的 CPU 报警阈值定为 85%", "Orion 昨晚出现内存泄漏", "周报模板已更新")
        assert await _found(pool, "告警阈值") == [] and await _found(pool, "Orion 内存泄露") == []
        await _mode(pool, "fulltext")
        assert await _found(pool, "告警阈值") == ["Orion 的 CPU 报警阈值定为 85%"]  # two of three pairs
        # Both words first; Orion alone scores under 60 % of that and is left out, but stands alone.
        assert await _found(pool, "Orion 内存泄露") == ["Orion 昨晚出现内存泄漏"]
        assert await _found(pool, "Orion") == ["Orion 昨晚出现内存泄漏", "Orion 的 CPU 报警阈值定为 85%"]
        assert await _found(pool, "Release") == await _found(pool, "Release room")  # a chat's name, as before
        assert len(await _found(pool, "Release")) == 3
        assert await _found(pool, "Alice") == await _found(pool, "Release")  # a person's name, as before
        assert await _found(pool, "nothing-like-this") == []
        assert await _found(pool, "") == await _found(pool, "a")  # no term: every lead, as words mode lists them
        # The filters apply as they do to words: here the chat's id.
        assert await _found(pool, "Orion", chat="oc_other") == []

    run_db(test, monkeypatch)


def test_an_english_word_is_also_matched_by_its_likeness_with_pg_trgm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        trigram = await message_leads.trigram_schema(pool)
        if trigram is None:
            pytest.skip("pg_trgm is not installed in this database")
        await _leads(pool, "Bob will deploy the Atlas API to staging", "Looper 新版本 release 定在下周二")
        await _mode(pool, "fulltext")
        assert await _found(pool, "deploying") == ["Bob will deploy the Atlas API to staging"]
        assert await _found(pool, "Loopr") == ["Looper 新版本 release 定在下周二"]
        rows, _ = await message_leads.search_rows(now=NOW, words=["deploying"], fulltext=True, trigram=None, db=pool)
        assert rows == []  # without pg_trgm only the parts match

    run_db(test, monkeypatch)


def test_on_the_labelled_set_fulltext_finds_more_and_never_less_than_words(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        measured = await labelled.measure(pool)
        words, fulltext = measured["words"], measured["fulltext"]
        for old, new in zip(words, fulltext):  # whatever the words found, fulltext finds
            assert set(old["relevant"]) & set(old["returned"]) <= set(new["returned"]), new["id"]
        chinese = [r for r in fulltext if r["category"] == "chinese"]
        assert (labelled.summary(chinese)["recall"]
                > labelled.summary([r for r in words if r["category"] == "chinese"])["recall"])
        assert labelled.summary(fulltext)["recall"] > labelled.summary(words)["recall"]
        assert labelled.summary(measured["fulltext_bilingual"])["recall"] >= 0.9
        assert labelled.summary(measured["fulltext_bilingual"])["precision"] >= labelled.summary(words)["precision"]

    run_db(test, monkeypatch, timeout=120)


def test_the_manager_tool_is_described_as_the_mode_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true')")
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok")
        turn = Turn(channel="web", chat="chat-1", message_id="m-1")

        async def described() -> str:
            (tool,) = [t for t in await ops.current_listing(turn) if t["name"] == ops_leads.LEADS_TOOL]
            return tool["description"]

        before = await described()
        assert before == ops_leads.TOOL.description and "发布 release" not in before
        await _mode(pool, "fulltext")
        assert await described() == ops_leads.TOOL_FULLTEXT.description
        assert ops_leads.TOOL_FULLTEXT.schema == ops_leads.TOOL.schema
        await _mode(pool, "words")
        assert await described() == before

    run_with_channel_configs(test, monkeypatch)


def test_the_command_line_searches_as_the_mode_says(tmp_path: Path, database: str, capsys: Any) -> None:  # noqa: F811
    import asyncio

    import asyncpg

    enable(database)
    add(database, summary="Orion 的 CPU 报警阈值定为 85%")

    async def fulltext() -> None:
        conn = await asyncpg.connect(database)
        try:
            await _mode(conn, "fulltext")
        finally:
            await conn.close()

    assert run_cli(tmp_path, database, "search", "告警阈值") == 1
    capsys.readouterr()
    asyncio.run(fulltext())
    assert run_cli(tmp_path, database, "search", "告警阈值") == 0
    assert "报警阈值" in capsys.readouterr().out


def test_pg_trgm_is_created_in_public_on_upgrade_and_a_database_without_it_still_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        try:
            await pool.execute("DROP EXTENSION IF EXISTS pg_trgm CASCADE")  # with the leads' trigram index
            await pool.execute("DELETE FROM schema_migrations WHERE name = 'leads_trigram'")
            async with pool.acquire() as conn:
                assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(pool)
            assert await message_leads.trigram_schema(pool) == "public"
            assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'leads_trigram'") == 1
            assert await pool.fetchval("SELECT to_regclass('idx_message_leads_trigram') IS NOT NULL")
            # Where the extension cannot be created, the start goes on, records the try and searches without it.
            await pool.execute("DROP EXTENSION pg_trgm CASCADE")
            await pool.execute("DELETE FROM schema_migrations WHERE name = 'leads_trigram'")
            monkeypatch.setattr(leads_trigram, "DDL",
                                leads_trigram.DDL.replace("pg_trgm", "pg_trgm_not_available"))
            await connection.ensure_schema(pool)
            assert await message_leads.trigram_schema(pool) is None
            assert await pool.fetchval("SELECT to_regclass('idx_message_leads_trigram') IS NULL")
            assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'leads_trigram'") == 1
            async with pool.acquire() as conn:
                assert not await connection.schema_change_pending(conn)
            await _leads(pool, "Orion 的 CPU 报警阈值定为 85%")
            await _mode(pool, "fulltext")
            assert await _found(pool, "告警阈值") == ["Orion 的 CPU 报警阈值定为 85%"]
        finally:  # the other tests of this database share the extension
            await pool.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public")

    run_db(test, monkeypatch, timeout=60)


def test_the_candidates_come_from_the_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.text_search import terms

    async def test(pool: Any) -> None:
        await _leads(pool, "Orion 的 CPU 报警阈值定为 85%")
        trigram = await message_leads.trigram_schema(pool)
        found = await terms(["告警阈值", "deploying"], pool)
        sql, args = message_leads.fulltext_query(found, trigram, now=NOW)
        async with pool.acquire() as conn, conn.transaction():
            # The plan without sequential scans shows that every lead set is indexable.
            await conn.execute("SET LOCAL enable_seqscan = off")
            plan = "\n".join(row[0] for row in await conn.fetch("EXPLAIN " + sql, *args))
        assert plan.count("Bitmap Index Scan on idx_message_leads_search") == 2, plan
        if trigram is not None:
            assert "Bitmap Index Scan on idx_message_leads_trigram" in plan, plan
        assert "Seq Scan on message_leads" not in plan, plan
        assert await _found(pool, "告警阈值") == []  # words mode: nothing; the plan above is the fulltext query

    run_db(test, monkeypatch, timeout=60)


def test_a_database_without_the_token_index_builds_it_online(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await pool.execute("DROP INDEX idx_message_leads_search")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT indisvalid FROM pg_index "
                                   "WHERE indexrelid = to_regclass('idx_message_leads_search')") is True
        assert {row["name"] for row in await pool.fetch("SELECT name FROM schema_migrations")} >= {
            "leads_search", "leads_search_online"}

    run_db(test, monkeypatch, timeout=60)


def test_each_candidate_set_brings_its_newest_so_a_common_word_cannot_hide_a_rare_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        monkeypatch.setattr(message_leads, "CANDIDATES_PER_SET", 3)
        await _leads(pool, "Orion 的回滚由 Carol 负责", *[f"我们明天再说 {i}" for i in range(8)])
        await _mode(pool, "fulltext")
        assert (await _found(pool, "我们 Carol"))[0] == "Orion 的回滚由 Carol 负责"
        assert len(await _found(pool, "我们")) == 3  # the newest three a set brings

    run_db(test, monkeypatch, timeout=60)


@pytest.mark.parametrize(("candidate", "excluded_by"), [
    (candidate, excluded_by)
    for candidate in ("token", "literal", "fuzzy", "chat_name", "person_name")
    for excluded_by in ("expired", "kind")
] + [("token", excluded_by) for excluded_by in ("source", "since", "chat", "person")])
def test_filtered_leads_do_not_use_up_the_candidate_limit(
    monkeypatch: pytest.MonkeyPatch, candidate: str, excluded_by: str,
) -> None:
    async def test(pool: Any) -> None:
        text = "Orion deploy" if candidate in ("token", "literal", "fuzzy") else "Release decision"
        chat_name = "Orion room" if candidate == "chat_name" else "Release room"
        (wanted,) = await _leads(pool, text, chat_name=chat_name)
        person_name = "Orion Person" if candidate == "person_name" else "Alice Example"
        await pool.execute("UPDATE source_people SET name = $1 WHERE source_id = $2 AND open_id = 'ou_alice'",
                           person_name, stream.SOURCE)
        filters = {"kind": "decision"} if excluded_by == "kind" else {}
        if excluded_by == "source":
            filters = {"source_id": stream.SOURCE}
        elif excluded_by == "since":
            filters = {"since": NOW - timedelta(days=1)}
        elif excluded_by == "chat":
            filters = {"chat": "oc_release"}
        elif excluded_by == "person":
            filters = {"person": "ou_alice"}
        query = {"literal": "rion", "fuzzy": "deploying"}.get(candidate, "Orion")
        # Positive control: the requested route really finds this lead before the competing rows exist.
        await _mode(pool, "fulltext")
        assert await _found(pool, query, **filters) == [text]
        source = "other-stream" if excluded_by == "source" else stream.SOURCE
        await stream.source(pool, source)
        sender = "ou_other" if excluded_by == "person" else "ou_alice"
        await stream.person(pool, sender, person_name, source_id=source)
        seq = await stream.add(pool, text, chat_id="oc_other" if excluded_by == "chat" else "oc_release",
                               chat_name=chat_name, sender=sender, sent_at=NOW, source_id=source)
        newer = await pool.fetch(
            "INSERT INTO message_leads (source_id, kind, summary, confidence, valid_until, first_sent_at, last_sent_at) "
            "SELECT $1, $2, $3, 'high', $4, $5, $5 FROM generate_series(1, 2001) RETURNING id", source,
            "fact" if excluded_by == "kind" else "decision", text,
            NOW - timedelta(days=1) if excluded_by == "expired" else NOW + timedelta(days=1),
            NOW - timedelta(days=2) if excluded_by == "since" else NOW)
        ids = [row["id"] for row in newer]
        assert len(ids) == 2001 and min(ids) > wanted
        await pool.execute("INSERT INTO message_lead_sources (lead_id, event_seq) "
                           "SELECT id, $2 FROM unnest($1::bigint[]) id", ids, seq)
        await pool.execute("INSERT INTO message_lead_people (lead_id, open_id, role) "
                           "SELECT id, $2, 'sender' FROM unnest($1::bigint[]) id", ids, sender)
        # Exercise the common entry point used by the CLI, operator API and Manager tool in both modes.
        await _mode(pool, "words")
        assert await _found(pool, "deploy" if candidate == "fuzzy" else query, **filters) == [text]
        await _mode(pool, "fulltext")
        assert await _found(pool, query, **filters) == [text]

    run_db(test, monkeypatch, timeout=60)


@pytest.mark.parametrize("fulltext,fenced", [(False, False), (True, False), (False, True), (True, True)])
def test_eval_cli_preserves_fulltext_and_fence_switches(monkeypatch, tmp_path, fulltext, fenced):
    from evals.prompt_regression import __main__ as cli

    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {"dialogs": [], "aborted": None}, tmp_path / "result.json"

    monkeypatch.setattr(cli.dialogs, "run", run)
    monkeypatch.setattr(cli, "_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.dialogs, "markdown", lambda result: "")
    arguments = ["dialogs", "--mode", "fake", "--dialogs", "leads_english"]
    if fulltext:
        arguments.append("--leads-fulltext")
    if fenced:
        arguments.append("--untrusted-fence")
    assert cli.main(arguments) == 0
    assert len(calls) == 1
    assert calls[0]["leads_fulltext"] is fulltext
    assert calls[0]["untrusted_fence"] is fenced
