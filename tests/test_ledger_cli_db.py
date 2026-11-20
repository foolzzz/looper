"""``looper ledger`` end to end: the ops file, the database it names (no server running), and every exit code."""

import asyncio
import json

import asyncpg
import pytest

from looper import cli
from looper.db import ledger
from tests import ledger_support as sup
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def not_in_an_agent(monkeypatch):
    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)


def run_cli(tmp_path, url, *argv):
    config = tmp_path / "server.toml"
    config.write_text(f'[database]\nurl = "{url}"\n')
    config.chmod(0o600)
    return cli.main(["ledger", argv[0], *argv[1:], "--runtime-root", str(tmp_path / "root"), "--config", str(config)])


def write(url, sql, *args):
    async def go():
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(go())


def relay_decision(url):
    async def go():
        conn = await asyncpg.connect(url)
        try:
            return await sup.add_own(conn, "decision", sup.RELAYED, title="Use PostgreSQL")
        finally:
            await conn.close()

    return asyncio.run(go())


def enable(url):
    write(url, "INSERT INTO system_settings (key, value) VALUES ('ledger.enabled', 'true')")


def test_a_disabled_ledger_is_exit_3_for_every_command(tmp_path, capsys):
    with fresh_database_without_recording(DSN) as (url, _):
        for argv in (("list",), ("show", "L-1"), ("add", "--kind", "pending", "--title", "T", "--session", "s"),
                     ("close", "L-1", "--session", "s"), ("confirm", "L-1", "--session", "s")):
            assert run_cli(tmp_path, url, *argv) == 3, argv
        assert "not enabled" in capsys.readouterr().err
        assert write(url, "SELECT count(*) AS n FROM ledger_items")[0]["n"] == 0


def test_add_list_show_close_with_the_service_stopped(tmp_path, capsys):
    with fresh_database_without_recording(DSN) as (url, _):
        enable(url)
        assert run_cli(tmp_path, url, "list") == 1  # nothing yet: "no results" is not an error
        assert "no open items" in capsys.readouterr().out
        assert run_cli(tmp_path, url, "add", "--kind", "pending", "--title", "Reply by Friday", "--detail", "d",
                       "--due", "2030-05-01", "--ref", "url:https://example.com", "--ref", "L-1",
                       "--quote", "reply by friday", "--session", "tool 2030-01-01 reply") == 0
        assert "L-0001" in capsys.readouterr().out
        assert run_cli(tmp_path, url, "list") == 0
        assert "Reply by Friday" in capsys.readouterr().out
        assert run_cli(tmp_path, url, "list", "--json") == 0
        listed = json.loads(capsys.readouterr().out)
        assert (listed["more"], [i["id"] for i in listed["items"]], listed["items"][0]["assurance"]) == (0, ["L-0001"], "user")
        assert run_cli(tmp_path, url, "show", "l-1") == 0
        shown = capsys.readouterr().out
        assert "cli · tool 2030-01-01 reply" in shown and "reply by friday" in shown
        assert run_cli(tmp_path, url, "show", "1", "--json") == 0
        (event,) = json.loads(capsys.readouterr().out)["events"]
        assert (event["actor_kind"], event["via"], event["session_label"]) == ("user", "cli", "tool 2030-01-01 reply")
        assert run_cli(tmp_path, url, "close", "L-1", "--note", "sent", "--session", "s") == 0
        capsys.readouterr()
        assert run_cli(tmp_path, url, "list") == 1
        assert run_cli(tmp_path, url, "list", "--status", "done") == 0
        assert run_cli(tmp_path, url, "close", "L-1", "--session", "s") == 4  # already closed
        assert "not open" in capsys.readouterr().err


def test_confirm_turns_a_relayed_decision_into_the_users(tmp_path, capsys):
    with fresh_database_without_recording(DSN) as (url, _):
        enable(url)
        item = relay_decision(url)
        assert item["assurance"] == "relayed"
        assert run_cli(tmp_path, url, "confirm", item["id"], "--session", "s") == 0
        assert run_cli(tmp_path, url, "confirm", item["id"], "--session", "s") == 4
        assert run_cli(tmp_path, url, "close", item["id"], "--cancel", "--session", "s") == 0
        assert write(url, "SELECT status FROM ledger_items")[0]["status"] == "cancelled"


@pytest.mark.parametrize("argv,code", [
    (("show", "L-99"), 5), (("close", "L-99", "--session", "s"), 5), (("confirm", "99", "--session", "s"), 5),
    (("show", "twelve"), 2), (("list", "--limit", "0"), 2), (("list", "--project", "nope"), 2),
    (("add", "--kind", "pending", "--title", " ", "--session", "s"), 2),
    (("add", "--kind", "pending", "--title", "T", "--due", "soon", "--session", "s"), 2),
    (("add", "--kind", "pending", "--title", "T", "--ref", "mail:x", "--session", "s"), 2),
    (("add", "--kind", "pending", "--title", "T", "--project", "nope", "--session", "s"), 2),
    (("add", "--kind", "pending", "--title", "T"), 2),            # a write names its session
    (("close", "L-1", "--session", " "), 2),
])
def test_exit_codes_for_what_is_missing_or_wrong(tmp_path, capsys, argv, code):
    with fresh_database_without_recording(DSN) as (url, _):
        enable(url)
        assert run_cli(tmp_path, url, *argv) == code
        assert write(url, "SELECT count(*) AS n FROM ledger_items")[0]["n"] == 0


@pytest.mark.parametrize("argv", [
    ("add", "--kind", "pending", "--title", "T"), ("close", "L-1"), ("confirm", "L-1")])
def test_a_write_is_refused_inside_an_agent_run(tmp_path, capsys, monkeypatch, argv):
    with fresh_database_without_recording(DSN) as (url, _):
        enable(url)
        item = relay_decision(url)
        monkeypatch.setenv("LOOPER_AGENT_RUN", "worker")
        assert run_cli(tmp_path, url, *argv, "--session", "s") == 4
        assert "own terminal" in capsys.readouterr().err
        assert write(url, "SELECT count(*) AS n FROM ledger_items")[0]["n"] == 1
        assert write(url, "SELECT count(*) AS n FROM ledger_events")[0]["n"] == 1
        assert run_cli(tmp_path, url, "list") == 0  # a read is not
        assert run_cli(tmp_path, url, "show", item["id"]) == 0


def test_an_unusable_database_is_exit_6(tmp_path, capsys):
    assert run_cli(tmp_path, "postgresql://nobody@127.0.0.1:1/none", "list") == 6
    assert "database" in capsys.readouterr().err
    with fresh_database_without_recording(DSN) as (url, _):
        write(url, "DROP TABLE ledger_events, ledger_items")
        assert run_cli(tmp_path, url, "list") == 6
        assert "no ledger tables" in capsys.readouterr().err


def test_the_cli_writes_what_apply_would(tmp_path):
    with fresh_database_without_recording(DSN) as (url, _):
        enable(url)
        assert run_cli(tmp_path, url, "add", "--kind", "decision", "--title", "Use PostgreSQL", "--session", "s") == 0
        row = write(url, "SELECT i.confirmed_actor, e.actor_kind, e.via FROM ledger_items i "
                         "JOIN ledger_events e ON e.item_id = i.id")[0]
        assert (row["confirmed_actor"], row["actor_kind"], row["via"]) == ("user", "user", "cli")
        assert ledger.format_id(1) == "L-0001"
