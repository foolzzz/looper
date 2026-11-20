"""The default Manager's read-only vault tools (``ops/manager_vault.py``): only with the setting's ``manager_read`` on,
only in the user's own turns, never a write; their answers; the group backstop over what they returned; and off,
the Manager launched exactly as before.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from looper.agents import vault
from looper.agents.managers_api import ManagerDefinition, ManagerTools, TaskPolicy, TaskScope
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.ops import Call, ErrorCode, OpError, Turn
from looper.agents.ops import leads as ops_leads
from looper.agents.ops import manager_vault
from looper.agents.ops.service import MANAGER_TOOLS, ManagerOps, Platform, scope_for
from tests.db_support import DSN, run_db, run_with_channel_configs
from tests.test_manager_tools_db import _converse

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OP = "ou_operatorsynthetic"
SEARCH, READ, WRITE = "looper_vault_search", "looper_vault_read", "looper_vault_write"
NOTE = "部署说明：Atlas 服务的部署端口是 47219，回滚前先停流量。"


@pytest.fixture(autouse=True)
def _nothing_seen():
    ops_leads._SEEN.clear()
    ops_leads._WITHHELD.clear()
    yield
    ops_leads._SEEN.clear()
    ops_leads._WITHHELD.clear()


def _vault_dir(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "projects").mkdir(parents=True)
    (root / "agent").mkdir()  # a writable folder: the Manager still gets no write
    (root / "projects" / "atlas.md").write_text(f"# Atlas\n\n{NOTE}\n", encoding="utf-8")
    return root


async def _setting(tmp_path: Path, *, manager_read: bool) -> Path:
    root = _vault_dir(tmp_path)
    await vault.update_settings({"path": str(root), "writable": ["agent"], "rules_file": None,
                                 "manager_read": manager_read})
    return root


async def _operator(pool: Any) -> None:
    await pool.execute("UPDATE channel_configs SET config = $1 WHERE channel_id = 'channel-lark'",
                       json.dumps({"allowed_open_ids": [OP, "ou_someoneelse"], "operator_sender_id": OP}))


def _ops() -> ManagerOps:
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok")
    ops._working_at = ops._clock()
    return ops


def _names(listing: list[dict[str, Any]]) -> list[str]:
    return [tool["name"] for tool in listing]


def _web(message_id: str = "m-1", **fields: Any) -> Turn:
    return Turn(channel="web", chat="chat-1", message_id=message_id, **fields)


async def _call(ops: ManagerOps, turn: Turn, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    binding = None if turn.autonomous else f"b-{turn.message_id}-{id(turn)}"
    ops.begin_turn(turn, binding=binding)
    try:
        return await ops.call(name, arguments, binding=binding)
    finally:
        ops.end_turn(turn)


OTHER_TURNS = {
    "group": Turn(channel="lark", chat="oc_group", message_id="m-1", sender=OP, group=True, channel_id="channel-lark"),
    "another-sender": Turn(channel="lark", chat="ou_someoneelse", message_id="m-1", sender="ou_someoneelse",
                           channel_id="channel-lark"),
    "api-client": Turn(channel="web", chat="chat-1", message_id="m-1", client_id="client-1", client_name="script"),
    "autonomous": Turn(channel="web", chat="chat-1"),
    "no-operator-id": Turn(channel="telegram", chat="12345", message_id="m-1", sender="12345",
                           channel_id="channel-telegram"),
}


@pytest.mark.parametrize("setting", ["absent", "manager_read off", "gone"])
def test_without_manager_read_on_a_holding_setting_the_manager_has_no_vault_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setting: str,
) -> None:
    async def test(pool: Any) -> None:
        if setting != "absent":
            root = await _setting(tmp_path, manager_read=setting == "gone")
            if setting == "gone":
                (root / "projects" / "atlas.md").unlink()
                (root / "projects").rmdir()
                (root / "agent").rmdir()
                root.rmdir()  # the setting no longer holds
        listing = _names(await _ops().current_listing(_web()))
        assert not {SEARCH, READ, WRITE} & set(listing)
        assert listing == [tool.name for tool in MANAGER_TOOLS]

    run_with_channel_configs(test, monkeypatch)


def test_with_manager_read_on_the_users_own_turns_get_search_and_read_and_never_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _setting(tmp_path, manager_read=True)
        await _operator(pool)
        await pool.execute("UPDATE channel_configs SET config = $1 WHERE channel_id = 'channel-telegram'",
                           json.dumps({"allowed_user_ids": ["12345"]}))
        ops = _ops()
        dm = Turn(channel="lark", chat=OP, message_id="m-2", sender=OP, channel_id="channel-lark")
        for turn in (_web(), dm):
            listing = _names(await ops.current_listing(turn))
            assert listing[-2:] == [SEARCH, READ] and WRITE not in listing
        for name, turn in OTHER_TURNS.items():
            assert not {SEARCH, READ, WRITE} & set(_names(await ops.current_listing(turn))), name
        # A scenario Manager never, whatever its tools.
        desk = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                          scope=scope_for(ManagerDefinition(
                              id="manager-desk", name="Desk", description="", system_prompt="p", profile_id=None,
                              tools=ManagerTools.ALL, task_scope=TaskScope.ALL, task_policy=TaskPolicy.FREE,
                              enabled=True, builtin=False, revision=-1)))
        assert not {SEARCH, READ, WRITE} & set(_names(await desk.current_listing(_web())))

    run_with_channel_configs(test, monkeypatch)


def test_a_dashboard_turn_searches_and_reads_the_users_notes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _setting(tmp_path, manager_read=True)
        ops = _ops()
        found = await _call(ops, _web(), SEARCH, {"query": "Atlas 端口"})
        assert found["status"] == "ok", found
        result = found["result"]
        assert result["notice"] == manager_vault.NOTICE
        assert [hit["path"] for hit in result["results"]] == ["projects/atlas.md"]
        note = await _call(ops, _web("m-2"), READ, {"path": "projects/atlas.md"})
        assert note["status"] == "ok" and NOTE in note["result"]["content"] and "sha256" not in note["result"]
        # The audit trail keeps no note text.
        stored = await pool.fetch("SELECT tool, status, result FROM manager_actions ORDER BY id")
        assert [(row["tool"], row["status"], row["result"]) for row in stored] == [(SEARCH, "ok", None),
                                                                                (READ, "ok", None)]

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("name", list(OTHER_TURNS))
def test_every_other_turn_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    async def test(pool: Any) -> None:
        await _setting(tmp_path, manager_read=True)
        await _operator(pool)
        answer = await _call(_ops(), OTHER_TURNS[name], SEARCH, {"query": "Atlas"})
        assert answer["status"] == "error" and answer["error"]["code"] == "forbidden", answer
        assert ops_leads._SEEN == {}

    run_with_channel_configs(test, monkeypatch)


def test_the_manager_never_writes_the_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        root = await _setting(tmp_path, manager_read=True)
        answer = await _call(_ops(), _web(), WRITE, {"path": "agent/x.md", "content": "x", "reason": "try"})
        assert answer["status"] == "error", answer
        assert not (root / "agent" / "x.md").exists()

    run_with_channel_configs(test, monkeypatch)


def test_turning_manager_read_off_takes_the_tools_away(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        root = await _setting(tmp_path, manager_read=True)
        ops = _ops()
        assert (await _call(ops, _web(), SEARCH, {"query": "Atlas"}))["status"] == "ok"
        await vault.update_settings({"path": str(root), "writable": ["agent"], "rules_file": None})
        answer = await _call(ops, _web("m-2"), SEARCH, {"query": "Atlas"})
        assert answer["error"] == {"code": "forbidden", "message": f"this Manager no longer has {SEARCH}"}
        with pytest.raises(OpError) as refused:  # a call already admitted when it was turned off
            await manager_vault.TOOLS[0].run(Call({"query": "Atlas"}, _web("m-3"), ops.platform))
        assert refused.value.code is ErrorCode.CONFLICT

    run_with_channel_configs(test, monkeypatch)


def test_a_group_reply_that_quotes_a_note_read_in_the_users_chat_is_not_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    async def test(pool: Any) -> None:
        await _setting(tmp_path, manager_read=True)
        assert (await _call(_ops(), _web(), READ, {"path": "projects/atlas.md"}))["status"] == "ok"
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        group = MessageEnvelope.create(
            source=channel_address("lark", "channel-lark"), target=manager_address("manager"),
            message_type=MessageType.USER_MESSAGE,
            payload={"text": "q", "channel_context": {"chat_id": "oc_group", "chat_type": "group", "sender_id": OP}})
        quoted = "好的：Atlas 服务的部署端口是 `47219`，回滚前先停流量。"
        assert manager._group_safe(group, quoted) == ops_leads.WITHHELD_REPLY
        assert manager._group_safe(group, "大家早上好，今天天气不错。") == "大家早上好，今天天气不错。"
        assert not ops_leads.quotes_a_lead("manager", quoted, now=time.monotonic() + ops_leads.SEEN_SECONDS + 1)

    run_with_channel_configs(test, monkeypatch)


def _prepared(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from looper.agents.tiers.manager import ManagerAgent

    prepared: list[Any] = []
    prepare = ManagerAgent._prepare_turn

    async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await prepare(self, *args, **kwargs)
        if result.signature is not None:
            prepared.append(result)
        return result

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)
    return prepared


def test_off_the_manager_is_launched_as_before_and_on_only_the_vault_reads_are_added(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The golden comparison: a vault configured for tasks but without manager_read launches the Manager with
    main's tool list, prompt and signature shape; manager_read on adds the two reads, the contract and the hash."""
    async def test(pool: Any) -> None:
        prepared = _prepared(monkeypatch)
        root = await _setting(tmp_path, manager_read=False)
        off = await _converse(tmp_path / "off", [[("looper_overview", {})]], ["hi"])
        await vault.update_settings({"path": str(root), "writable": ["agent"], "rules_file": None,
                                     "manager_read": True})
        on = await _converse(tmp_path / "on", [[("looper_overview", {})]], ["hi"])
        before, after = prepared
        assert off.tools == [tool.name for tool in MANAGER_TOOLS]
        assert on.tools == [*off.tools, SEARCH, READ]
        assert before.system_prompt.layers["contract"]["name"] == "TOOLS_CONTRACT"
        assert "vault" not in before.system_prompt.text.lower()
        assert after.system_prompt.layers["contract"]["name"] == "TOOLS_CONTRACT+VAULT_CONTRACT"
        assert after.system_prompt.text.replace("\n" + manager_vault.VAULT_CONTRACT, "", 1) == \
            before.system_prompt.text
        assert len(after.signature) == len(before.signature) + 1  # the listing's hash
        assert not any("vault" in arg for arg in before.signature[0])

    run_db(test, monkeypatch, timeout=60)


def test_the_setting_stores_manager_read_only_when_on_and_checks_it(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        root = await _setting(tmp_path, manager_read=False)
        stored = json.loads(await pool.fetchval("SELECT value FROM system_settings WHERE key = $1", vault.SETTING_KEY))
        assert stored == {"path": str(root.resolve()), "writable": ["agent"], "rules_file": None}  # as before
        assert (await vault.settings_view())["manager_read"] is False
        view = await vault.update_settings({"path": str(root), "writable": ["agent"], "manager_read": True})
        assert view["manager_read"] is True and (await vault.active()).manager_read is True
        with pytest.raises(ValueError, match="manager_read must be true or false"):
            vault.check_config({"path": str(root), "manager_read": "yes"})
        cleared = await vault.update_settings({"path": None})
        assert cleared["configured"] is False and cleared["manager_read"] is False

    run_with_channel_configs(test, monkeypatch)
