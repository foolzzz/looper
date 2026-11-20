"""``manager.stable_context`` off (no row): the Manager's user turns, composed system prompt, tool listing and process
signature are what they were before the switch existed; on, only the turns' start and the contract's end change.

Each case runs one turn with the switch off, then the same turn with it on, on one Manager and one endpoint, and
compares what each launched and sent in full: the default Manager, a scenario Manager, leads on, a custom role prompt,
a message with an attachment and a completion turn. Runs only when LOOPER_TEST_DATABASE_URL points at a disposable
database.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

CASES = ("default", "scenario", "leads", "custom_prompt", "attachment", "completion")
COMPLETION = "[COMPLETION] task task-000000000001 ended: done"
UPLOAD = "upload-000000000001"


async def _setup(case: str, root: Path, pool: Any) -> tuple[Any, dict[str, Any]]:
    """The case's Manager definition (None: the default Manager) and its message's payload."""
    from looper.agents.chat_attachments import store_upload
    from looper.agents.managers_api import ManagerDefinition
    from looper.db import managers

    payload: dict[str, Any] = {"text": "hi", "channel_context": {"session_id": "chat-1"}}
    if case == "scenario":
        async with pool.acquire() as conn:
            row = await managers.insert_manager({"id": "manager-review", "name": "Review", "system_prompt":
                                                 "Review pull requests.", "task_scope": "all"}, conn=conn)
        return ManagerDefinition.from_row(row), payload
    if case == "leads":
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true')")
    if case == "custom_prompt":
        await pool.execute("INSERT INTO role_configs (role, system_prompt) VALUES ('manager', $1) ON CONFLICT (role) "
                           "DO UPDATE SET system_prompt = EXCLUDED.system_prompt", "You are a terse Manager.")
    if case == "attachment":
        saved = store_upload(root, "web", UPLOAD, [("note.txt", b"the code word")])
        payload["attachments"] = [item.to_payload() for item in saved]
    return None, payload


async def turns(case: str, root: Path, pool: Any, monkeypatch: pytest.MonkeyPatch, *,
                on: Any = None) -> list[dict[str, Any]]:
    """The case's turn on one Manager, sent once, then (with ``on``, awaited between them) once more: for each, the
    composed system prompt and its layers, the process signature, the full tool listing and the user turn Claude got."""
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
    from looper.agents.ops import Turn, TurnOrigin
    from looper.agents.ops.service import ManagerOps
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    root.mkdir(parents=True, exist_ok=True)
    definition, payload = await _setup(case, root, pool)
    seen: list[dict[str, Any]] = []
    listed_last: list[Any] = [None]  # the listing the turn being prepared asked for last
    prepare, listing = ManagerAgent._prepare_turn, ManagerOps.current_listing

    async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await prepare(self, *args, **kwargs)
        if result.signature is not None:
            seen.append({"prompt": result.system_prompt.text, "layers": result.system_prompt.layers,
                         "signature": result.signature, "prompt_file": str(result.prompt_file),
                         "tools": listed_last[0]})
        return result

    async def listed(self: Any, *args: Any, **kwargs: Any) -> Any:
        tools = await listing(self, *args, **kwargs)
        listed_last[0] = json.loads(json.dumps(tools))
        return tools

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)
    monkeypatch.setattr(ManagerOps, "current_listing", listed)
    bus = MessageBus(runtime_root=root)
    manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(root, [[], []]), work_dir=root),
                           runtime_root=root, definition=definition)
    manager._bus = bus
    web = channel_address("web")
    async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda task_id: False), root):
        for index in range(2 if on is not None else 1):
            if index:
                await on()
            envelope = MessageEnvelope.create(
                source=web, target=manager_address(manager.agent_id), message_type=MessageType.USER_MESSAGE,
                payload=payload, correlation_id=f"corr-{index}", metadata={"origin_channel_id": "channel-web"})
            if case == "completion":
                await manager._run_turn(COMPLETION, envelope, ops_turn=Turn(
                    channel="web", chat="chat-1", origin=TurnOrigin.COMPLETION, envelope=envelope))
            else:
                await manager._handle(envelope)
        await manager.stop()
    await bus.shutdown()
    sent = [json.loads(line) for line in (root / "turns.jsonl").read_text().splitlines()]
    assert len(sent) == len(seen)
    for view, text in zip(seen, sent):
        view["turn"] = text
    return seen


@pytest.mark.parametrize("case", CASES)
def test_off_the_manager_is_launched_and_told_as_before_and_on_only_the_turns_start_and_contract_end_change(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.tiers.state_view import STABLE_CONTEXT_CONTRACT, STABLE_CONTEXT_KEY

    async def test(pool: Any) -> None:
        async def turn_on() -> None:
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true')", STABLE_CONTEXT_KEY)

        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1", STABLE_CONTEXT_KEY) == 0
        off, on = await turns(case, tmp_path, pool, monkeypatch, on=turn_on)

        # The user turn: off, the sections (none for a completion turn) and the message, no block; on, the whole
        # block is those sections under its rev line, and the rest is the same.
        assert "Looper state" not in off["turn"]
        if case == "completion":
            assert off["turn"] == on["turn"] == COMPLETION
        else:
            assert off["turn"].startswith("Projects:\n")
            rev = on["turn"].partition("\n")[0].removeprefix("Looper state (rev ").removesuffix("):")
            assert on["turn"] == f"Looper state (rev {rev}):\n{off['turn']}"
        # Each case is what it says.
        assert off["layers"]["contract"]["name"] == {"scenario": "TOOLS_CONTRACT+SCOPE_CONTRACT",
                                                     "leads": "TOOLS_CONTRACT+LEADS_CONTRACT"}.get(case, "TOOLS_CONTRACT")
        assert ("looper_leads_search" in [tool["name"] for tool in off["tools"]]) is (case == "leads")
        assert ("You are a terse Manager." in off["prompt"]) is (case == "custom_prompt")
        assert ('1. file "note.txt" (text/plain, 13 B)' in off["turn"]) is (case == "attachment")
        # The tool listing, in full.
        assert off["tools"] == on["tools"] and off["tools"]
        # The system prompt: on, the off prompt with the block's contract after it; every layer but the contract's
        # name the same.
        assert "Looper state" not in off["prompt"]
        assert on["prompt"] == off["prompt"] + "\n" + STABLE_CONTEXT_CONTRACT
        name = off["layers"]["contract"]["name"]
        assert on["layers"]["contract"]["name"] == name + "+STABLE_CONTEXT_CONTRACT"
        assert {k: v for k, v in on["layers"].items() if k != "contract"} == \
            {k: v for k, v in off["layers"].items() if k != "contract"}
        # The process signature: the same but for the prompt's hash and the file named by it.
        assert len(on["signature"]) == len(off["signature"])
        assert [arg.replace(on["prompt_file"], off["prompt_file"]) for arg in on["signature"][0]] == \
            list(off["signature"][0])
        assert on["signature"][2:] == off["signature"][2:]
        assert off["signature"][1] != on["signature"][1]

    run_db(test, monkeypatch, timeout=90)
