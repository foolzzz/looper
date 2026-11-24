"""Fail-closed gate for every operator data source offered to routed Manager sessions."""

from __future__ import annotations

import json
from dataclasses import replace
from types import MappingProxyType
from pathlib import Path
from typing import Any

import pytest

from looper.agents import attention, lanes_settings, operator_profile, persona, vault
from looper.agents.ops import Risk, Tool, Turn
from looper.agents.ops import service
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers.manager import GoalSnapshot, ManagerAgent
from tests.db_support import DSN
from tests.lark_cli_stand_in import OPEN_ID, OTHER
from tests.test_manager_session_routing_db import (  # noqa: F401
    LARK, _lark, _published, _Resident, _run, _Rig, routed,
)

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# Every new kind of operator data must add a different sentinel here and seed its actual source below.
SENTINELS = {name: f"PRIVATE_{name.upper()}_SENTINEL" for name in (
    "ready_task", "project", "work_dir", "profile", "memory_candidate", "project_lesson", "intent",
    "reminder", "ledger", "confirmation", "needs_you", "other_gate", "vault_note", "lead",
    "harness", "role_prompt", "custom_prompt", "template", "workflow", "goal", "persona_tone", "persona_notes",
)}
MAIN_CHAT = "oc_allowlist_main"


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def _seed(pool: Any, root: Path) -> None:
    await pool.execute("INSERT INTO projects (id, name, work_dir) VALUES ('private-project', $1, $2)",
                       SENTINELS["project"], SENTINELS["work_dir"])
    await pool.execute("INSERT INTO tasks (id, requirement, status, project_id, origin_channel_id, chat_id) "
                       "VALUES ('private-task', $1, 'init', 'private-project', 'channel-lark', $2)",
                       SENTINELS["ready_task"], MAIN_CHAT)
    await pool.execute("INSERT INTO task_steps (id, task_id, step_index, instruction, kind, step_key) "
                       "VALUES ('private-gate', 'private-task', 0, 'question', 'gate', 'approval')")
    await pool.execute("INSERT INTO task_user_gates (step_id, task_id, topic, state, opened_at) "
                       "VALUES ('private-gate', 'private-task', $1, 'open', now())", SENTINELS["other_gate"])
    await pool.execute("INSERT INTO memory_candidates (kind, source_kind, source_class, statement, fingerprint) "
                       "VALUES ('fact', 'manager_turn', 'operator', $1, $2)",
                       SENTINELS["memory_candidate"], "1" * 64)
    await pool.execute("INSERT INTO memory_candidates (kind, source_kind, source_class, destination, state, "
                       "statement, fingerprint, project_id, valid_until) VALUES "
                       "('lesson', 'task', 'agent', 'looper', 'in_effect', $1, $2, 'private-project', now() + interval '1 day')",
                       SENTINELS["project_lesson"], "2" * 64)
    await pool.execute("INSERT INTO event_sources (id, kind, name) VALUES ('private-source', 'lark_user', 'Synthetic')")
    await pool.execute("INSERT INTO intent_types (key, version) VALUES ('synthetic_type', 1)")
    await pool.execute("INSERT INTO intent_type_versions (type_key, version, name, guidance, priority, edited_via) "
                       "VALUES ('synthetic_type', 1, 'Synthetic type', 'Synthetic guidance', 'normal', 'dashboard')")
    await pool.execute("INSERT INTO intents (source_id, type_key, type_version, title, confidence, chat_id) "
                       "VALUES ('private-source', 'synthetic_type', 1, $1, 'high', $2)", SENTINELS["intent"], MAIN_CHAT)
    await pool.execute("INSERT INTO reminders (text, due_at, timezone, channel_id, chat, created_channel) "
                       "VALUES ($1, now() + interval '1 day', 'UTC', 'channel-lark', $2, 'lark')",
                       SENTINELS["reminder"], MAIN_CHAT)
    for name in ("ledger", "needs_you"):
        await pool.execute("INSERT INTO ledger_items (kind, title, status) VALUES ('pending', $1, 'open')",
                           SENTINELS[name])
    await pool.execute("INSERT INTO message_leads (source_id, kind, summary, confidence, valid_until, "
                       "first_sent_at, last_sent_at) VALUES ('private-source', 'fact', $1, 'high', "
                       "now() + interval '1 day', now(), now())", SENTINELS["lead"])
    await pool.execute("INSERT INTO manager_task_templates (manager_id, key, name) "
                       "VALUES ('manager', 'synthetic-template', $1)", SENTINELS["template"])
    await pool.execute("INSERT INTO workflows (key, name, definition) VALUES ('synthetic-workflow', $1, '{}'::jsonb)",
                       SENTINELS["workflow"])
    await pool.execute("INSERT INTO role_configs (role, system_prompt) VALUES ('manager', $1) "
                       "ON CONFLICT (role) DO UPDATE SET system_prompt = EXCLUDED.system_prompt", SENTINELS["role_prompt"])
    await pool.execute("INSERT INTO agent_setting_layers (scope, role, harness_prompt) VALUES ('role', 'manager', $1) "
                       "ON CONFLICT DO NOTHING", SENTINELS["harness"])
    await pool.execute("UPDATE agent_profiles SET custom_prompt = $1", SENTINELS["custom_prompt"])
    note_root = root / "synthetic-vault"
    note_root.mkdir()
    (note_root / "关于我.md").write_text(f"## Name\n{SENTINELS['profile']}\n", encoding="utf-8")
    (note_root / "private.md").write_text(f"# Synthetic note\n{SENTINELS['vault_note']}\n", encoding="utf-8")
    await vault.update_settings({"path": str(note_root.resolve()), "writable": [], "rules_file": None,
                                 "manager_read": True})
    for key in ("manager.stable_context", "manager.profile.enabled", "leads.enabled", "intents.enabled",
                "intents.recognize.enabled", "reminders.enabled", "ledger.enabled", "attention.enabled",
                "manager.session_search.enabled", "manager.lanes.enabled"):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true') "
                           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key)


async def _operator(channel_id: str) -> str:
    return OPEN_ID


def _arguments(schema: dict[str, Any]) -> dict[str, Any]:
    """Plausible required arguments; actions use their listed first option (normally list/get)."""
    def value(item: dict[str, Any]) -> Any:
        if "enum" in item:
            return item["enum"][0]
        kind = item.get("type")
        if kind == "integer" or kind == "number":
            return item.get("minimum", 1)
        if kind == "boolean":
            return True
        if kind == "object":
            return _arguments(item)
        if kind == "array":
            return []
        return "synthetic"
    return {key: value(schema.get("properties", {}).get(key, {})) for key in schema.get("required", [])}


@pytest.mark.parametrize("kind", [SessionKind.GROUP, SessionKind.DM])
def test_operator_data_never_reaches_a_routed_session(routed: Any, tmp_path: Path, kind: SessionKind) -> None:
    async def test(rig: _Rig, pool: Any) -> None:
        await _seed(pool, tmp_path)
        rig.manager._goal_snapshots["synthetic-goal"] = GoalSnapshot("synthetic-goal", title=SENTINELS["goal"])
        routed.setattr(persona, "_current", persona.Persona(name="Synthetic Manager", tone=SENTINELS["persona_tone"], notes=SENTINELS["persona_notes"]))
        routed.setattr(lanes_settings, "_enabled", True)
        routed.setattr(service, "operator_sender_id", _operator)
        captures: dict[str, list[dict[str, Any]]] = {}
        prepare = ManagerAgent._prepare_turn

        async def capture(self: Any, *args: Any, **kwargs: Any) -> Any:
            prepared = await prepare(self, *args, **kwargs)
            if prepared.signature is not None:
                captures.setdefault(self._session_now().key, []).append({
                    "system_prompt": prepared.system_prompt.text, "turn": prepared.turn_text,
                    "state": prepared.state, "tools": await self.ops.current_listing(prepared.turn),
                })
            return prepared

        routed.setattr(ManagerAgent, "_prepare_turn", capture)
        ops = ManagerOps(Platform(bus=None, task_router=None, manager=rig.manager),
                         url="http://127.0.0.1:9/", token="synthetic-token")
        ops._working_at = ops._clock()
        rig.manager.ops = ops
        operator_turn = Turn(channel="lark", channel_id=LARK.agent_id, chat=MAIN_CHAT,
                             message_id="synthetic-confirmation", sender=OPEN_ID)
        ops._pending[operator_turn.chat_key] = service._Pending(
            id="synthetic-confirmation", tool=service.CONFIRM, arguments={},
            block=SENTINELS["confirmation"], asked_in="earlier-message", at=ops._clock(), confirmation={},
            asker=operator_turn)
        # A newly registered global read must also default to unavailable in every non-main session.
        async def future_read(call: Any) -> dict[str, Any]:
            return {"project": dict(await pool.fetchrow("SELECT * FROM projects WHERE id = 'private-project'"))}
        future = Tool("synthetic_future_read", "Read operator state", {"type": "object", "properties": {}},
                      Risk.READ, future_read)
        ops._scope = replace(ops._scope, tools=MappingProxyType({**ops._scope.tools, future.name: future}))

        main_results: dict[str, Any] = {}
        async def main_calls(on_event: Any) -> None:
            binding = rig.manager._running_token.binding
            for name, arguments in {
                "looper_tasks": {"action": "get", "task_id": "private-task"},
                "looper_projects": {"action": "list"},
                "looper_reminder_list": {},
                "looper_vault_read": {"path": "private.md"},
                "looper_leads_search": {"query": SENTINELS["lead"]},
            }.items():
                main_results[name] = await ops.call(name, arguments, binding=binding)

        rig.main.on_turn = main_calls
        await rig.reply(await _published(rig, _lark("operator positive control", MAIN_CHAT, OPEN_ID, group=False)))
        main_input = _encoded(captures["main"]) + "\n".join(rig.main.turns)
        for name in ("ready_task", "project", "work_dir", "profile", "confirmation", "template", "workflow", "goal",
                     "harness", "role_prompt", "custom_prompt", "persona_tone", "persona_notes"):
            assert SENTINELS[name] in main_input, (name, main_input)
        for name in ("ready_task", "project", "work_dir", "reminder", "vault_note", "lead", "other_gate"):
            assert SENTINELS[name] in _encoded(main_results), (name, main_results)
        # These sources have no Manager read tool: read the real operator views to prove the fixture is live.
        assert SENTINELS["memory_candidate"] in _encoded(await pool.fetch("SELECT statement FROM memory_candidates"))
        assert SENTINELS["project_lesson"] in _encoded(await pool.fetch("SELECT statement FROM memory_candidates"))
        summary = await attention.collect()
        for name in ("needs_you", "ledger", "intent"):
            assert SENTINELS[name] in _encoded(summary), (name, summary)
        assert SENTINELS["profile"] in await operator_profile.resident_text()

        scope = await ops.current_scope()
        ops._scope = replace(scope, tools=MappingProxyType({**scope.tools, future.name: future}))
        global_tools = dict(ops._scope.tools)
        assert {"looper_tasks", "looper_projects", "looper_costs", "looper_settings", future.name} <= global_tools.keys()
        main_turn = Turn(channel="lark", channel_id=LARK.agent_id, chat=MAIN_CHAT,
                         message_id="future-control", sender=OPEN_ID)
        ops.begin_turn(main_turn, binding="future-control")
        try:
            assert future.name in {item["name"] for item in await ops.current_listing(main_turn)}
            assert SENTINELS["project"] in _encoded(await ops.call(future.name, {}, binding="future-control"))
        finally:
            ops.end_turn(main_turn)
        # Leave the operator's pending request alive while the other conversation runs.
        ops._pending[operator_turn.chat_key] = service._Pending(
            id="synthetic-confirmation", tool=service.CONFIRM, arguments={},
            block=SENTINELS["confirmation"], asked_in="earlier-message", at=ops._clock(), confirmation={},
            asker=operator_turn)
        chat = "oc_allowlist_group" if kind is SessionKind.GROUP else "oc_allowlist_other"
        resident = _Resident("sess-allowlist")
        session = rig.manager._router.session(SessionKey(kind, LARK.agent_id, chat))
        session.claude = resident
        denied: list[Any] = []
        async def calls(on_event: Any) -> None:
            turn = rig.manager._running_token.ops_turn
            binding = rig.manager._running_token.binding
            assert ops.has_confirmation(operator_turn.chat_key)
            assert SENTINELS["confirmation"] in ops._pending[operator_turn.chat_key].block
            assert await ops.current_listing(turn) == []
            assert await ops.current_listing(session_key=session.key) == []
            for tool in global_tools.values():
                answer = await ops.call(tool.name, _arguments(tool.schema), binding=binding,
                                        session_key=session.key)
                denied.append(answer)
                assert answer.get("status") == "error", (tool.name, answer)
                assert answer["error"]["code"] == "forbidden", (tool.name, answer)
        resident.on_turn = calls
        for index in range(2):
            await rig.reply(await _published(rig, _lark(f"hello {index}", chat, OTHER, group=kind is SessionKind.GROUP)))
        assert len(denied) == 2 * len(global_tools)
        assert len(resident.turns) == 2 and len(captures[session.key]) == 2
        complete = _encoded(captures[session.key]) + "\n".join(resident.turns) + _encoded(denied)
        for name, sentinel in SENTINELS.items():
            assert sentinel not in complete, name
        assert "[GROUP SESSION]" in resident.turns[0] if kind is SessionKind.GROUP else "[DIRECT CHAT SESSION]" in resident.turns[0]
        assert "Synthetic Manager" in resident.turns[0]
        assert "Looper state" in resident.turns[0]
        assert "Looper state: unchanged" in resident.turns[1]
        assert session.state_shown is not rig.manager._sessions["main"].state_shown

    _run(test, routed, tmp_path)
