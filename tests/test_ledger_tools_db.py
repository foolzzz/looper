"""The Manager's ledger tools through ``ManagerOps.call``: who is speaking, the quote, the line code adds, what a group
sees, and that the tools exist exactly while the ledger is on, for the default Manager only."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from looper.agents.ops import Turn
from looper.agents.ops import ledger as tools
from looper.agents.ops.service import DEFAULT_SCOPE, MANAGER_TOOLS, ManagerOps, Platform, scope_for
from looper.agents.managers_api import ManagerDefinition, ManagerTools, TaskPolicy, TaskScope
from looper.db import ledger
from tests.db_support import DSN, run_db

OPERATOR = "ou_operator01"
OTHER = "ou_colleague01"


def turn(text="Let's use PostgreSQL for it.", *, channel="web", chat="c1", sender=None, group=False, channel_id=None,
         client_id=None, client_name=None, message_id="m1"):
    return Turn(channel=channel, chat=chat, message_id=message_id, sender=sender, group=group,
                channel_id=channel_id or f"channel-{channel}", client_id=client_id, client_name=client_name,
                envelope=SimpleNamespace(payload={"text": text}))


@pytest.mark.parametrize("kwargs,operator,expect", [
    ({"channel": "web"}, None, ("user", "dashboard", None)),
    ({"channel": "web", "client_id": "k1", "client_name": "demo"}, None, ("relayed", "api", "client:demo")),
    ({"channel": "lark", "sender": OPERATOR}, OPERATOR, ("user", "lark", None)),
    ({"channel": "lark_bot", "sender": OPERATOR}, OPERATOR, ("user", "lark", None)),
    ({"channel": "telegram", "sender": "7"}, "7", ("user", "telegram", None)),
    ({"channel": "lark", "sender": OTHER}, OPERATOR, ("relayed", "lark", f"lark:{OTHER}")),
    ({"channel": "lark", "sender": OPERATOR}, None, ("relayed", "lark", f"lark:{OPERATOR}")),  # no "my ID" set
    ({"channel": "lark", "sender": OPERATOR, "group": True}, OPERATOR, ("relayed", "lark", f"lark:{OPERATOR}")),
    ({"channel": "telegram", "sender": "7", "group": True}, "7", ("relayed", "telegram", "telegram:7")),
    ({"channel": "lark", "sender": OPERATOR, "client_id": "k1"}, OPERATOR, ("relayed", "api", "client:k1")),
    ({"channel": "somewhere", "sender": OPERATOR}, OPERATOR, ("relayed", "api", "channel:somewhere")),
])
def test_who_is_speaking_follows_the_turns_typed_facts(kwargs, operator, expect):
    actor = tools.actor_for(turn(**kwargs), operator, manager_id="manager")
    assert (actor.kind, actor.via, actor.relayed_by) == expect
    assert actor.manager_id == "manager" and actor.message_id == "m1"


@pytest.mark.parametrize("quote,ok", [
    ("Use PostgreSQL", False),                      # not a passage: case differs
    ("Let's use PostgreSQL", True),
    ("  Let's   use\nPostgreSQL  ", True),           # trimmed and whitespace-merged
    ("use PostgreSQL for it. And more", False),
    ("", True), (None, True),
])
def test_a_quote_is_a_passage_of_the_message_answered(quote, ok):
    if ok:
        assert tools.check_quote(quote, turn()) == (" ".join(quote.split()) if quote and quote.strip() else None)
    else:
        with pytest.raises(Exception, match="exact excerpt"):
            tools.check_quote(quote, turn())


def test_the_two_tool_definitions_stay_small():
    size = sum(len(json.dumps(t.listing(), separators=(",", ":"))) for t in tools.TOOLS)
    assert size <= 1800, size


async def enable(pool, on=True):
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ('ledger.enabled', $1) "
                       "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", "true" if on else "false")


def make_ops(scope=None):
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:1", scope=scope)
    return ops


async def say(ops, name, arguments, the_turn):
    ops._turn = the_turn
    return await ops.call(name, arguments)


def test_the_tools_exist_exactly_while_the_ledger_is_on_and_only_for_the_default_manager(monkeypatch):
    async def check(pool):
        ops = make_ops()
        baseline = [t.listing() for t in MANAGER_TOOLS if t.name != "looper_task_create"]
        off = await ops.current_listing()
        assert [t["name"] for t in off if t["name"] != "looper_task_create"] == [b["name"] for b in baseline]
        assert not any(t["name"].startswith("looper_ledger") for t in off)
        before = json.dumps(off, sort_keys=True)
        answer = await say(ops, "looper_ledger_add", {"kind": "pending", "title": "T"}, turn())
        assert answer["status"] == "error" and "no tool" in answer["error"]["message"]
        await enable(pool)
        on = await ops.current_listing()
        assert [t["name"] for t in on[-2:]] == list(tools.TOOL_NAMES) and on[:-2] == json.loads(before)
        await enable(pool, False)  # and off again: byte-identical to before
        assert json.dumps(await ops.current_listing(), sort_keys=True) == before
        await enable(pool)
        other = ManagerDefinition(id="other", name="Other", description="", system_prompt=None, profile_id=None,
                                  tools=ManagerTools.ALL, task_scope=TaskScope.ALL, task_policy=TaskPolicy.FREE,
                                  enabled=True, builtin=False, revision=-1)
        scoped = make_ops(scope_for(other))
        assert not any(t["name"].startswith("looper_ledger") for t in await scoped.current_listing())

    run_db(check, monkeypatch, timeout=30)


def test_a_decision_is_recorded_as_said_and_the_reply_names_it(monkeypatch):
    async def check(pool):
        await enable(pool)
        ops = make_ops()
        the_turn = turn("OK, 就用第二个方案吧。")
        answer = await say(ops, "looper_ledger_add", {"kind": "decision", "title": "Use the second plan",
                                                      "quote": "就用第二个方案"}, the_turn)
        assert answer["status"] == "ok", answer
        item = answer["result"]["item"]
        assert (item["id"], item["assurance"], item["kind"]) == ("L-0001", "user", "decision")
        assert the_turn.blocks == ["已记入账本 L-0001：Use the second plan（说「L-0001 不对」可以改或撤销）"]
        event = (await ledger.list_events(1, db=pool))[0]
        assert (event["actor_kind"], event["via"], event["quote"], event["manager_id"], event["message_id"]) == (
            "user", "dashboard", "就用第二个方案", "manager", "m1")
        for arguments, message in (
                ({"kind": "decision", "title": "T"}, "needs the quote"),
                ({"kind": "decision", "title": "T", "quote": "made up words"}, "exact excerpt"),
                ({"kind": "pending", "title": "T", "quote": "made up words"}, "exact excerpt")):
            refused = await say(ops, "looper_ledger_add", arguments, turn("OK, 就用第二个方案吧。"))
            assert refused["status"] == "error" and message in refused["error"]["message"], refused
        assert await pool.fetchval("SELECT count(*) FROM ledger_items") == 1

    run_db(check, monkeypatch, timeout=30)


def test_a_relayed_voice_adds_and_notes_but_cannot_confirm_or_close(monkeypatch):
    async def check(pool):
        await enable(pool)
        ops = make_ops()
        group = turn("定了，用 Redis", channel="lark", sender=OTHER, group=True, chat="g1", channel_id="channel-lark")
        added = await say(ops, "looper_ledger_add", {"kind": "decision", "title": "Use Redis", "quote": "定了"}, group)
        assert added["result"]["item"]["assurance"] == "relayed"
        assert group.blocks == ["已记入账本 L-0001（转述，待本人核实）：Use Redis"]
        for action in ("confirm", "close", "cancel", "reopen"):
            answer = await say(ops, "looper_ledger_update", {"action": action, "item": "L-1"},
                               turn("ok", channel="lark", sender=OTHER, group=True, chat="g1",
                                    channel_id="channel-lark", message_id="m2"))
            assert answer["status"] == "error" and answer["error"]["code"] == "forbidden", (action, answer)
        noted = await say(ops, "looper_ledger_update", {"action": "note", "item": "L-1", "note": "seen"},
                          turn("seen", channel="lark", sender=OTHER, group=True, chat="g1",
                               channel_id="channel-lark", message_id="m3"))
        assert noted["status"] == "ok"
        # the user's own message on the dashboard confirms it
        the_turn = turn("确认，就是 Redis")
        confirmed = await say(ops, "looper_ledger_update", {"action": "confirm", "item": "L-1"}, the_turn)
        assert confirmed["status"] == "ok" and confirmed["result"]["item"]["assurance"] == "user"
        assert the_turn.blocks == ["L-0001 已确认"]

    run_db(check, monkeypatch, timeout=30)


def test_a_group_sees_only_what_was_written_in_it(monkeypatch):
    async def check(pool):
        await enable(pool)
        ops = make_ops()
        for chat, title in (("g1", "Mine"), ("g2", "Theirs")):
            await say(ops, "looper_ledger_add", {"kind": "pending", "title": title},
                      turn("x", channel="lark", sender=OTHER, group=True, chat=chat, channel_id="channel-lark"))
        await say(ops, "looper_ledger_add", {"kind": "pending", "title": "Dashboard"}, turn())
        here = turn("x", channel="lark", sender=OTHER, group=True, chat="g1", channel_id="channel-lark", message_id="m9")
        listed = await say(ops, "looper_ledger_update", {"action": "list", "status": "all"}, here)
        assert [i["title"] for i in listed["result"]["items"]] == ["Mine"]
        for item in ("L-2", "L-3"):
            for action in ("get", "note"):
                answer = await say(ops, "looper_ledger_update", {"action": action, "item": item, "note": "n"}, here)
                assert answer["error"]["code"] == "not_found", (item, action, answer)
        everything = await say(ops, "looper_ledger_update", {"action": "list", "status": "all"}, turn(message_id="m8"))
        assert len(everything["result"]["items"]) == 3

    run_db(check, monkeypatch, timeout=30)


def test_a_turn_the_model_runs_on_its_own_cannot_write_but_can_read(monkeypatch):
    async def check(pool):
        await enable(pool)
        ops = make_ops()
        own = Turn(channel=None, chat=None)
        refused = await say(ops, "looper_ledger_add", {"kind": "pending", "title": "T"}, own)
        assert refused["error"]["code"] == "needs_user_turn"
        assert (await say(ops, "looper_ledger_update", {"action": "list"}, own))["status"] == "ok"

    run_db(check, monkeypatch, timeout=30)


def test_arguments_owner_refs_due_project_and_supersedes(monkeypatch):
    async def check(pool):
        await enable(pool)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj', 'Demo project')")
        await pool.execute("INSERT INTO tasks (id, requirement, project_id) VALUES ('task-1', 'r', 'proj')")
        ops = make_ops()
        the_turn = turn("Reply by Friday, and use Redis.")
        ok = await say(ops, "looper_ledger_add", {
            "kind": "pending", "title": "Reply", "due": "2030-05-01", "project": "Demo project", "owner": "task-1",
            "refs": ["task-1", "L-7", "I-9", "https://example.com/x", "vault:notes/a.md"]}, the_turn)
        assert ok["status"] == "ok", ok
        item = await ledger.get_item(1, db=pool)
        assert (item["owner"], item["project_id"], item["due_at"].year) == ("task:task-1", "proj", 2030)
        assert [r["kind"] for r in item["refs"]] == ["task", "ledger", "intent", "url", "vault"]
        for arguments in ({"owner": "nobody"}, {"refs": ["not a ref"]}, {"project": "nope"}, {"due": "soon"},
                          {"supersedes": "L-1"}):
            bad = await say(ops, "looper_ledger_add", {"kind": "pending", "title": "T", **arguments}, turn())
            assert bad["status"] == "error", arguments
        first = await say(ops, "looper_ledger_add", {"kind": "decision", "title": "Use Redis", "quote": "use Redis"},
                          turn("Reply by Friday, and use Redis.", message_id="m4"))
        second = await say(ops, "looper_ledger_add", {"kind": "decision", "title": "Use SQLite", "quote": "use SQLite",
                                                      "supersedes": first["result"]["item"]["id"]},
                           turn("Actually use SQLite.", message_id="m5"))
        assert second["status"] == "ok", second
        assert (await ledger.get_item(2, db=pool))["status"] == "superseded"
        relayed = await say(ops, "looper_ledger_add", {"kind": "decision", "title": "X", "quote": "use SQLite",
                                                       "supersedes": "L-3"},
                            turn("use SQLite", channel="lark", sender=OTHER, group=True, channel_id="channel-lark",
                                 message_id="m6"))
        assert relayed["error"]["code"] == "forbidden"
        amended = await say(ops, "looper_ledger_update", {"action": "amend", "item": "L-1", "fields": {
            "title": "Reply Monday", "due": None, "detail": "d", "owner": "manager", "project": None}},
            turn("move it", message_id="m7"))
        assert amended["status"] == "ok", amended
        item = await ledger.get_item(1, db=pool)
        assert (item["title"], item["due_at"], item["owner"], item["project_id"]) == (
            "Reply Monday", None, "manager:manager", None)
        got = await say(ops, "looper_ledger_update", {"action": "get", "item": "1"}, turn(message_id="m10"))
        assert [e["action"] for e in got["result"]["events"]] == ["add", "amend"]

    run_db(check, monkeypatch, timeout=60)
