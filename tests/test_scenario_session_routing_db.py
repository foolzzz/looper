"""Scenario routing isolates resident history without changing the Manager's capabilities."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from looper.agents import session_routing
from looper.agents.managers_api import ManagerDefinition
from looper.agents.message import channel_thread_id
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import DSN, run_db
from tests.test_manager_session_routing_db import GROUP_A, GROUP_B, LARK, OTHER, _Resident, _Rig, _lark, _published, _web

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
MANAGER = "manager-synthetic"
MARKERS = {GROUP_A: "SYNTHETIC_ALPHA_PRIVATE", GROUP_B: "SYNTHETIC_BETA_PRIVATE"}


async def _scenario(pool: Any, root: Path, mode: str) -> _Rig:
    row = dict(await pool.fetchrow(
        "INSERT INTO managers (id, name, system_prompt, tools, task_scope) "
        "VALUES ($1, 'Synthetic', 'Use the synthetic scenario contract.', 'all', 'own') RETURNING *", MANAGER))
    row["session_routing"] = mode
    await pool.execute("UPDATE managers SET session_routing = $1 WHERE id = $2", mode, MANAGER)
    await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name) VALUES ($1, 'lark', 'Synthetic')",
                       LARK.agent_id)
    rig = _Rig(root)
    rig.manager = ManagerAgent(config=ManagerConfig(work_dir=root), runtime_root=root,
                               definition=ManagerDefinition.from_row(row))
    rig.manager._sessions["main"].claude = rig.main
    for chat, marker in MARKERS.items():
        await pool.execute(
            "INSERT INTO manager_chat_bindings (channel_id, chat_id, manager_id) VALUES ($1, $2, $3)",
            LARK.agent_id, chat, MANAGER)
        thread = channel_thread_id(LARK.agent_id, chat, MANAGER, "manager")
        await pool.execute("INSERT INTO chat_threads (session_id, manager_id) VALUES ($1, $2)", thread, MANAGER)
        await pool.execute("INSERT INTO chat_messages (session_id, message_id, role, content, channel, created_at) "
                           "VALUES ($1, $2, 'user', $3, 'lark', '2026-11-01 10:00:00+00')",
                           thread, f"seed-{chat}", marker)
    return rig


def _to(rig: _Rig, message: Any) -> Any:
    from dataclasses import replace

    metadata = dict(message.metadata)
    if message.source == LARK:
        chat = message.payload["channel_context"]["chat_id"]
        metadata.update(channel_thread_id=channel_thread_id(LARK.agent_id, chat, MANAGER, "manager"),
                        channel_manager_id="manager")
    return replace(message, target=rig.manager.agent_address, metadata=metadata)


@pytest.mark.parametrize("mode", ["off", "per_chat"])
def test_groups_get_separate_histories_only_when_opted_in(mode: str, monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig = await _scenario(pool, tmp_path, mode)
        await rig.start()
        if mode == "per_chat":
            assert session_routing.group_isolation(MANAGER) is session_routing.GroupIsolation.ISOLATED
        a, b = rig.group(GROUP_A, _Resident("scenario-a")), rig.group(GROUP_B, _Resident("scenario-b"))
        try:
            for chat in MARKERS:
                await rig.reply(await _published(rig, _to(rig, _lark(MARKERS[chat] + "_NOW", chat, OTHER, group=True))))
            await rig.reply(await _published(rig, _to(rig, _lark("synthetic dm", "oc_synthetic_dm", OTHER, group=False))))
            await rig.reply(await _published(rig, _to(rig, _web("synthetic dashboard"))))
            if mode == "per_chat":
                assert a.turns and b.turns and len(rig.main.turns) == 2
                for own, other, resident in ((GROUP_A, GROUP_B, a), (GROUP_B, GROUP_A, b)):
                    request = "\n".join(resident.turns)
                    assert MARKERS[own] in request and MARKERS[other] not in request
                    assert f"- user: {MARKERS[own]}" in request  # positive control: stored history was actually read
                    assert request.startswith("[GROUP SESSION]")
                assert a.session_id != b.session_id
                processes = [session.process for session in rig.manager._sessions.values()]
                assert len({process.system_prompt.sha256 for process in processes}) == 1
                assert all(process.launch.access is None for process in processes)
            else:
                assert len(rig.main.turns) == 4 and not a.turns and not b.turns
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)


def test_scenario_router_keeps_non_groups_in_main_and_switch_off_returns_to_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def body(pool: Any) -> None:
        rig = await _scenario(pool, tmp_path, "per_chat")
        group = _to(rig, _lark("synthetic group", GROUP_A, OTHER, group=True))
        selected = await rig.manager._router.target(group)
        assert selected is not None and selected.key == f"group:{LARK.agent_id}:{GROUP_A}"
        # The global default-Manager switch must not enable other-sender DM routing here.
        monkeypatch.setattr(session_routing, "_routing_enabled", True)
        dm = _to(rig, _lark("synthetic dm", "oc_synthetic_dm", OTHER, group=False))
        rig.manager._router._operator_ids[LARK.agent_id] = (__import__("time").monotonic(), "ou_synthetic_operator")
        assert (await rig.manager._router.target(dm)).key == "main"
        await pool.execute("UPDATE managers SET session_routing = 'off' WHERE id = $1", MANAGER)
        await ManagerAgent._load_db_config(rig.manager)
        target = await rig.manager._router.target(group)
        assert target is None or target.key == "main"
        assert selected.key in rig.manager._sessions  # reclaimed later; no history migration
        assert not session_routing.routes(MANAGER)
        assert session_routing.routes("manager")  # still controlled independently

    run_db(body, monkeypatch, timeout=60)


def test_scenario_resident_cap_and_idle_reclaim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        monkeypatch.setattr(session_routing, "_max_resident", 1)
        rig = await _scenario(pool, tmp_path, "per_chat")
        a = rig.manager._router.session(session_routing.SessionKey(session_routing.SessionKind.GROUP, LARK.agent_id, GROUP_A))
        b = rig.manager._router.session(session_routing.SessionKey(session_routing.SessionKind.GROUP, LARK.agent_id, GROUP_B))
        a.claude, b.claude = _Resident("scenario-a"), _Resident("scenario-b")
        a.claude.started = True
        await rig.manager._router.admit(b, None)
        assert not a.claude.started  # only this Manager's residents count, including main when running
        b.claude.started = True
        await rig.manager._router.release(b)
        assert rig.manager._router.resident_count() == 1
        await pool.execute("UPDATE managers SET session_routing = 'off' WHERE id = $1", MANAGER)
        await rig.manager._load_db_config()
        await rig.manager._router.sweep()
        assert not b.claude.started and b.key in rig.manager._sessions

    run_db(body, monkeypatch, timeout=60)


def test_scenario_runtime_files_belong_to_the_manager_and_chat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dataclasses import replace

    from looper.agents.ops.service import ManagerOps, Platform, scope_for

    async def body(pool: Any) -> None:
        rig = await _scenario(pool, tmp_path, "per_chat")
        other = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path,
                             definition=replace(rig.manager._definition, id="manager-another"))
        roots, bindings = [], []
        for agent in (rig.manager, other, _Rig(tmp_path).manager):
            ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic",
                             scope=scope_for(agent._definition), turn_file=tmp_path / ".local" / f"{agent.agent_id}-turn.json")
            session = agent._router.session(session_routing.SessionKey(session_routing.SessionKind.GROUP, LARK.agent_id, GROUP_A))
            agent._current.set(session)
            roots.append(agent._session_root())
            bindings.append(ops.turn_file_for(session.key))
        assert len(set(roots)) == len(set(bindings)) == 3
        assert all(binding.is_relative_to(root) for root, binding in zip(roots, bindings))

    run_db(body, monkeypatch, timeout=60)


def test_scenario_sessions_keep_the_same_profile_prompt_and_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.ops import Turn
    from looper.agents.ops.service import ManagerOps, Platform, scope_for
    from looper.db import agent_sessions

    async def body(pool: Any) -> None:
        rig = await _scenario(pool, tmp_path, "per_chat")
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                           "SELECT 'synthetic-profile', 'Synthetic profile', runtime_id, model_id "
                           "FROM agent_profiles WHERE builtin ORDER BY id LIMIT 1")
        await agent_sessions.select_profile(MANAGER, "synthetic-profile")
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic",
                         scope=scope_for(rig.manager._definition))
        profiles, prompts, listings = [], [], []
        for chat in (None, GROUP_A, GROUP_B):
            session = (rig.manager._sessions["main"] if chat is None else rig.manager._router.session(
                session_routing.SessionKey(session_routing.SessionKind.GROUP, LARK.agent_id, chat)))
            rig.manager._current.set(session)
            settings = await rig.manager._load_agent_settings(fallback=False)
            profiles.append(settings.profile.id)
            prompts.append(rig.manager._composed_prompt(settings).text)
            listings.append(await ops.current_listing(Turn(channel="lark", chat=chat, group=chat is not None)))
        assert profiles == ["synthetic-profile"] * 3
        assert len(set(prompts)) == 1 and listings[0] == listings[1] == listings[2]

    run_db(body, monkeypatch, timeout=60)


def test_off_scenario_request_bytes_and_process_signature_match_main(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    async def body(pool: Any) -> None:
        rig = await _scenario(pool, tmp_path, "off")
        await rig.start()
        try:
            for chat in MARKERS:
                await rig.reply(await _published(rig, _to(rig, _lark(MARKERS[chat] + "_NOW", chat, OTHER, group=True))))
            await rig.reply(await _published(rig, _to(rig, _web("synthetic dashboard"))))
            prompt = rig.manager._process.system_prompt.text
            # Normalize machine-specific launch paths and the parent-directory exclusion setting only.
            command = list(rig.manager._process.signature[0])
            command[command.index("--settings") + 1] = "<settings>"
            signature = json.dumps([command, *rig.manager._process.signature[1:]])
            value = json.dumps([prompt, rig.main.turns, signature], ensure_ascii=False)
            value = value.replace(str(tmp_path), "<root>").replace(sys.executable, "<python>")
            digest = hashlib.sha256(value.encode()).hexdigest()
            # Captured with the routing, Manager, router and ops implementations from f53e6d62f.
            assert digest == "178539194688342bf2b99e1e2a6df884aaf24e0cc6cc7e83f3dc8e9f56c2d509", digest
            assert list(rig.manager._sessions) == ["main"] and len(rig.main.starts) == 1
        finally:
            await rig.close()

    run_db(body, monkeypatch, timeout=90)
