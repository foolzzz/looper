"""The operator names a lead; code keeps its words and external provenance."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import memory_settings
from looper.agents.memory_model import Kind
from looper.agents.ops import Call, ErrorCode, OpError
from looper.agents.ops import leads as ops_leads, memory_propose as tool
from tests import leads_support, memory_propose_support as sup, subscription_support as stream
from tests.db_support import DSN, run_db, run_with_channel_configs

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
FROZEN = json.loads((Path(__file__).parent / "fixtures/memory_propose_from_lead_off.json").read_text())


async def setup(pool, tmp_path, *, leads=True):
    await sup.setup(pool, tmp_path)
    raw = dict(await memory_settings.stored(pool))
    raw["sources"]["leads"] = leads
    raw["targets"] = {kind.value: raw["targets"]["preference"] for kind in Kind}
    await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_write'", json.dumps(raw))
    await stream.source(pool)
    seq = await stream.add(pool, "Synthetic release decision", sent_at=datetime.now(timezone.utc))
    return await leads_support.lead(pool, [seq], summary="Ship on Friday", detail="After the review")


async def propose(service, current, arguments):
    binding = f"lead-memory-{id(current)}"
    service.begin_turn(current, binding=binding)
    try:
        return await service.call(tool.TOOL_NAME, arguments, binding=binding)
    finally:
        service.end_turn(current)


def test_from_lead_is_listed_only_with_the_leads_source(monkeypatch, tmp_path):
    async def check(pool):
        await setup(pool, tmp_path, leads=False)
        service = sup.ops()
        listing = await service.current_listing(sup.turn())
        listed = next(item for item in listing if item["name"] == tool.TOOL_NAME)
        assert listed == FROZEN["tools"][tool.TOOL_NAME]
        assert tool.contract_for(listing) == FROZEN["contracts"]["memory"]
        assert tool.TOOL.listing() == listed
        await pool.execute("UPDATE system_settings SET value=jsonb_set(value::jsonb, '{sources,leads}', 'true')::text "
                           "WHERE key='memory_write'")
        listing = await service.current_listing(sup.turn())
        listed = next(item for item in listing if item["name"] == tool.TOOL_NAME)
        assert listed["inputSchema"]["properties"]["from_lead"] == {"type": "string", "maxLength": 40}
        assert listed["inputSchema"]["required"] == []
        assert tool.contract_for(listing).splitlines()[-1] == (
            "To keep a message lead the user points at, pass from_lead with its id instead of restating it.")
        assert await tool.memory_tool() is await tool.memory_tool()
    run_db(check, monkeypatch)


@pytest.mark.parametrize("override", [None, "person"])
def test_from_lead_creates_the_lead_candidate_without_restating(monkeypatch, tmp_path, override):
    async def check(pool):
        number = await setup(pool, tmp_path)
        quote = AsyncMock(side_effect=AssertionError("a lead needs no operator quote"))
        monkeypatch.setattr(tool.domain, "quote_found", quote)
        current = sup.turn(f"把 lead-{number} 记下来")
        answer = await propose(sup.ops(), current, {"from_lead": f"lead-{number}",
                                                  **({"kind": override} if override else {})})
        assert answer["status"] == "ok", answer
        row = await pool.fetchrow("SELECT * FROM memory_candidates")
        assert (row["statement"], row["why"], row["kind"], row["state"]) == (
            "Ship on Friday", "After the review", override or "decision", "drafting")
        assert (row["source_kind"], row["source_class"], row["untrusted"], row["manager_id"]) == (
            "lead", "external", True, "manager")
        source, = await pool.fetch("SELECT * FROM memory_candidate_sources")
        assert source["lead_id"] == number and source["manager_action_id"] is None and source["quote"] is None
        action, = await pool.fetch("SELECT * FROM manager_actions")
        assert action["status"] == "ok" and json.loads(action["result"]) == {
            "candidate": answer["result"]["candidate"], "state": "drafting"}
        event = await pool.fetchrow("SELECT * FROM memory_candidate_events")
        assert (event["actor_kind"], event["via"]) == ("manager", "dashboard")
        assert current.blocks == [f'（已提议记入记忆 {answer["result"]["candidate"]}：Ship on Friday，待你确认）']
        assert ops_leads.for_group("Ship on Friday") == ops_leads.WITHHELD_MEMORY_REPLY
        assert await pool.fetchval("SELECT state FROM message_leads WHERE id=$1", number) == "active"
        quote.assert_not_awaited()
    try:
        run_db(check, monkeypatch)
    finally:
        ops_leads._SEEN.clear()


@pytest.mark.parametrize("extra", [{"statement": "rewritten"}, {"why": "reason"}, {"quote": "quote"},
                                  {"keywords": ["release"]}, {"revise": "M-1"}])
def test_from_lead_refuses_restatement(monkeypatch, tmp_path, extra):
    async def check(pool):
        number = await setup(pool, tmp_path)
        answer = await propose(sup.ops(), sup.turn(), {"from_lead": f"lead-{number}", **extra})
        assert answer["error"] == {"code": "invalid", "message": "with from_lead give only kind, if any"}
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("case,code", [("request", "invalid"), ("preference", "invalid"),
                                      ("missing", "not_found"), ("duplicate", "conflict")])
def test_from_lead_domain_refusals_amend_the_audit(monkeypatch, tmp_path, case, code):
    async def check(pool):
        number = await setup(pool, tmp_path)
        arguments = {"from_lead": f"lead-{number}"}
        if case == "request":
            await pool.execute("UPDATE message_leads SET kind='request' WHERE id=$1", number)
        elif case == "preference":
            arguments["kind"] = "preference"
        elif case == "missing":
            arguments["from_lead"] = "lead-999999"
        elif case == "duplicate":
            assert (await propose(sup.ops(), sup.turn(), arguments))["status"] == "ok"
        current = sup.turn(message_id="refused")
        answer = await propose(sup.ops(), current, arguments)
        assert answer["error"]["code"] == code, answer
        assert current.blocks == []
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == (1 if case == "duplicate" else 0)
        action = await pool.fetchrow("SELECT * FROM manager_actions ORDER BY id DESC LIMIT 1")
        assert (action["status"], action["error_code"]) == ("error", code)
    run_db(check, monkeypatch)


@pytest.mark.parametrize("fields", [{"group": True, "channel": "lark", "channel_id": "channel-lark"},
                                  {"client_id": "synthetic-client"},
                                  {"channel": "lark", "sender": "ou_other", "channel_id": "channel-lark"}])
def test_from_lead_refuses_non_operator_turns(monkeypatch, tmp_path, fields):
    async def check(pool):
        number = await setup(pool, tmp_path)
        current, service = sup.turn(), sup.ops()
        for key, value in fields.items():
            setattr(current, key, value)
        assert tool.TOOL_NAME not in [item["name"] for item in await service.current_listing(current)]
        assert (await propose(service, current, {"from_lead": f"lead-{number}"}))["error"]["code"] == "forbidden"
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
    run_with_channel_configs(check, monkeypatch)


@pytest.mark.parametrize("field", ["kind", "statement", "quote", "keywords"])
def test_normal_proposals_still_require_the_original_fields(monkeypatch, tmp_path, field):
    async def check(pool):
        await setup(pool, tmp_path)
        arguments = {key: value for key, value in sup.ARGS.items() if key != field}
        assert (await propose(sup.ops(), sup.turn(), arguments))["error"] == {
            "code": "invalid", "message": f"{field} is required"}
    run_db(check, monkeypatch)


def test_stale_from_lead_call_cannot_write_after_source_is_disabled(monkeypatch, tmp_path):
    async def check(pool):
        number = await setup(pool, tmp_path)
        listed = await tool.memory_tool()
        await pool.execute("UPDATE system_settings SET value=jsonb_set(value::jsonb, '{sources,leads}', 'false')::text "
                           "WHERE key='memory_write'")
        with pytest.raises(OpError) as error:
            await listed.run(Call({"from_lead": f"lead-{number}"}, sup.turn(), None))
        assert error.value.code is ErrorCode.INVALID
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
    run_db(check, monkeypatch)
