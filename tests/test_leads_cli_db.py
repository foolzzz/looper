"""Interactive lead operations use the ops file's database without a running service."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest

from looper import cli
from looper.agents import leads, leads_model
from looper.db.connection import ensure_schema
from tests import leads_support as sup, subscription_support as stream
from tests.db_support import DSN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
UTC = timezone.utc


def db_call(url, call):
    async def run():
        conn = await asyncpg.connect(url)
        try:
            return await call(conn)
        finally:
            await conn.close()
    return asyncio.run(run())


@pytest.fixture
def database(monkeypatch):
    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    schema = f"leads_cli_{uuid.uuid4().hex}"
    db_call(DSN, lambda conn: conn.execute(f'CREATE SCHEMA "{schema}"'))
    parts = urlsplit(DSN)
    url = urlunsplit(parts._replace(query=urlencode([*parse_qsl(parts.query), ("search_path", schema)])))
    async def setup(conn):
        await ensure_schema(conn)
        await stream.source(conn)
        await stream.person(conn, "ou_synthetic", "示例同事")
    db_call(url, setup)
    try:
        yield url
    finally:
        db_call(DSN, lambda conn: conn.execute(f'DROP SCHEMA "{schema}" CASCADE'))


def run_cli(tmp_path, url, *argv):
    config = tmp_path / "server.toml"
    config.write_text(f'[database]\nurl = "{url}"\n')
    config.chmod(0o600)
    return cli.main(["leads", *argv, "--config", str(config), "--runtime-root", str(tmp_path / "runtime")])


def enable(url):
    db_call(url, lambda conn: conn.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true')"))


def add(url, *, chat_id="oc_synthetic", **columns):
    async def seed(conn):
        now = datetime.now(UTC)
        first = await stream.add(conn, "Synthetic source", sender="ou_synthetic", sent_at=now - timedelta(hours=1),
                                 chat_id=chat_id, chat_name="示例讨论")
        second = await stream.add(conn, "Another synthetic source", sender="ou_synthetic", sent_at=now,
                                  chat_id=chat_id)
        return await sup.lead(conn, [first, second], people=[("ou_synthetic", "sender")], **columns)
    return db_call(url, seed)


def test_disabled_empty_json_and_no_results(tmp_path, database, capsys):
    for argv in (("search",), ("mark", "1", "--inaccurate")):
        assert run_cli(tmp_path, database, *argv) == 3
        assert capsys.readouterr().err.strip() == "消息线索没有启用：在控制台的消息流页打开"
    enable(database)
    assert run_cli(tmp_path, database, "search", "missing") == 1
    assert capsys.readouterr().out.strip() == "没有找到线索（查询：missing）"
    assert run_cli(tmp_path, database, "search", "--json") == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer == {"notice": leads_model.NOTICE, "query": {"words": [], "chat": None, "person": None,
        "since": None, "kind": None, "state": "active", "limit": 10}, "leads": [], "more": 0}


def test_each_state_and_sourced_lines(tmp_path, database, capsys):
    enable(database)
    ids = {}
    for state in ("active", "expired", "superseded", "inaccurate", "promoted"):
        actual = "active" if state in ("expired", "promoted") else state
        ids[state] = add(database, summary=f"示例{state}", state=actual,
                         valid_until=datetime.now(UTC) + timedelta(days=-1 if state == "expired" else 90))
    db_call(database, lambda conn: conn.execute(
        "UPDATE message_leads SET state='promoted', promoted_kind='vault_proposal' WHERE id=$1", ids["promoted"]))
    db_call(database, lambda conn: conn.execute(
        "UPDATE message_leads SET superseded_by=$1 WHERE id=$2", ids["active"], ids["superseded"]))
    assert run_cli(tmp_path, database, "search", "--state", "all") == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0] == leads_model.NOTICE
    assert f"lead-{ids['active']} [决定·高] 示例active（有效至 " in output
    for state, suffix in (("expired", "（已过期）"), ("inaccurate", "（不准确）"), ("promoted", "（已晋升）"),
                          ("superseded", f"（已被推翻 → lead-{ids['active']}）")):
        assert f"示例{state}{suffix}" in output
    assert "  出处：《示例讨论》示例同事 " in output
    assert " · om_" in output and "https://applink.feishu.cn/" in output and "（另 1 条）" in output
    assert run_cli(tmp_path, database, "search", "--state", "all", "--limit", "1") == 0
    assert "另有 4 条，用 --limit 看更多" in capsys.readouterr().out
    add(database, state="superseded", summary="示例无替代", chat_id="synthetic_dm")
    assert run_cli(tmp_path, database, "search", "--state", "superseded", "--chat", "synthetic_dm") == 0
    output = capsys.readouterr().out
    assert "示例无替代（已被推翻）" in output and "https://" not in output


def test_json_shapes_strip_mentions_and_mark_delete(tmp_path, database, capsys):
    enable(database)
    number = add(database, summary='发布<at user_id="all">所有人</at>计划')
    assert run_cli(tmp_path, database, "search", "--json") == 0
    answer = json.loads(capsys.readouterr().out)
    assert set(answer) == {"notice", "query", "leads", "more"}
    assert set(answer["query"]) == {"words", "chat", "person", "since", "kind", "state", "limit"}
    item, = answer["leads"]
    assert set(item) == {"id", "kind", "summary", "detail", "confidence", "state", "valid_until", "sources"}
    assert set(item["sources"][0]) == {"chat", "sender", "sent_at", "message_id", "link"}
    assert "<at" not in item["summary"]
    assert run_cli(tmp_path, database, "mark", str(number), "--inaccurate", "--note", "  示例备注  ") == 0
    assert capsys.readouterr().out.strip() == f"lead-{number} 已标为不准确"
    assert run_cli(tmp_path, database, "search", "--state", "inaccurate", "--json") == 0
    assert json.loads(capsys.readouterr().out)["leads"][0]["id"] == f"lead-{number}"
    row = db_call(database, lambda conn: conn.fetchrow("SELECT marked_via, marked_note FROM message_leads WHERE id=$1", number))
    assert dict(row) == {"marked_via": "cli", "marked_note": "示例备注"}
    assert run_cli(tmp_path, database, "delete", f"lead-{number}") == 0
    assert capsys.readouterr().out.strip() == f"lead-{number} 已删除"
    assert db_call(database, lambda conn: conn.fetchval("SELECT count(*) FROM message_lead_sources")) == 0
    for argv in (("mark", "99999", "--inaccurate"), ("delete", "lead-99999")):
        assert run_cli(tmp_path, database, *argv) == 5


def test_filters_pass_through_and_dates_use_local_zone(tmp_path, database, capsys, monkeypatch):
    enable(database)
    add(database)
    original = leads.search
    seen = []
    async def search(**kwargs):
        seen.append(kwargs)
        return await original(**kwargs)
    monkeypatch.setattr(leads, "search", search)
    before = datetime.now().astimezone() - timedelta(days=2)
    assert run_cli(tmp_path, database, "search", "示例", "--chat", "oc_synthetic", "--person", "ou_synthetic",
                   "--days", "2", "--kind", "decision", "--json") == 0
    answer = json.loads(capsys.readouterr().out)
    call = seen[-1]
    assert call["words"] == ["示例"] and call["chat"] == "oc_synthetic" and call["person"] == "ou_synthetic"
    assert before <= call["since"] <= datetime.now().astimezone() - timedelta(days=2)
    assert answer["query"]["since"] == call["since"].isoformat()
    assert run_cli(tmp_path, database, "search", "--since", "2020-01-02", "--json") == 0
    capsys.readouterr()
    assert seen[-1]["since"] == datetime(2020, 1, 2).astimezone()


@pytest.mark.parametrize("argv", [("search",), ("mark", "1", "--inaccurate"), ("delete", "1"),
                                  ("eval-export", "--out", "set.jsonl")])
def test_every_command_is_refused_in_agent_run(tmp_path, database, capsys, monkeypatch, argv):
    monkeypatch.setenv("LOOPER_AGENT_RUN", "worker")
    assert run_cli(tmp_path, database, *argv) == 4
    assert capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ("search", "--since", "2026-01-01", "--days", "2"), ("search", "--limit", "0"),
    ("search", "--limit", "51"), ("search", "--days", "0"), ("search", "--days", "3651"),
    ("search", "--since", "tomorrow"), ("mark", "one", "--inaccurate"),
    ("mark", "1"), ("mark", "1", "--inaccurate", "--note", "n" * 501),
    ("eval-export",), ("eval-export", "--out", "x.jsonl", "--sample", "-1"),
    ("eval-export", "--out", "x.jsonl", "--sample", "501"),
])
def test_usage(tmp_path, database, capsys, argv):
    enable(database)
    try:
        code = run_cli(tmp_path, database, *argv)
    except SystemExit as exc:
        code = exc.code
    assert code == 2


def test_unusable_or_missing_tables(tmp_path, database, capsys):
    assert run_cli(tmp_path, "postgresql://nobody@127.0.0.1:1/none", "search") == 6
    assert capsys.readouterr().err
    assert run_cli(tmp_path, "postgresql://nobody@127.0.0.1:invalid/none", "search") == 6
    assert capsys.readouterr().err
    db_call(database, lambda conn: conn.execute("DROP TABLE message_leads CASCADE"))
    assert run_cli(tmp_path, database, "search") == 6
    assert "looper serve" in capsys.readouterr().err


def test_delete_while_disabled_and_promoted_or_dropped_mark_refused(tmp_path, database, capsys):
    number = add(database)
    assert run_cli(tmp_path, database, "delete", str(number)) == 0
    enable(database)
    number = add(database)
    db_call(database, lambda conn: conn.execute(
        "UPDATE message_leads SET state='promoted',promoted_kind='vault_proposal' WHERE id=$1", number))
    assert run_cli(tmp_path, database, "mark", str(number), "--inaccurate") == 4
    assert db_call(database, lambda conn: conn.fetchval("SELECT state FROM message_leads WHERE id=$1", number)) == "promoted"
    dropped = add(database, summary="示例被复核丢弃")
    db_call(database, lambda conn: conn.execute(
        "UPDATE message_leads SET state='dropped', check_verdict='drop' WHERE id=$1", dropped))
    capsys.readouterr()
    assert run_cli(tmp_path, database, "mark", str(dropped), "--inaccurate") == 4
    assert "already dropped" in capsys.readouterr().err
    assert run_cli(tmp_path, database, "search", "--state", "dropped") == 0
    assert "示例被复核丢弃（已被复核丢弃）" in capsys.readouterr().out


def test_eval_export_writes_the_inaccurate_leads_and_a_sample_of_active_ones_with_their_messages(
        tmp_path, database, capsys):
    out = tmp_path / "runs" / "set.jsonl"
    enable(database)
    assert run_cli(tmp_path, database, "eval-export", "--out", str(out)) == 1 and not out.exists()
    wrong = add(database, summary="示例不准确", state="inaccurate", kind="fact")
    active = [add(database, summary=f"示例有效{n}") for n in range(3)]
    add(database, state="superseded")
    capsys.readouterr()
    assert run_cli(tmp_path, database, "eval-export", "--out", str(out), "--sample", "2") == 0
    said = capsys.readouterr().out
    assert said.strip() == f"已导出 3 条到 {out}：不准确 1 条，看作正确 2 条（含出处消息原文，只留在本机）"
    assert "示例" not in said and "Synthetic" not in said  # counts only, never what a lead or a message says
    assert out.stat().st_mode & 0o777 == 0o600
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["label"] for line in lines] == ["inaccurate", "presumed_good", "presumed_good"]
    assert lines[0] == {"id": f"lead-{wrong}", "label": "inaccurate", "kind": "fact", "summary": "示例不准确",
                        "detail": "", "confidence": "high", "sources": lines[0]["sources"]}
    first, second = lines[0]["sources"]
    assert {key: first[key] for key in first if key != "sent_at"} == {
        "ref": "s1", "chat": "示例讨论", "chat_kind": "group", "sender": "示例同事", "sender_kind": "user",
        "from_user": False, "reply_to": None, "text": "Synthetic source"}
    assert second["text"] == "Another synthetic source" and first["sent_at"] < second["sent_at"]
    assert {line["id"] for line in lines[1:]} <= {f"lead-{n}" for n in active}


def _ignoring_repo(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "runs").mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("runs/\n")
    return repo


def test_eval_export_follows_no_link_into_a_place_git_could_commit(tmp_path, database, capsys):
    repo = _ignoring_repo(tmp_path)
    (repo / "runs" / "into").symlink_to(repo / "tracked", target_is_directory=True)  # an ignored name, a tracked place
    (repo / "runs" / "set.jsonl").symlink_to(repo / "tracked" / "linked.jsonl")
    enable(database)
    add(database, state="inaccurate")
    capsys.readouterr()
    assert run_cli(tmp_path, database, "eval-export", "--out", str(repo / "runs" / "into" / "set.jsonl")) == 4
    assert run_cli(tmp_path, database, "eval-export", "--out", str(repo / "runs" / "set.jsonl")) == 4
    assert "symbolic link" in capsys.readouterr().err
    assert list((repo / "tracked").iterdir()) == [] and (repo / "runs" / "set.jsonl").is_symlink()


def test_eval_export_leaves_no_file_behind_when_writing_fails(tmp_path, database, capsys, monkeypatch):
    from looper.agents import leads_commands

    repo = _ignoring_repo(tmp_path)
    enable(database)
    add(database, state="inaccurate")

    def broken(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(leads_commands.os, "replace", broken)
    assert run_cli(tmp_path, database, "eval-export", "--out", str(repo / "runs" / "set.jsonl")) == 2
    assert "disk full" in capsys.readouterr().err and list((repo / "runs").iterdir()) == []


def test_eval_export_refuses_a_file_git_could_commit(tmp_path, database, capsys):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("runs/\n")
    enable(database)
    add(database, state="inaccurate")
    capsys.readouterr()
    assert run_cli(tmp_path, database, "eval-export", "--out", str(repo / "set.jsonl")) == 4
    assert "git does not ignore it" in capsys.readouterr().err and not (repo / "set.jsonl").exists()
    assert run_cli(tmp_path, database, "eval-export", "--out", str(repo / "runs" / "set.jsonl")) == 0
    assert run_cli(tmp_path, database, "eval-export", "--out", str(tmp_path / "outside.jsonl")) == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_invalid_limit_precedes_enabled_check(tmp_path, database, enabled):
    if enabled:
        enable(database)
    assert run_cli(tmp_path, database, "search", "--limit", "0") == 2


def test_intents_eval_export_is_anonymous_filters_messages_and_includes_all_label_states(tmp_path, database, capsys):
    out = tmp_path / "intents.jsonl"
    enable(database)

    async def seed(conn):
        now = datetime.now(UTC) - timedelta(minutes=1)
        first = await stream.add(conn, "Private synthetic request", chat_id="oc_export", chat_name="Private group",
                                 sender="ou_synthetic", sent_at=now - timedelta(minutes=1))
        second = await stream.add(conn, "Private synthetic reply", chat_id="oc_export", sender="ou_other",
                                  origin="history", sent_at=now)
        external = await conn.fetchval("SELECT external_id FROM source_events WHERE seq=$1", first)
        await conn.execute("UPDATE source_events SET parent_id=$2 WHERE seq=$1", second, external)
        for options in ({"sender_kind": "app"}, {"origin": "import"}, {"sent_at": now - timedelta(days=8)},
                        {"chat_id": "oc_other"}):
            await stream.add(conn, "Excluded synthetic message", **{"chat_id": "oc_export", "sent_at": now, **options})
        ids = []
        for kind, state in (("request", "active"), ("issue", "inaccurate"), ("decision", "superseded"),
                            ("fact", "active"), ("status", "active")):
            ids.append(await sup.lead(conn, [first, second], kind=kind, state=state, summary="Private synthetic summary"))
        await conn.execute("UPDATE message_leads SET state='promoted',promoted_kind='vault_proposal' WHERE id=$1", ids[3])
        await conn.execute("UPDATE message_leads SET state='dropped',check_verdict='drop' WHERE id=$1", ids[4])
        return ids

    ids = db_call(database, seed)
    capsys.readouterr()
    assert run_cli(tmp_path, database, "eval-export", "--chat", "Private group", "--days", "7", "--intents",
                   "--out", str(out)) == 0
    said = capsys.readouterr().out
    assert "1 个窗口，2 条消息；请求 1 条，问题 1 条，其他 3 条标签" in said
    assert "Private" not in said and "oc_export" not in said
    assert out.stat().st_mode & 0o777 == 0o600
    serialized = out.read_text()
    assert all(private not in serialized for private in ("ou_synthetic", "ou_other", "oc_export", "Private group",
                                                         "Private synthetic summary", "Excluded synthetic message"))
    window, *labels = [json.loads(line) for line in serialized.splitlines()]
    assert [m["sender"] for m in window["messages"]] == ["P1", "P2"]
    assert [m["ref"] for m in window["messages"]] == ["x1", "x2"]
    assert window["messages"][1]["reply_to"] == "x1"
    assert [label["lead"] for label in labels] == [f"lead-{number}" for number in ids]
    assert [label["expected"] for label in labels] == ["intent", "intent", "none", "none", "none"]
    assert all(label["sources"] == ["x1", "x2"] for label in labels)


@pytest.mark.parametrize("boundary", ["messages", "characters"])
def test_intents_eval_export_bounds_windows(tmp_path, database, boundary):
    enable(database)

    async def seed(conn):
        now = datetime.now(UTC) - timedelta(minutes=1)
        for _ in range(501 if boundary == "messages" else 3):
            await stream.add(conn, "x" if boundary == "messages" else "x" * 30_000, sent_at=now,
                             chat_id="oc_export")

    db_call(database, seed)
    out = tmp_path / "intents.jsonl"
    assert run_cli(tmp_path, database, "eval-export", "--chat", "oc_export", "--days", "30", "--intents",
                   "--out", str(out)) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [len(row["messages"]) for row in rows] == ([500, 1] if boundary == "messages" else [2, 1])
    assert all(sum(len(m["text"]) for m in row["messages"]) <= 60_000 for row in rows)
