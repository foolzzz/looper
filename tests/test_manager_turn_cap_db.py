"""The configured per-turn cap applies to subscription runs; off preserves the old recording path."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from looper.agents import chat_settings, session_budget, turn_metrics
from looper.agents.claude_cli import ClaudeRunRecorder
from looper.agents.runtimes.pricing import ModelPrice
from looper.agents.tiers import manager_budget
from tests.db_support import DSN, run_db
from tests.manager_budget_support import manager

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def off_fingerprint(metadata, root, input_text):
    command = [part.replace(str(root.resolve()), "<root>") for part in metadata["command"]]
    settings = command.index("--settings") + 1
    isolation = json.loads(command[settings])
    isolation["claudeMdExcludes"] = ["<host-instruction-paths>"]
    command[settings] = json.dumps(isolation, sort_keys=True)
    return hashlib.sha256(json.dumps({"command": command, "prompt": metadata["system_prompt_sha256"],
                                     "input": input_text}, sort_keys=True).encode()).hexdigest()


def test_subscription_turn_cap_replies_and_resumes(monkeypatch, tmp_path):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 0.01})
        async with manager(pool, tmp_path, monkeypatch, context=20_000) as chat:
            assert await chat.ask() == [session_budget.over_cap_reply(0.01)]
            assert not chat.manager._claude.started
            saved = chat.manager._session_now().session_id
            await session_budget.update({"turn_budget_usd": 4})
            assert await chat.ask("again") == ["answer"]
            assert chat.manager._session_now().session_id == saved
            assert "--resume" in chat.manager._claude.command
    run_db(check, monkeypatch, timeout=30)


def test_no_catalog_price_stops_at_the_soft_cap_and_warns_once(monkeypatch, tmp_path, caplog):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 0.01})
        manager_budget._no_price.cache_clear()
        monkeypatch.setattr(manager_budget, "pricer", lambda *a, **k: type("UnknownPrice", (), {"model_price": lambda self, _: ModelPrice(), "__call__": lambda self, model, usage: None})())
        async with manager(pool, tmp_path, monkeypatch, context=1000) as chat:
            assert await chat.ask() == [session_budget.over_cap_reply(0.01)]
            assert chat.manager._session_now().budget.spend.budget_unverifiable
            assert await chat.ask("again") == [session_budget.over_cap_reply(0.01)]
        assert sum("no price;" in record.message for record in caplog.records) == 1
    run_db(check, monkeypatch, timeout=30)


def test_native_budget_error_uses_the_same_fixed_reply(monkeypatch, tmp_path):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 4})
        async with manager(pool, tmp_path, monkeypatch, context=100, budget_error=True) as chat:
            assert await chat.ask() == [session_budget.over_cap_reply(4)]
            assert not chat.manager._claude.started
            assert await pool.fetchval("SELECT session_id FROM agent_sessions WHERE agent_id='manager'")
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("budget", [None, 4])
def test_native_cap_keeps_over_cap_metrics_with_budget_on_or_off(monkeypatch, tmp_path, budget):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
        await session_budget.update({"turn_budget_usd": budget})
        async with manager(pool, tmp_path, monkeypatch, context=100, budget_error=True) as chat:
            replies = await chat.ask()
            await asyncio.gather(*turn_metrics._pending)
            if budget is None:
                assert replies[0].startswith("Manager 出错，没能完成这次回复：")
            else:
                assert replies == [session_budget.over_cap_reply(budget)]
            metadata = json.loads(await pool.fetchval("SELECT metadata FROM llm_records ORDER BY id DESC LIMIT 1"))
            assert metadata["turn_metrics"]["delivery"] == "over_cap"
            assert not chat.manager._claude.started
            assert not chat.manager._turn_lock.locked()
    run_db(check, monkeypatch, timeout=30)


def test_reply_metrics_finish_before_budget_maintenance_releases_the_lock(monkeypatch, tmp_path):
    async def check(pool):
        monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
        await session_budget.update({"turn_budget_usd": 4})
        async with manager(pool, tmp_path, monkeypatch, context=100) as chat:
            writing, release, written = asyncio.Event(), asyncio.Event(), asyncio.Event()
            annotate = ClaudeRunRecorder.annotate
            bound = chat.manager._bound_session

            async def slow_metrics(recorder, metadata):
                if "turn_metrics" in metadata:
                    writing.set()
                    await release.wait()
                    assert chat.manager._turn_lock.locked()
                await annotate(recorder, metadata)
                if "turn_metrics" in metadata:
                    written.set()

            async def after_metrics(session):
                assert written.is_set()
                assert session.turn_lock.locked()
                await bound(session)

            monkeypatch.setattr(ClaudeRunRecorder, "annotate", slow_metrics)
            monkeypatch.setattr(chat.manager, "_bound_session", after_metrics)
            reply = asyncio.create_task(chat.ask())
            await writing.wait()
            try:
                assert chat.manager._turn_lock.locked()
                assert not reply.done()
            finally:
                release.set()
                await asyncio.gather(reply, *turn_metrics._pending)
            assert reply.result() == ["answer"]
            assert not chat.manager._turn_lock.locked()
            metadata = json.loads(await pool.fetchval("SELECT metadata FROM llm_records ORDER BY id DESC LIMIT 1"))
            assert metadata["turn_metrics"]["delivery"] == "reply"
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("compact,rotated", [("success", False), ("missing", True)])
def test_native_cap_compacts_before_stopping_the_process(monkeypatch, tmp_path, compact, rotated):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 4})
        async with manager(pool, tmp_path, monkeypatch, budget_error=True, compact=compact) as chat:
            assert await chat.ask() == [session_budget.over_cap_reply(4)]
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE input_text='/compact'") == 1
            assert (chat.manager._pending_switch is not None) is rotated
            assert not chat.manager._claude.started
    run_db(check, monkeypatch, timeout=30)


def test_enabling_the_budget_during_delivery_cannot_release_another_turns_lock(monkeypatch, tmp_path):
    async def check(pool):
        async with manager(pool, tmp_path, monkeypatch, context=1000) as chat:
            sending = [asyncio.Event(), asyncio.Event()]
            release = [asyncio.Event(), asyncio.Event()]
            send = chat.manager._send_reply
            count = 0

            async def slow_delivery(*args, **kwargs):
                nonlocal count
                index = count
                count += 1
                sending[index].set()
                await release[index].wait()
                await send(*args, **kwargs)

            monkeypatch.setattr(chat.manager, "_send_reply", slow_delivery)
            first = asyncio.create_task(chat.ask())
            await sending[0].wait()
            await session_budget.update({"turn_budget_usd": 4})
            second = asyncio.create_task(chat.ask("again"))
            await sending[1].wait()
            release[0].set()
            assert await first == ["answer"]
            assert chat.manager._turn_lock.locked()  # second reply still owns it
            release[1].set()
            assert await second == ["answer"]
            assert not chat.manager._turn_lock.locked()
    run_db(check, monkeypatch, timeout=30)


def test_off_does_not_check_or_write_measurements(monkeypatch, tmp_path):
    async def check(pool):
        constructed = []
        initialize = ClaudeRunRecorder.__init__

        def record_init(recorder, **kwargs):
            initialize(recorder, **kwargs)
            constructed.append(recorder)

        monkeypatch.setattr(ClaudeRunRecorder, "__init__", record_init)
        async with manager(pool, tmp_path, monkeypatch) as chat:
            async def forbidden(*args, **kwargs):
                pytest.fail("off must never check bounds")
            monkeypatch.setattr(chat.manager, "_bound_session", forbidden, raising=False)
            assert await chat.ask() == ["answer"]
            assert chat.manager._claude.started and chat.manager._claude.command
            assert len(constructed) == 1 and constructed[0]._spend is None
            (row,) = await pool.fetch("SELECT metadata, context_tokens, cost_usd FROM llm_records")
            metadata = json.loads(row["metadata"])
            # Pinned on origin/main with the same stand-in; excludes host paths and variable timings.
            stable = {key: metadata[key] for key in ("model_choice", "profile", "pricing", "settings_sources")}
            assert hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest() == (
                "46d17c4049b0bfe6282b1aa947af6be957157270c928c555662b4863dd97d801")
            assert off_fingerprint(metadata, tmp_path, await pool.fetchval("SELECT input_text FROM llm_records")) == "891432884e0127938b8fa46d567139321ca6be83a2ca0afef0e29e8219b84df2"
            assert "session_bound" not in metadata
            assert tuple(await pool.fetchrow("SELECT context_tokens, last_turn_cost_usd FROM agent_sessions")) == (None, None)
    run_db(check, monkeypatch, timeout=30)


def test_hidden_delivery_followup_keeps_the_budget_reply_and_metrics(monkeypatch, tmp_path):
    from looper.agents.delivery_guard import FOLLOWUP_MESSAGE

    async def check(pool):
        monkeypatch.setattr(chat_settings, "_delivery_guard_enabled", True)
        monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
        await session_budget.update({"turn_budget_usd": 0.01})
        async with manager(pool, tmp_path, monkeypatch, context=100) as chat:
            script = tmp_path / "claude.py"
            body = script.read_text()
            body = body.replace("answer = 'handoff facts' if text.startswith('[HANDOFF]') else 'answer'",
                                f"answer = 'answer' if text == {FOLLOWUP_MESSAGE!r} else ''")
            body = body.replace("cost += 0.1", f"cost += 0.1 if text == {FOLLOWUP_MESSAGE!r} else 0")
            body = body.replace("over_cap = False and text != '/compact'",
                                f"over_cap = text == {FOLLOWUP_MESSAGE!r}")
            script.write_text(body)
            assert await chat.ask() == [session_budget.over_cap_reply(0.01)]
            await asyncio.gather(*turn_metrics._pending)
            records = await pool.fetch("SELECT input_text, metadata FROM llm_records ORDER BY id")
            assert len(records) == 2 and records[1]["input_text"] == FOLLOWUP_MESSAGE
            assert json.loads(records[1]["metadata"])["turn_metrics"]["delivery"] == "over_cap"
            assert not chat.manager._turn_lock.locked()

    run_db(check, monkeypatch, timeout=30)
