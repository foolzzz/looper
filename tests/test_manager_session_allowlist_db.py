"""Fail-closed gate for every operator data source offered to routed Manager sessions."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from pathlib import Path
from typing import Any

import pytest

from looper.agents import attention, lanes_settings, operator_profile, persona, session_visibility, vault
from looper.agents.chat_attachments import Attachment, AttachmentKind, AttachmentState
from looper.agents.ops import Risk, Tool, Turn
from looper.agents.ops import service
from looper.agents.ops.service import ManagerOps, Platform, scope_for
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers.manager import GoalSnapshot, ManagerAgent, channel_line
from looper.agents import api_v1, intent_rules, message_stream_parity
from looper.db import message_collection, message_parity, message_stream_alerts
from tests import subscription_support
from tests.db_support import DSN, run_db
from tests.lark_cli_stand_in import OPEN_ID, OTHER
from tests.test_manager_session_routing_db import (  # noqa: F401
    LARK, _lark, _published, _Resident, _run, _Rig, _web, routed,
)
from tests.test_scoped_routing_db import GROUP_A, MANAGER, OPERATOR, _message, _send, _setup
from tests.test_scenario_session_routing_db import GROUP_B, MARKERS, _to

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# Every new kind of operator data must add a different sentinel here and seed its actual source below.
SENTINELS = {name: f"PRIVATE_{name.upper()}_SENTINEL" for name in (
    "ready_task", "project", "work_dir", "profile", "memory_candidate", "project_lesson", "intent",
    "reminder", "ledger", "confirmation", "needs_you", "other_gate", "vault_note", "lead",
    "harness", "role_prompt", "custom_prompt", "template", "workflow", "goal", "persona_tone", "persona_notes",
    "intent_type", "intent_rule", "intent_rule_hit", "collection_config", "collected_message",
    "collection_parity", "collection_alert",
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


async def _seed_operator_extensions(pool: Any) -> None:
    """Exercise new operator sources without changing the compatibility baseline's inputs."""
    await pool.execute("UPDATE intent_type_versions SET guidance=$1 WHERE type_key='synthetic_type'",
                       SENTINELS["intent_type"])
    rule_id = "11111111-1111-4111-8111-111111111111"
    await pool.execute("INSERT INTO intent_rules(id,version,position) VALUES($1::text::uuid,1,1)", rule_id)
    await pool.execute("INSERT INTO intent_rule_versions(rule_id,version,enabled,position,intent_type,scope,"
                       "conditions,action,edited_by) VALUES($1::text::uuid,1,true,1,'any','{\"kind\":\"all\"}',"
                       "$2::jsonb,'push_for_confirmation','synthetic')", rule_id,
                       _encoded([{"kind": "sender_in_allowlist", "sender_ids": [SENTINELS["intent_rule"]]}]))
    hit_id = await pool.fetchval("INSERT INTO intent_rule_hits(evaluation_key,intent_id,generation,rule_id,"
                                "rule_version,selection,action,mr_identity) SELECT 'synthetic-evaluation',id,0,"
                                "$1::text::uuid,1,'matched','push_for_confirmation',$2::jsonb FROM intents "
                                "WHERE source_id='private-source' RETURNING id", rule_id, _encoded({
                                    "system_id": "synthetic", "repository_id": SENTINELS["intent_rule_hit"],
                                    "mr_id": "1", "revision": "synthetic",
                                    "canonical_url": "https://review.example.invalid/changes/1"}))
    await pool.execute("UPDATE event_sources SET lark_profile=$1 WHERE id='private-source'",
                       SENTINELS["collection_config"])
    await pool.execute("INSERT INTO source_collection_settings(source_id) VALUES('private-source')")
    now = datetime.now(timezone.utc)
    seq = await subscription_support.add(pool, SENTINELS["collected_message"], source_id="private-source",
                                         chat_id="oc_synthetic_collected", sent_at=now - timedelta(minutes=1))
    await pool.execute("UPDATE source_events SET body=jsonb_build_object('text',text) WHERE seq=$1", seq)
    async with pool.acquire() as conn:
        parity_id = await message_parity.create_session(conn, "private-source", 0, now - timedelta(hours=1), now, 0,
                                                        policy_digest="synthetic", reference_digest="synthetic")
        await message_parity.put_members(conn, parity_id, "collect", [{
            "external_id": SENTINELS["collection_parity"], "chat_id": "oc_synthetic_collected",
            "sent_at": now - timedelta(minutes=1),
        }])
        alert = await message_stream_alerts.open_episode(conn, "private-source", "collector_failure",
                                                         target_chat=SENTINELS["collection_alert"])
    # Positive readbacks prove each sentinel is present in a real source before testing its exclusion.
    sources = {
        "intent_type": await pool.fetchval("SELECT guidance FROM intent_type_versions WHERE type_key='synthetic_type'"),
        "intent_rule": await intent_rules.list_rules(principal=api_v1.OPERATOR, db=pool),
        "intent_rule_hit": await intent_rules.get_hit(hit_id, principal=api_v1.OPERATOR, db=pool),
        "collection_config": await message_collection.collector_source("private-source", db=pool),
        "collected_message": dict(await pool.fetchrow("SELECT text,body FROM source_events WHERE seq=$1", seq)),
        "collection_parity": await message_stream_parity.read_report("private-source", parity_id, db=pool),
        "collection_alert": alert,
    }
    for name, source in sources.items():
        assert SENTINELS[name] in _encoded(source), (name, source)


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
        await _seed_operator_extensions(pool)
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
        denied: list[tuple[str, Any]] = []
        turn_checks: list[dict[str, Any]] = []
        async def calls(on_event: Any) -> None:
            turn = rig.manager._running_token.ops_turn
            binding = rig.manager._running_token.binding
            turn_checks.append({
                "has_confirmation": ops.has_confirmation(operator_turn.chat_key),
                "confirmation": ops._pending[operator_turn.chat_key].block,
                "turn_listing": await ops.current_listing(turn),
                "session_listing": await ops.current_listing(session_key=session.key),
            })
            for tool in global_tools.values():
                answer = await ops.call(tool.name, _arguments(tool.schema), binding=binding,
                                        session_key=session.key)
                denied.append((tool.name, answer))
        resident.on_turn = calls
        for index in range(2):
            await rig.reply(await _published(rig, _lark(f"hello {index}", chat, OTHER, group=kind is SessionKind.GROUP)))
        assert len(turn_checks) == 2
        for checks in turn_checks:
            assert checks["has_confirmation"]
            assert SENTINELS["confirmation"] in checks["confirmation"]
            assert checks["turn_listing"] == []
            assert checks["session_listing"] == []
        assert len(denied) == 2 * len(global_tools)
        for name, answer in denied:
            assert answer.get("status") == "error", (name, answer)
            assert answer["error"]["code"] == "forbidden", (name, answer)
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


async def _scoped_inputs(pool: Any, root: Path, monkeypatch: Any, *, default: str | None = None,
                         seed: bool = False) -> tuple[Any, list[Any], dict[str, list[dict[str, Any]]]]:
    """Reuse the routed boundary's sources and the scoped bus fixture, capturing the actual prepared inputs."""
    if seed:
        await _seed(pool, root)
    rig, residents = await _setup(pool, root, monkeypatch, default=default)
    if seed:
        await pool.execute("UPDATE tasks SET manager_id=$1 WHERE id='private-task'", MANAGER)
        await pool.execute("UPDATE manager_task_templates SET manager_id=$1 WHERE key='synthetic-template'", MANAGER)
        await pool.execute("UPDATE managers SET system_prompt=$1 WHERE id=$2", SENTINELS["role_prompt"], MANAGER)
        rig.manager._goal_snapshots["synthetic-goal"] = GoalSnapshot("synthetic-goal", title=SENTINELS["goal"])
    ops = ManagerOps(Platform(bus=None, task_router=SimpleNamespace(has_planner=lambda _: False), manager=rig.manager),
                     url="http://127.0.0.1:9/", token="synthetic", scope=scope_for(rig.manager._definition))
    ops._working_at = ops._clock()
    rig.manager.ops = ops
    captures: dict[str, list[dict[str, Any]]] = {}
    prepare = ManagerAgent._prepare_turn

    async def capture(self: Any, *args: Any, **kwargs: Any) -> Any:
        prepared = await prepare(self, *args, **kwargs)
        if prepared.signature is not None:
            captures.setdefault(self._session_now().key, []).append({
                "system_prompt": prepared.system_prompt.text, "layers": prepared.system_prompt.layers,
                "sections": await self._sections(prepared.envelope), "turn": prepared.turn_text,
                "state": prepared.state, "tools": await self.ops.current_listing(prepared.turn),
                "prepared": prepared,
            })
        return prepared

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", capture)
    return rig, residents, captures


def test_operator_group_session_input_gate(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents, captures = await _scoped_inputs(pool, tmp_path, monkeypatch, seed=True)
        asker = Turn(channel="lark", channel_id=LARK.agent_id, chat=MAIN_CHAT,
                     message_id="synthetic-pending", sender=OPERATOR)
        rig.manager.ops._pending[asker.chat_key] = service._Pending(
            id="synthetic-pending", tool=service.CONFIRM, arguments={}, block=SENTINELS["confirmation"],
            asked_in="earlier-message", at=rig.manager.ops._clock(), confirmation={}, asker=asker)
        await rig.start()
        try:
            await rig.reply(await _published(rig, _to(rig, _web("main positive control"))))
            main_input = _encoded(captures["main"]) + "\n".join(rig.main.turns)
            for name in ("ready_task", "other_gate", "goal", "project", "template", "workflow"):
                assert SENTINELS[name] in main_input, name
            assert SENTINELS["profile"] in await operator_profile.resident_text()
            assert SENTINELS["ledger"] in _encoded(await attention.collect())
            assert MARKERS[GROUP_B] in await pool.fetchval(
                "SELECT content FROM chat_messages WHERE message_id=$1", f"seed-{GROUP_B}")
            await _send(rig, "group operator input", OPERATOR)
            session = next(s for s in rig.manager._sessions.values() if s.scope is not None)
            prepared = captures[session.key][0]
            complete = _encoded({key: value for key, value in prepared.items() if key != "prepared"}) + residents[0].turns[0]
            for name in ("ready_task", "other_gate", "goal", "profile", "ledger", "confirmation"):
                assert SENTINELS[name] not in complete, name
            assert MARKERS[GROUP_B] not in complete
            assert MARKERS[GROUP_A] in residents[0].turns[0]
            for name in ("project", "template", "workflow", "harness", "custom_prompt", "role_prompt"):
                assert SENTINELS[name] in complete, name
            assert session_visibility.OPERATOR_GROUP_CONTRACT in prepared["system_prompt"]
            assert prepared["state"] and session.state_shown is not rig.manager._sessions["main"].state_shown
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_chat_only_shared_input(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents, captures = await _scoped_inputs(pool, tmp_path, monkeypatch, seed=True)
        await pool.execute("UPDATE system_settings SET value='false' WHERE key='manager.stable_context'")
        await rig.start()
        try:
            await _send(rig, "shared message")
            session = next(s for s in rig.manager._sessions.values() if s.scope is not None)
            prepared = captures[session.key][0]
            assert prepared["system_prompt"] == (
                session_visibility.PUBLIC_PROMPT + "\n\n" + SENTINELS["role_prompt"] + "\n\n" +
                session_visibility.SHARED_SPEAKERS_NOTE)
            assert prepared["layers"]["contract"]["name"] == "CHAT_SESSION_ALLOWLIST+SHARED_SPEAKERS"
            complete = _encoded({key: value for key, value in prepared.items() if key != "prepared"}) + residents[0].turns[0]
            for name, sentinel in SENTINELS.items():
                if name != "role_prompt":
                    assert sentinel not in complete, name
            assert "TOOLS_CONTRACT" not in _encoded(prepared["layers"])
            assert prepared["tools"] == [] and prepared["sections"] == []
            assert MARKERS[GROUP_A] in residents[0].turns[0]
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_legacy_shared_input_is_1a_plus_note(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents, captures = await _scoped_inputs(pool, tmp_path, monkeypatch, default="legacy", seed=True)
        await pool.execute("UPDATE system_settings SET value='false' WHERE key='manager.stable_context'")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES('untrusted_fence.enabled','true')")
        await rig.start()
        try:
            message = _message(rig, "legacy comparison")
            session = await rig.manager._router.target(message)
            await rig.manager._load_session_state(session)
            rig.manager._current.set(session)
            scope, session.scope = session.scope, None
            try:
                settings = await rig.manager._load_agent_settings(fallback=False)
                baseline_sections = await rig.manager._sections(message)
                baseline_prompt = rig.manager._composed_prompt(settings)
                baseline_brief = await rig.manager._resume_brief(message, "legacy comparison")
                baseline_tools = await rig.manager.ops.current_listing(session_key=session.key)
            finally:
                session.scope = scope
            await rig.bus.publish(message)
            await rig.reply(message)
            prepared = captures[session.key][0]
            actual = prepared["prepared"]
            scope, session.scope = session.scope, None
            try:
                baseline_turn = await rig.manager._build_user_message(
                    actual.message, actual.note, channel=channel_line(message), envelope=message,
                    turn=actual.turn, image_input=actual.launch.runtime.image_input,
                    persona_scope=actual.run.persona_scope)
            finally:
                session.scope = scope
            assert prepared["system_prompt"] == baseline_prompt.text + "\n" + session_visibility.SHARED_SPEAKERS_NOTE
            assert prepared["layers"].keys() == baseline_prompt.layers.keys()
            for layer, value in baseline_prompt.layers.items():
                if layer != "contract":
                    assert prepared["layers"][layer] == value, layer
            assert prepared["layers"]["contract"]["name"] == baseline_prompt.layers["contract"]["name"] + "+SHARED_SPEAKERS"
            for field, expected in (("sections", baseline_sections), ("tools", baseline_tools)):
                assert len(prepared[field]) == len(expected), field
                for index, value in enumerate(expected):
                    assert prepared[field][index] == value, (field, index)
            assert prepared["state"] is None
            assert prepared["turn"] == baseline_turn
            assert residents[0].turns[0] == f"{baseline_brief}\n\n{baseline_turn}"
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_fence_always_on_in_scoped_sessions(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents, captures = await _scoped_inputs(pool, tmp_path, monkeypatch)
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key='untrusted_fence.enabled'") is None
        await rig.start()
        try:
            await _send(rig, "member words")
            await _send(rig, "operator words", OPERATOR)
            sessions = [s for s in rig.manager._sessions.values() if s.scope is not None]
            for session, resident in zip(sessions, residents):
                assert '<untrusted source="group_message" chat=' in resident.turns[0]
                assert MARKERS[GROUP_A] in resident.turns[0]
                turn = captures[session.key][0]["turn"]
                if session.scope.resolved.audience.value == "operator":
                    assert "operator words" in turn and '<untrusted source="group_message"' not in turn
                else:
                    assert '<untrusted source="group_message"' in turn
                    assert "\nmember words\n</untrusted>" in turn
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


@pytest.mark.parametrize("default", [None, "legacy"], ids=["chat-only", "legacy"])
def test_attachments_line_follows_mode(default: str | None, monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents, captures = await _scoped_inputs(pool, tmp_path, monkeypatch, default=default)
        directory = tmp_path / "chat-attachments" / "lark" / "synthetic-message"
        directory.mkdir(parents=True)
        path = directory / "synthetic.txt"
        path.write_text("synthetic attachment", encoding="utf-8")
        attachment = Attachment(1, AttachmentKind.FILE, AttachmentState.SAVED, path.name, "lark",
                                path=str(path), mime="text/plain", size=path.stat().st_size)
        message = _message(rig, "use the attachment")
        message = replace(message, payload={**message.payload, "attachments": [attachment.to_payload()]})
        await rig.start()
        try:
            await rig.bus.publish(message)
            await rig.reply(message)
            session = next(s for s in rig.manager._sessions.values() if s.scope is not None)
            prepared = captures[session.key][0]
            expected = ("This session cannot open attachments; if the message needs one, say so." if default is None else
                        "Open one with your file-reading tool when the message needs it.")
            assert prepared["turn"].endswith(expected)
            assert residents[0].turns[0].endswith(expected)
            assert '<untrusted source="attachment">' in prepared["turn"]
            assert str(path) in prepared["turn"]
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


@pytest.mark.parametrize("default", [None, "legacy"])
def test_scope_change_brief_has_no_handoff(default: str | None, monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, residents, _ = await _scoped_inputs(pool, tmp_path, monkeypatch, default=default)
        await rig.start()
        try:
            message = _message(rig, "fresh scope")
            session = await rig.manager._router.target(message)
            await rig.manager._load_session_state(session)
            # Seed a stale handoff on this new scope's actual row, not only the retired old row.
            session.pending_handoff = "SYNTHETIC_STALE_SCOPE_HANDOFF"
            await rig.manager._write_session_state(session=session)
            assert "SYNTHETIC_STALE_SCOPE_HANDOFF" == await pool.fetchval(
                "SELECT pending_handoff FROM manager_sessions WHERE manager_id=$1 AND session_key=$2",
                MANAGER, session.key)
            await rig.bus.publish(message)
            await rig.reply(message)
            assert "SYNTHETIC_STALE_SCOPE_HANDOFF" not in residents[0].turns[0]
            assert "Handoff from your previous session" not in residents[0].turns[0]
            assert MARKERS[GROUP_A] in residents[0].turns[0]
        finally:
            await rig.close()
    run_db(body, monkeypatch, timeout=90)


def test_inputs_version_moves_the_fingerprint(monkeypatch: Any, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig, _, _ = await _scoped_inputs(pool, tmp_path, monkeypatch)
        message = _message(rig, "operator version", OPERATOR)
        first = await rig.manager._router.target(message)
        original = session_visibility.OPERATOR_GROUP_INPUTS
        monkeypatch.setattr(session_visibility, "OPERATOR_GROUP_INPUTS", replace(original, version=original.version + 1))
        second = await rig.manager._router.target(message)
        assert first.scope.resolved == second.scope.resolved
        assert first.scope.mode == second.scope.mode
        assert first.key != second.key and first.scope.fingerprint != second.scope.fingerprint
    run_db(body, monkeypatch, timeout=90)
