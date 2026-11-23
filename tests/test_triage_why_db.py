"""Section 7, in order, over synthetic messages. The service is not running; the CLI opens its own pool."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest

from looper import cli
from looper.agents import source_service, triage_settings
from looper.agents.triage_why import NOT_HANDED, Case, MessageNotFound, UnusableRef, format_lines, why
from looper.db.connection import ensure_schema
from tests import subscription_support as stream
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
NEXT = datetime(2026, 11, 23, 12, 5, tzinfo=timezone.utc)
TEXT = "synthetic-body-alpha"
TITLE = "synthetic-title-beta"
SUMMARY = "synthetic-summary-gamma"
REASON = "synthetic-reason-delta"
FLOOR = "操作者还没在这个会话里回复（代码保底）"
SOURCE = stream.SOURCE


def db_call(url, call):
    async def run():
        conn = await asyncpg.connect(url)
        try:
            return await call(conn)
        finally:
            await conn.close()
    return asyncio.run(run())


async def enable(conn, on=True):
    await conn.execute(
        "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        triage_settings.KEY, "true" if on else "false")


async def install(conn, *, schedule_id="triage-s", source_id=SOURCE, cursor=0, next_at=NEXT,
                  include=(), exclude=()):
    await stream.source(conn, source_id)
    await conn.execute("INSERT INTO projects (id, name) VALUES ('p', 'Synthetic') ON CONFLICT DO NOTHING")
    await conn.execute(
        "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, kind, enabled, next_fire_at) "
        "VALUES ($1, 'p', $1, '*/5 * * * *', 'UTC', 'Triage', 'subscription', $2, $3)",
        schedule_id, next_at is not None, next_at)
    await conn.execute(
        "INSERT INTO subscriptions (schedule_id, source_id, chat_kinds, sender_kinds, include_chats, exclude_chats, "
        "max_age_minutes, max_events, text_max_chars, output_kind, cursor_seq) "
        "VALUES ($1, $2, '{p2p,group,topic}', '{user,app}', $3, $4, 10080, 500, 200, 'triage', $5)",
        schedule_id, source_id, list(include), list(exclude), cursor)


async def message(conn, **columns):
    columns.setdefault("chat_id", "oc_dm_synthetic")
    columns.setdefault("kind", "p2p")
    return await stream.add(conn, TEXT, sent_at=NOW, **columns)


async def external_id(conn, seq):
    return await conn.fetchval("SELECT external_id FROM source_events WHERE seq = $1", seq)


async def cover(conn, seq, *, outcome="extracted", skip_reason=None, run_id="run-1", schedule_id="triage-s",
                window=None, to_seq=None):
    await conn.execute(
        "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status) "
        "VALUES ($1, $2, $3, 'manual', 'completed')", run_id, schedule_id, NOW)
    await conn.execute(
        "INSERT INTO subscription_runs (run_id, from_seq, to_seq, outcome, skip_reason, window_seqs) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        run_id, seq - 1, to_seq if to_seq is not None else seq, outcome, skip_reason,
        window if window is not None else [seq])


async def cite(conn, seq, *, category, reason_kind, reason=REASON, run_id="run-1", schedule_id="triage-s"):
    title, summary = ("", "") if category == "ignore" else (TITLE, SUMMARY)
    event_id = await conn.fetchval(
        "INSERT INTO triage_events (source_id, schedule_id, run_id, category, title, summary, reason, "
        "reason_kind, confidence, first_sent_at, last_sent_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'high', $9, $9) RETURNING id",
        SOURCE, schedule_id, run_id, category, title, summary, reason, reason_kind, NOW)
    await conn.execute("INSERT INTO triage_event_sources (event_id, event_seq) VALUES ($1, $2)", event_id, seq)
    return event_id


async def passed(conn, seq, schedule_id="triage-s"):
    await conn.execute("UPDATE subscriptions SET cursor_seq = GREATEST(cursor_seq, $2) WHERE schedule_id = $1",
                       schedule_id, seq)


class _Sql:
    """Records statements issued through one connection."""

    def __init__(self, conn):
        self.conn = conn
        self.sql = []

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def _call(self, name, sql, args):
        self.sql.append(sql)
        return await getattr(self.conn, name)(sql, *args)

    async def fetch(self, sql, *args):
        return await self._call("fetch", sql, args)

    async def fetchrow(self, sql, *args):
        return await self._call("fetchrow", sql, args)

    async def fetchval(self, sql, *args):
        return await self._call("fetchval", sql, args)

    async def execute(self, sql, *args):
        return await self._call("execute", sql, args)


def test_triage_off_reads_nothing_else(monkeypatch):
    async def check(pool):
        monkeypatch.setattr(source_service, "own_bot_app_ids", _boom)
        await stream.source(pool)
        seq = await message(pool)
        ext = await external_id(pool, seq)
        async with pool.acquire() as conn:
            recorded = _Sql(conn)
            for ref in (ext, str(seq), "om_not_stored"):
                answer = await why(recorded, ref, now=NOW)
                assert answer.case is Case.TRIAGE_OFF and answer.cause == "disabled"
                assert answer.as_dict() == {"case": "triage_off", "cause": "disabled"}
        assert recorded.sql and all("system_settings" in sql for sql in recorded.sql)
        assert not any("source_events" in sql or "subscriptions" in sql for sql in recorded.sql)

    run_db(check, monkeypatch)


async def _boom():
    raise AssertionError("the switch is off; nothing else is read")


def test_no_subscription_is_off_without_resolving_the_message(monkeypatch):
    async def check(pool):
        await enable(pool)
        async with pool.acquire() as conn:
            recorded = _Sql(conn)
            answer = await why(recorded, "999999", now=NOW)
        assert answer.case is Case.TRIAGE_OFF and answer.cause == "no_subscription"
        assert format_lines(answer) == ["分流没开", "原因：没有分流订阅"]
        assert not any("source_events" in sql for sql in recorded.sql)

    run_db(check, monkeypatch)


def test_out_of_scope_names_the_rule_and_beats_not_yet(monkeypatch):
    async def check(pool):
        await enable(pool)
        await install(pool, cursor=0)
        await pool.execute(
            "INSERT INTO channel_configs (channel_id, channel_type, config) "
            "VALUES ('synthetic-bot', 'lark_bot', '{\"app_id\":\"cli_synthetic\"}'::jsonb)")
        await pool.execute("INSERT INTO intent_alert_senders (source_id, app_id) VALUES ($1, 'cli_synthetic')", SOURCE)
        excluded = await message(pool, chat_id="oc_excluded")
        await pool.execute(
            "UPDATE source_chats SET inclusion = 'excluded', exclusion_reason = 'operator' "
            "WHERE source_id = $1 AND chat_id = 'oc_excluded'", SOURCE)
        imported = await message(pool, chat_id="oc_imported", origin="import")
        own = await message(pool, chat_id="oc_own", sender="cli_synthetic", sender_kind="app")
        group = await message(pool, chat_id="oc_group_synthetic", kind="group", sender="ou_other")
        expected = {excluded: "chat_excluded", imported: "imported_history", own: "own_app", group: "sender_kind"}
        for seq, rule in expected.items():
            answer = await why(pool, str(seq), now=NOW)
            assert answer.case is Case.OUT_OF_SCOPE and answer.rule == rule, rule
            assert answer.as_dict() == {"case": "out_of_scope", "rule": rule}
            assert format_lines(answer)[0] == "不在分流范围"
            assert "cursor_seq" not in answer.as_dict()

    run_db(check, monkeypatch)


def test_not_yet_reports_the_cursor_and_the_next_run(monkeypatch):
    async def check(pool):
        await enable(pool)
        await install(pool, cursor=0, next_at=NEXT)
        seq = await message(pool)
        answer = await why(pool, str(seq), now=NOW)
        assert answer.case is Case.NOT_YET and answer.cursor_seq == 0 and answer.next_run_at == NEXT
        assert format_lines(answer) == ["还没轮到这条消息", "游标：0", "下次运行：2026-11-23T12:05:00+00:00"]
        await pool.execute("UPDATE schedules SET next_fire_at = NULL WHERE id = 'triage-s'")
        later = await message(pool, chat_id="oc_dm_later")
        quiet = await why(pool, str(later), now=NOW)
        assert quiet.case is Case.NOT_YET and quiet.next_run_at is None
        assert format_lines(quiet)[-1] == "下次运行：没有下次运行"

    run_db(check, monkeypatch)


def test_a_skipped_window_is_skipped_even_without_or_with_an_event(monkeypatch):
    async def check(pool):
        await enable(pool)
        await install(pool)
        bare = await message(pool)
        await passed(pool, bare)
        await cover(pool, bare, outcome="skipped", skip_reason="invalid_reply", run_id="run-invalid")
        answer = await why(pool, str(bare), now=NOW)
        assert answer.case is Case.SKIPPED and answer.skip_reason == "invalid_reply" and answer.run_id == "run-invalid"
        assert format_lines(answer) == ["这一段被跳过", "跳过原因：invalid_reply", "运行：run-invalid"]
        cited = await message(pool, chat_id="oc_dm_cited")
        await passed(pool, cited)
        await cover(pool, cited, outcome="skipped", skip_reason="blocked_by_hook", run_id="run-hook")
        await cite(pool, cited, category="important", reason_kind="model", run_id="run-hook")
        still = await why(pool, str(cited), now=NOW)
        assert still.case is Case.SKIPPED and still.skip_reason == "blocked_by_hook"
        assert still.event_id is None

    run_db(check, monkeypatch)


def test_an_event_cites_the_message_including_a_raised_one(monkeypatch, caplog):
    async def check(pool):
        await enable(pool)
        await install(pool)
        raised_seq = await message(pool, chat_id="oc_raised")
        await passed(pool, raised_seq)
        await cover(pool, raised_seq, run_id="run-raised")
        raised_id = await cite(pool, raised_seq, category="digest", reason_kind="raised", reason=FLOOR,
                               run_id="run-raised")
        important_seq = await message(pool, chat_id="oc_important")
        await passed(pool, important_seq)
        await cover(pool, important_seq, run_id="run-important")
        await cite(pool, important_seq, category="digest", reason_kind="model", reason="synthetic-digest-loses",
                   run_id="run-important")
        important_id = await cite(pool, important_seq, category="important", reason_kind="model",
                                  run_id="run-important")
        old_seq = await message(pool, chat_id="oc_old_uncited")
        await passed(pool, old_seq)
        await cover(pool, old_seq, run_id="run-old")
        old_id = await cite(pool, old_seq, category="ignore", reason_kind="uncited", reason="synthetic-old-uncited",
                            run_id="run-old")
        caplog.set_level(logging.INFO, logger="looper.agents.triage_why")
        raised = await why(pool, str(raised_seq), now=NOW)
        assert raised.case is Case.EVENT and raised.reason_kind == "raised" and raised.category == "digest"
        assert raised.event_id == f"E-{raised_id}" and raised.reason == FLOOR and raised.handling is None
        assert format_lines(raised)[2:] == [f"理由：{FLOOR}", "理由来源：代码调高"]
        important = await why(pool, await external_id(pool, important_seq), now=NOW)
        assert important.case is Case.EVENT and important.category == "important"
        assert important.event_id == f"E-{important_id}" and important.reason == REASON
        assert important.reason_kind == "model" and important.handling == NOT_HANDED
        assert format_lines(important)[-1] == NOT_HANDED
        old = await why(pool, str(old_seq), now=NOW)
        assert old.case is Case.EVENT and old.reason_kind == "uncited" and old.event_id == f"E-{old_id}"
        logged = "\n".join(record.getMessage() for record in caplog.records
                           if record.name == "looper.agents.triage_why")
        assert "case=event" in logged and f"event_id=E-{important_id}" in logged
        for secret in (TEXT, TITLE, SUMMARY, REASON, FLOOR, "synthetic-digest-loses", "synthetic-old-uncited"):
            assert secret not in logged

    run_db(check, monkeypatch, timeout=30)


def test_uncited_comes_from_the_window_not_the_seq_range(monkeypatch):
    async def check(pool):
        await enable(pool)
        await install(pool)
        chosen = await message(pool, chat_id="oc_chosen")
        neighbor = await message(pool, chat_id="oc_neighbor")
        await passed(pool, neighbor)
        await cover(pool, chosen, run_id="run-open", window=[chosen], to_seq=neighbor)
        await cover(pool, chosen, outcome="skipped", skip_reason="blocked_by_hook", run_id="run-also-skipped",
                    window=[chosen])
        answer = await why(pool, str(chosen), now=NOW)
        assert answer.case is Case.UNCITED and answer.run_id == "run-open"
        assert format_lines(answer) == ["忽略", "未引用：处理器没有提到这条消息", "运行：run-open"]
        missed = await why(pool, str(neighbor), now=NOW)
        assert missed.case is Case.UNKNOWN and "in_scope" in missed.checked
        assert any(item.startswith("covering_runs=0") for item in missed.checked)

    run_db(check, monkeypatch)


def test_unknown_keeps_the_facts_and_does_not_guess(monkeypatch):
    async def check(pool):
        await enable(pool)
        await install(pool, schedule_id="triage-a", exclude=("oc_mixed",))
        await install(pool, schedule_id="triage-b")
        await stream.source(pool, "other-source")
        elsewhere = await message(pool, source_id="other-source", chat_id="oc_elsewhere")
        other = await why(pool, str(elsewhere), now=NOW)
        assert other.case is Case.UNKNOWN and other.checked == ("no_triage_subscription_for_source",)
        mixed = await message(pool, chat_id="oc_mixed", origin="import")
        disagreed = await why(pool, str(mixed), now=NOW)
        assert disagreed.case is Case.UNKNOWN
        assert disagreed.checked == ("rule:chat_excluded", "rule:imported_history")
        left = await message(pool, chat_id="oc_left_behind")
        await passed(pool, left, "triage-a")
        await passed(pool, left, "triage-b")
        behind = await why(pool, str(left), now=NOW)
        assert behind.case is Case.UNKNOWN and "in_scope" in behind.checked
        assert format_lines(behind)[0] == "无法判断"

    run_db(check, monkeypatch)


def test_id_link_and_seq_resolve_and_garbage_does_not(monkeypatch):
    async def check(pool):
        await enable(pool)
        await install(pool)
        seq = await message(pool)
        await passed(pool, seq)
        await cover(pool, seq, run_id="run-ref")
        ext = await external_id(pool, seq)
        link = f"https://example.invalid/open?openMessageId={ext}"
        path = f"https://example.invalid/thread/{ext}"
        answers = [await why(pool, ref, now=NOW) for ref in (ext, link, path, str(seq))]
        assert {item.case for item in answers} == {Case.UNCITED}
        assert {item.run_id for item in answers} == {"run-ref"}
        with pytest.raises(UnusableRef):
            await why(pool, "https://applink.feishu.cn/client/chat/open?openChatId=oc_synthetic", now=NOW)
        with pytest.raises(MessageNotFound):
            await why(pool, "om_not_stored_synthetic", now=NOW)
        await stream.source(pool, "other-source")
        other = await message(pool, source_id="other-source", chat_id="oc_copy")
        await pool.execute("UPDATE source_events SET external_id = $2 WHERE seq = $1", other, ext)
        from looper.agents.triage_why import AmbiguousMessage
        with pytest.raises(AmbiguousMessage):
            await why(pool, ext, now=NOW)
        assert (await why(pool, str(seq), now=NOW)).case is Case.UNCITED

    run_db(check, monkeypatch)


def test_a_client_token_is_forbidden_and_the_operator_gets_the_case(monkeypatch, tmp_path):
    async def check(pool):
        await enable(pool)
        await install(pool)
        seq = await message(pool)
        await passed(pool, seq)
        await cover(pool, seq, run_id="run-api")
        event_id = await cite(pool, seq, category="important", reason_kind="model", run_id="run-api")
        ext = await external_id(pool, seq)
        body = TEXT + " leftover"
        async with v1_api(tmp_path, pool) as call:
            status, denied = await call("triage.why", {"message": ext}, client=True)
            assert status == 403 and denied["error"]["code"] == "forbidden"
            assert TEXT not in json.dumps(denied)
            status, blank = await call("triage.why", {"message": "  "})
            assert status == 400 and blank["error"]["code"] == "invalid" and TEXT not in json.dumps(blank)
            status, garbage = await call("triage.why", {"message": body})
            assert status == 400 and garbage["error"]["code"] == "invalid" and body not in json.dumps(garbage)
            status, missing = await call("triage.why", {"message": "om_missing_synthetic"})
            assert status == 404 and missing["error"]["code"] == "not_found"
            status, found = await call("triage.why", {"message": ext})
            assert status == 200 and found["result"]["event_id"] == f"E-{event_id}"
            assert found["result"]["handling"] == NOT_HANDED and TEXT not in json.dumps(found)
            await stream.source(pool, "other-source")
            other = await message(pool, source_id="other-source", chat_id="oc_api_copy")
            await pool.execute("UPDATE source_events SET external_id = $2 WHERE seq = $1", other, ext)
            status, clash = await call("triage.why", {"message": ext})
            assert status == 409 and clash["error"]["code"] == "conflict"

    run_db(check, monkeypatch, timeout=40)


@pytest.fixture
def database(monkeypatch):
    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    schema = f"why_cli_{uuid.uuid4().hex}"
    db_call(DSN, lambda conn: conn.execute(f'CREATE SCHEMA "{schema}"'))
    parts = urlsplit(DSN)
    url = urlunsplit(parts._replace(query=urlencode([*parse_qsl(parts.query), ("search_path", schema)])))

    async def setup(conn):
        await ensure_schema(conn)
    db_call(url, setup)
    try:
        yield url
    finally:
        db_call(DSN, lambda conn: conn.execute(f'DROP SCHEMA "{schema}" CASCADE'))


def run_cli(tmp_path, url, *argv):
    config = tmp_path / "server.toml"
    config.write_text(f'[database]\nurl = "{url}"\n')
    config.chmod(0o600)
    return cli.main(["msgflow", "why", *argv, "--config", str(config), "--runtime-root", str(tmp_path / "runtime")])


def test_the_cli_answers_with_no_server_and_uses_the_leads_exit_codes(tmp_path, database, capsys):
    """No looper serve is started: the command reads the database the way delete-message does."""
    async def seed(conn):
        await enable(conn)
        await install(conn)
        seq = await message(conn)
        await passed(conn, seq)
        await cover(conn, seq, run_id="run-cli")
        event_id = await cite(conn, seq, category="important", reason_kind="model", run_id="run-cli")
        return seq, await external_id(conn, seq), event_id

    seq, ext, event_id = db_call(database, seed)
    assert run_cli(tmp_path, database, ext) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"事件：E-{event_id}" and lines[-1] == NOT_HANDED and TEXT not in "\n".join(lines)
    assert run_cli(tmp_path, database, "--json", str(seq)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["case"] == "event" and payload["event_id"] == f"E-{event_id}" and "seq" not in payload
    link = f"https://example.invalid/open?message_id={ext}"
    assert run_cli(tmp_path, database, link) == 0
    assert f"E-{event_id}" in capsys.readouterr().out
    assert run_cli(tmp_path, database, "999999") == 5
    assert capsys.readouterr().err.strip() == "没有这条消息"
    garbage = "not a stored message"
    assert run_cli(tmp_path, database, garbage) == 2
    err = capsys.readouterr().err
    assert "消息引用无法识别" in err and garbage not in err
    assert run_cli(tmp_path, database, "https://applink.feishu.cn/client/chat/open?openChatId=oc_synthetic") == 2
    await_second = db_call(database, lambda conn: _copy_external_id(conn, ext))
    assert await_second
    assert run_cli(tmp_path, database, ext) == 2
    assert "seq" in capsys.readouterr().err and ext not in capsys.readouterr().err
    assert run_cli(tmp_path, database, str(seq)) == 0
    capsys.readouterr()
    db_call(database, lambda conn: enable(conn, False))
    assert run_cli(tmp_path, database, str(seq)) == 3
    assert capsys.readouterr().out.splitlines() == ["分流没开", "原因：开关关闭"]
    assert run_cli(tmp_path, database, garbage) == 2


async def _copy_external_id(conn, ext):
    await stream.source(conn, "other-source")
    other = await message(conn, source_id="other-source", chat_id="oc_cli_copy")
    await conn.execute("UPDATE source_events SET external_id = $2 WHERE seq = $1", other, ext)
    return True


def test_a_missing_database_is_exit_6(tmp_path, capsys):
    parts = urlsplit(DSN)
    missing = urlunsplit(parts._replace(path="/triage_why_no_such_database"))
    assert run_cli(tmp_path, missing, "1") == 6
    assert "database" in capsys.readouterr().err.lower()
