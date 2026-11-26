"""Resident Claude compactions restore only their own session's next eligible turn."""

import json
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, session_budget
from looper.db import chat as db_chat
from tests.db_support import DSN, run_db
from tests.manager_budget_support import manager

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _enabled(pool, value="true"):
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ('compaction_restore.enabled', $1) "
                       "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", value)
    await chat_settings.load()


async def _records(pool):
    return [{**dict(row), "metadata": json.loads(row["metadata"])}
            for row in await pool.fetch("SELECT * FROM llm_records WHERE agent_id='manager' ORDER BY id")]


def _inputs(root):
    return [json.loads(line) for line in (root / "inputs.jsonl").read_text().splitlines()]


def test_disabled_turns_do_not_read_the_restore_setting(monkeypatch, tmp_path):
    from looper.agents import compaction_restore as restore
    reader = AsyncMock(side_effect=RuntimeError("turns must use the settings cache"))

    async def check(pool):
        await chat_settings.load()
        monkeypatch.setattr(restore, "enabled", reader)
        async with manager(pool, tmp_path, monkeypatch, context=100) as chat:
            await chat.ask()
            await chat.ask("continue")
        assert len(_inputs(tmp_path)) == 2
        reader.assert_not_awaited()

    run_db(check, monkeypatch, timeout=45)


@pytest.mark.parametrize("stable", [False, True])
def test_budget_compaction_restores_once_before_current_state(monkeypatch, tmp_path, stable):
    async def check(pool):
        from looper.agents import compaction_restore as restore

        await _enabled(pool)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('manager.stable_context', $1)",
                           str(stable).lower())
        await session_budget.update({"turn_budget_usd": 4})
        await db_chat.save_message(session_id="budget-chat", message_id="earlier", role="user",
                                   content="SYNTHETIC_EARLIER_CHAT")
        async with manager(pool, tmp_path, monkeypatch) as chat:
            await chat.ask()
            # Keep later user turns below the bound: test one compaction, not repeated maintenance.
            await session_budget.update({"turn_budget_usd": None})
            await chat.ask("continue")
            await chat.ask("later")
        first, compact, restored, later = _inputs(tmp_path)
        assert compact == restore.command(restore.Role.MANAGER)
        assert restored.startswith("[COMPACTED]") and "SYNTHETIC_EARLIER_CHAT" in restored
        assert restored.endswith("continue")
        assert ("\n\nLooper state (rev " in restored) is stable
        assert "[COMPACTED]" not in later and "Looper state (rev " not in later
        rows = await _records(pool)
        assert rows[1]["metadata"]["compaction_restore"]["state"] == "due"
        sent = rows[2]["metadata"]["compaction_restore"]
        assert sent["state"] == "sent" and sent["due_record"] == rows[1]["id"]
        assert "compaction_restore" not in rows[3]["metadata"]
    run_db(check, monkeypatch, timeout=45)


@pytest.mark.parametrize("compact", ["raise", "missing"])
@pytest.mark.parametrize("typing", [False, True])
def test_failed_budget_compaction_preserves_failure_classification_and_rotates(monkeypatch, tmp_path, compact, typing):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 4})
        if typing:
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ('engine.failure_typing', 'record')")
        outcomes = []
        for enabled in [False, True]:
            await _enabled(pool, str(enabled).lower())
            root = tmp_path / str(enabled)
            root.mkdir()
            async with manager(pool, root, monkeypatch, compact=compact) as chat:
                await chat.ask()
                assert chat.manager._pending_switch is not None
                await session_budget.update({"turn_budget_usd": None})
                await chat.ask("continue")
            rows = await _records(pool)
            maintenance = [row for row in rows if row["input_text"].startswith("/compact")][-1]
            from looper.agents import compaction_restore as restore
            assert maintenance["input_text"] == (restore.command(restore.Role.MANAGER) if enabled else "/compact")
            outcomes.append((maintenance["failure_class"], maintenance["failure_cause"]))
            assert (maintenance["failure_class"] is not None) is typing
            assert all("compaction_restore" not in row["metadata"] for row in rows)
            assert any(text.startswith("[RESUME]") and "[COMPACTED]" not in text for text in _inputs(root))
            await pool.execute("DELETE FROM agent_sessions WHERE agent_id='manager'")
            await session_budget.update({"turn_budget_usd": 4})
        assert outcomes[0] == outcomes[1]
    run_db(check, monkeypatch, timeout=60)


def test_restart_reads_pending_snapshot_from_saved_session(monkeypatch, tmp_path):
    async def check(pool):
        await _enabled(pool)
        async with manager(pool, tmp_path, monkeypatch, context=100, compact_on=(1,)) as chat:
            await chat.ask("before restart")
        async with manager(pool, tmp_path, monkeypatch, context=100) as chat:
            await chat.ask("after restart")
            await chat.ask("later")
        before, after, later = _inputs(tmp_path)
        assert "[COMPACTED]" not in before
        assert after.startswith("[COMPACTED]") and "auto" in after
        assert "[COMPACTED]" not in later
        rows = await _records(pool)
        assert rows[1]["metadata"]["compaction_restore"]["due_record"] == rows[0]["id"]
        assert rows[1]["metadata"]["compaction_restore"]["state"] == "sent"
    run_db(check, monkeypatch, timeout=45)


def test_failed_snapshot_is_offered_again_until_success(monkeypatch, tmp_path):
    async def check(pool):
        await _enabled(pool)
        async with manager(pool, tmp_path, monkeypatch, context=100, compact_on=(1,), error_on=(2,)) as chat:
            for text in ("first", "fails", "retries", "later"):
                await chat.ask(text)
        first, failed, retry, later = _inputs(tmp_path)
        assert failed.startswith("[COMPACTED]") and retry.startswith("[COMPACTED]")
        assert "[COMPACTED]" not in later
        rows = await _records(pool)
        assert [row["metadata"].get("compaction_restore", {}).get("state") for row in rows] == [
            "due", "offered", "sent", None]
    run_db(check, monkeypatch, timeout=45)


def test_hidden_delivery_followup_leaves_snapshot_for_next_user(monkeypatch, tmp_path):
    async def check(pool):
        await _enabled(pool)
        monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", True)
        async with manager(pool, tmp_path, monkeypatch, context=100, compact_on=(1,), empty_on=(1,)) as chat:
            await chat.ask("first")
            await chat.ask("next user")
        first, hidden, user = _inputs(tmp_path)
        assert "[COMPACTED]" not in hidden and user.startswith("[COMPACTED]")
        rows = await _records(pool)
        assert rows[1]["metadata"]["delivery_guard"]["followup"] == 1
        assert "compaction_restore" not in rows[1]["metadata"]
        assert rows[2]["metadata"]["compaction_restore"]["state"] == "sent"
    run_db(check, monkeypatch, timeout=45)


def test_completion_boundary_arms_but_never_carries_snapshot(monkeypatch, tmp_path):
    from tests.test_completion_turn_db import _task, _end_and_tell, _goal_status

    async def check(pool):
        await _enabled(pool)
        monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
        async with manager(pool, tmp_path, monkeypatch, context=100, compact_on=(1, 2)) as chat:
            await chat.ask("first")
            await _task(pool, tmp_path, origin_thread="budget-chat")
            await _end_and_tell(chat, _goal_status(thread="budget-chat"))
            await chat.ask("next user")
        first, completion, user = _inputs(tmp_path)
        assert "[COMPLETION]" in completion and "[COMPACTED]" not in completion
        assert user.startswith("[COMPACTED]")
        rows = await _records(pool)
        assert rows[1]["metadata"]["compaction_restore"]["state"] == "due"
        assert rows[2]["metadata"]["compaction_restore"]["due_record"] == rows[1]["id"]
    run_db(check, monkeypatch, timeout=45)


def test_scenario_chat_snapshot_reads_only_its_own_history(monkeypatch, tmp_path):
    from tests.test_manager_session_routing_db import GROUP_A, GROUP_B, OTHER, _Resident, _lark, _published
    from tests.test_scenario_session_routing_db import MARKERS, _scenario, _to

    async def check(pool):
        await _enabled(pool)
        rig = await _scenario(pool, tmp_path, "per_chat")
        await rig.start()
        a, b = rig.group(GROUP_A, _Resident("scenario-a")), rig.group(GROUP_B, _Resident("scenario-b"))
        async def boundary(on_event):
            await on_event({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto"}})
        a.on_turn = boundary
        try:
            for chat in (GROUP_A, GROUP_B, GROUP_A):
                await rig.reply(await _published(rig, _to(rig, _lark("continue", chat, OTHER, group=True))))
                a.on_turn = None
            assert "[COMPACTED]" not in b.turns[-1]
            assert a.turns[-1].startswith("[COMPACTED]") and "[GROUP SESSION]" in a.turns[-1]
            assert MARKERS[GROUP_A] in a.turns[-1] and MARKERS[GROUP_B] not in a.turns[-1]
        finally:
            await rig.close()
    run_db(check, monkeypatch, timeout=90)


def test_default_routed_manager_neither_keeps_nor_restores(monkeypatch, tmp_path):
    from looper.agents import session_routing
    from looper.agents.session_routing import SessionKey, SessionKind
    from tests.test_manager_session_routing_db import GROUP_A, LARK, OTHER, _lark

    async def check(pool):
        await _enabled(pool)
        await session_budget.update({"turn_budget_usd": 4})
        monkeypatch.setattr(session_routing, "_routing_enabled", True)
        async with manager(pool, tmp_path, monkeypatch, compact_on=(1,)) as chat:
            session = chat.manager._router.session(SessionKey(SessionKind.GROUP, LARK.agent_id, GROUP_A))
            token = chat.manager._current.set(session)
            try:
                for text in ("first", "next"):
                    await chat.manager._handle_user_message(_lark(text, GROUP_A, OTHER, group=True))
                inputs = _inputs(chat.manager._session_work_dir())
                assert "/compact" in inputs and all("[COMPACTED]" not in text for text in inputs)
                rows = await _records(pool)
                assert rows and all("compaction_restore" not in row["metadata"] for row in rows)
            finally:
                chat.manager._current.reset(token)
    run_db(check, monkeypatch, timeout=45)


def test_disabled_values_keep_model_inputs_and_launches_identical(monkeypatch, tmp_path):
    async def check(pool):
        from looper.db import llm_records
        calls = []
        async def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("disabled restore must not read")
        monkeypatch.setattr(llm_records, "latest_compaction_restore", forbidden, raising=False)
        captures = []
        for index, value in enumerate((None, "false", "yes")):
            if value is not None:
                await _enabled(pool, value)
            root = tmp_path / str(index)
            root.mkdir()
            await session_budget.update({"turn_budget_usd": 4})
            async with manager(pool, root, monkeypatch, compact_on=(1,)) as chat:
                await chat.ask("first")
                await session_budget.update({"turn_budget_usd": None})
                await chat.ask("next")
            rows = await _records(pool)
            current = [r for r in rows if r["metadata"]["cwd"] == str(root)]
            capture = []
            for row in current:
                meta = row["metadata"]
                assert "compaction_restore" not in meta
                capture.append((row["input_text"], [s.replace(str(root), "<root>") for s in meta["command"]],
                                meta["system_prompt_sha256"]))
            captures.append(capture)
            assert _inputs(root)[1] == "/compact"
            await pool.execute("DELETE FROM agent_sessions WHERE agent_id='manager'")
        assert captures[0] == captures[1] == captures[2]
        assert calls == []
    run_db(check, monkeypatch, timeout=90)


@pytest.mark.parametrize("scenario", ["user", "completion", "delivery"])
@pytest.mark.parametrize("compacted", [False, True])
def test_optional_restore_preserves_model_inputs_until_compaction(monkeypatch, tmp_path, scenario, compacted):
    from looper.agents.tiers.turn_token import TurnToken
    from tests.test_completion_turn_db import _task, _end_and_tell, _goal_status

    captures = []
    for value in ((None, "false", "yes") if compacted else (None, "false", "yes", "true")):
        async def check(pool):
            if value is not None:
                await _enabled(pool, value)
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ('manager.stable_context', 'true'), "
                               "('manager.research_order', 'true')")
            monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", scenario == "delivery")
            monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
            resume_steps = []
            step = TurnToken.step
            async def observe_step(token, name, work, *args, **kwargs):
                if name == "resume_brief":
                    resume_steps.append(name)
                return await step(token, name, work, *args, **kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(TurnToken, "step", observe_step)
                latest_reads = []
                async def forbidden_read(*args, **kwargs):
                    latest_reads.append(True)
                    raise RuntimeError("disabled restore must not read")
                if value != "true":
                    from looper.db import llm_records
                    patch.setattr(llm_records, "latest_compaction_restore", forbidden_read)
                async with manager(pool, tmp_path, monkeypatch, context=100,
                                   empty_on=(1,) if scenario == "delivery" else (),
                                   compact_on=((1, 2) if scenario == "completion" else (1,)) if compacted else ()) as chat:
                    await chat.ask("first")
                    if scenario == "completion":
                        await _task(pool, tmp_path, origin_thread="budget-chat")
                        await _end_and_tell(chat, _goal_status(thread="budget-chat"))
                    await chat.ask("next")
            rows = await _records(pool)
            captures.append(([(r["input_text"], r["metadata"]["command"], r["metadata"]["system_prompt_sha256"])
                              for r in rows], _inputs(tmp_path)))
            assert all("compaction_restore" not in row["metadata"] for row in rows)
            # Only a first turn can ask for the existing new-session brief when restore is disabled.
            if value != "true":
                assert len(resume_steps) == 1
                assert latest_reads == []
            assert any(json.loads(row["compactions"]) for row in rows) is compacted
        if (tmp_path / "inputs.jsonl").exists():
            (tmp_path / "inputs.jsonl").unlink()
        run_db(check, monkeypatch, timeout=60)
    assert all(capture == captures[0] for capture in captures[1:])


@pytest.mark.parametrize("scope_name", ["OMIT", "PERSONA_ONLY"])
def test_isolated_snapshot_does_not_read_conversations(monkeypatch, tmp_path, scope_name):
    from types import SimpleNamespace
    from looper.agents import compaction_restore, manager_persona_context
    from looper.agents.tiers.manager import RunState

    async def check(pool):
        await _enabled(pool)
        async with manager(pool, tmp_path, monkeypatch, context=100, compact_on=(1,)) as chat:
            await chat.ask("first")
            reads = []
            async def conversation(*args, **kwargs):
                reads.append(True)
                return ["SYNTHETIC_PRIVATE_CHAT"]
            monkeypatch.setattr(chat.manager, "_recent_conversations", conversation)
            prepared = SimpleNamespace(session=chat.manager._session_now(), message="next")
            run = RunState(persona_scope=getattr(manager_persona_context.Scope, scope_name))
            offer = await chat.manager._restore_offer(None, prepared, run)
            assert offer is not None and offer.text.startswith("[COMPACTED]")
            assert len(offer.text) <= compaction_restore.MANAGER_MAX_CHARS + len("\n\nThe user's new message follows.")
            assert reads == [] and "SYNTHETIC_PRIVATE_CHAT" not in offer.text
    run_db(check, monkeypatch, timeout=45)


def test_restore_read_failure_leaves_user_turn_and_pending_snapshot_available(monkeypatch, tmp_path):
    from looper.db import llm_records

    async def check(pool):
        await _enabled(pool)
        async with manager(pool, tmp_path, monkeypatch, context=100, compact_on=(1,)) as chat:
            await chat.ask("first")
            calls = []
            async def unavailable(*args, **kwargs):
                calls.append(True)
                raise RuntimeError("synthetic restore read failure")
            with monkeypatch.context() as patch:
                patch.setattr(llm_records, "latest_compaction_restore", unavailable)
                assert await chat.ask("read unavailable") == ["answer"]
            await chat.ask("read works")
        first, unavailable, restored = _inputs(tmp_path)
        assert calls == [True]
        assert "[COMPACTED]" not in unavailable and restored.startswith("[COMPACTED]")
        rows = await _records(pool)
        assert "compaction_restore" not in rows[1]["metadata"]
        assert rows[2]["metadata"]["compaction_restore"]["due_record"] == rows[0]["id"]
    run_db(check, monkeypatch, timeout=45)
