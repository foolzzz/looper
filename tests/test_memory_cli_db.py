"""``looper memory list / show / check`` read the ops file's database directly, with no service running (memory-write-path
RFC, section 9.2); inside an agent run they are refused."""

import asyncio
import json
import subprocess
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest

from looper import cli
from looper.db.connection import ensure_schema
from tests import memory_support as sup
from tests.db_support import DSN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


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
    schema = f"memory_cli_{uuid.uuid4().hex}"
    db_call(DSN, lambda conn: conn.execute(f'CREATE SCHEMA "{schema}"'))
    parts = urlsplit(DSN)
    url = urlunsplit(parts._replace(query=urlencode([*parse_qsl(parts.query), ("search_path", schema)])))
    db_call(url, ensure_schema)
    try:
        yield url
    finally:
        db_call(DSN, lambda conn: conn.execute(f'DROP SCHEMA "{schema}" CASCADE'))


def run_cli(tmp_path, url, *argv):
    config = tmp_path / "server.toml"
    config.write_text(f'[database]\nurl = "{url}"\n')
    config.chmod(0o600)
    return cli.main(["memory", *argv, "--config", str(config), "--runtime-root", str(tmp_path / "runtime")])


def seed(url):
    async def make(conn):
        first = await sup.candidate(conn, statement="Weekly reports: conclusion first")
        await sup.revise(conn, first)
        second = await sup.candidate(conn, kind="decision", statement="Ship on Friday")
        await sup.move(conn, second, "dropped", "drop", reason="duplicate")
        return first, second
    return db_call(url, make)


def test_list_shows_the_open_candidates_and_json_the_view(database, tmp_path, capsys):
    assert run_cli(tmp_path, database, "list") == 1
    assert "没有记忆候选" in capsys.readouterr().out
    first, second = seed(database)
    assert run_cli(tmp_path, database, "list") == 0
    out = capsys.readouterr().out
    assert f"M-{first:04d} [偏好] Weekly reports: conclusion first → mem/prefs/weekly.md（待确认，" in out
    assert f"M-{second:04d}" not in out  # dropped is not open
    assert run_cli(tmp_path, database, "list", "--state", "all", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in listed["candidates"]] == [f"M-{second:04d}", f"M-{first:04d}"]
    assert listed["candidates"][0]["reason"] == "duplicate" and not listed["more"]
    assert run_cli(tmp_path, database, "list", "--kind", "decision", "--state", "dropped") == 0
    assert f"M-{second:04d} [决定]" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [("list", "--state", "lost"), ("list", "--kind", "wish"), ("list", "--limit", "0"),
                                  ("show", "42"), ("show", "L-0042")])
def test_a_bad_argument_is_a_usage_error(database, tmp_path, argv):
    assert run_cli(tmp_path, database, *argv) == 2


def test_show_gives_the_statement_sources_revision_and_events(database, tmp_path, capsys):
    first, _ = seed(database)
    assert run_cli(tmp_path, database, "show", f"m-{first}") == 0
    out = capsys.readouterr().out
    for text in ("陈述：Weekly reports: conclusion first", "理由：easier to skim", "出处：Manager 对话 lark",
                 "修订 1（draft）：Weekly reports: conclusion first", "create_note mem/prefs/weekly.md +4 -0",
                 "append_line mem/prefs/_index.md", "propose · manager · lark", "draft · system · system"):
        assert text in out, text
    assert run_cli(tmp_path, database, "show", f"M-{first:04d}", "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["current"]["changes"][0] == sup.NOTE and shown["sources"][0]["quote"] == (
        "from now on, conclusion first")
    assert run_cli(tmp_path, database, "show", "M-999999") == 5
    assert "没有这条记忆候选" in capsys.readouterr().err


def _repo(tmp_path):
    repo = tmp_path / "vault"
    (repo / "mem" / "prefs").mkdir(parents=True)
    (repo / "mem" / "prefs" / "_index.md").write_text("## Index\n")
    for args in (["init", "-q", "-b", "main", str(repo)], ["-C", str(repo), "add", "-A"],
                 ["-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c",
                  "commit.gpgsign=false", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], check=True, capture_output=True)
    return repo


def test_check_reports_each_condition_and_its_exit_code(database, tmp_path, capsys):
    assert run_cli(tmp_path, database, "check") == 3
    assert "not configured" in capsys.readouterr().err
    repo = _repo(tmp_path)
    setting = {"enabled": True, "root": "mem", "targets": {"preference": {
        "shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"}}, "branch": "main", "commit_prefix": "mem"}
    db_call(database, lambda conn: conn.execute(
        "INSERT INTO system_settings (key, value) VALUES ('knowledge_vault', $1), ('memory_write', $2)",
        json.dumps({"path": str(repo.resolve()), "writable": [], "rules_file": None}), json.dumps(setting)))
    assert run_cli(tmp_path, database, "check") == 0
    out = capsys.readouterr().out
    assert "✓ branch: main" in out and "✓ worktree_root:" in out and "记忆写入已打开" in out
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
    assert run_cli(tmp_path, database, "check", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and [item["name"] for item in result["checks"] if not item["ok"]] == ["branch"]


@pytest.mark.parametrize("argv", [("list",), ("show", "M-1"), ("check",), ("reject", "M-1"),
                                  ("retry", "M-1"), ("delete", "M-1")])
def test_every_command_is_refused_inside_an_agent_run(monkeypatch, tmp_path, capsys, argv):
    monkeypatch.setenv("LOOPER_AGENT_RUN", "1")
    assert cli.main(["memory", *argv, "--runtime-root", str(tmp_path)]) == 4
    assert "只供操作者查看" in capsys.readouterr().err


def test_a_database_without_the_tables_is_unusable(database, tmp_path, capsys):
    db_call(database, lambda conn: conn.execute("DROP TABLE memory_candidates CASCADE"))
    assert run_cli(tmp_path, database, "list") == 6
    assert "looper serve" in capsys.readouterr().err


def test_reject_retry_delete_and_state_exit_codes(database, tmp_path, capsys):
    from looper.db import memory as db_memory

    first, _ = seed(database)
    assert run_cli(tmp_path, database, "reject", f"M-{first}") == 3
    capsys.readouterr()
    repo = _repo(tmp_path)
    db_call(database, lambda conn: conn.execute(
        "INSERT INTO system_settings(key,value) VALUES('memory_write',$1)",
        json.dumps({"enabled": True, "root": "mem"})))
    assert run_cli(tmp_path, database, "retry", f"M-{first}") == 7
    capsys.readouterr()

    async def block(conn):
        found = await db_memory.get(first, db=conn)
        await sup.move(conn, first, "confirmed", "confirm", actor="user", via="dashboard",
                       content_hash=found["candidate"]["content_hash"])
        await sup.move(conn, first, "writing", "claim")
        await sup.move(conn, first, "blocked", "block", reason="dirty_target")

    db_call(database, block)
    assert run_cli(tmp_path, database, "retry", f"M-{first}") == 0
    assert "已确认" in capsys.readouterr().out
    db_call(database, lambda conn: sup.move(conn, first, "writing", "claim"))
    db_call(database, lambda conn: sup.move(conn, first, "blocked", "block", reason="dirty_target"))
    assert run_cli(tmp_path, database, "reject", f"M-{first}", "--reason", "no longer useful") == 0
    assert "已驳回" in capsys.readouterr().out
    found = db_call(database, lambda conn: db_memory.get(first, db=conn))
    assert found["events"][-1]["note"] == "no longer useful"
    assert run_cli(tmp_path, database, "delete", f"M-{first}") == 0
    assert "墓碑" in capsys.readouterr().out
    assert db_call(database, lambda conn: db_memory.get(first, db=conn)) is None
    assert db_call(database, lambda conn: db_memory.tombstone_count(db=conn)) == 1
    assert not (repo / "mem/prefs/weekly.md").exists()
    assert run_cli(tmp_path, database, "delete", "M-999999") == 5
    capsys.readouterr()
