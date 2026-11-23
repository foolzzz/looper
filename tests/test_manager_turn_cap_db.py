"""The configured per-turn cap applies to subscription runs; off preserves the old recording path."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from looper.agents import session_budget
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


def test_no_catalog_price_has_no_soft_cap_and_warns_once(monkeypatch, tmp_path, caplog):
    async def check(pool):
        await session_budget.update({"turn_budget_usd": 0.01})
        manager_budget._no_price.cache_clear()
        monkeypatch.setattr(manager_budget, "pricer", lambda *a, **k: SimpleNamespace(model_price=lambda _: ModelPrice()))
        async with manager(pool, tmp_path, monkeypatch, context=1000) as chat:
            assert await chat.ask() == ["answer"]
            assert chat.manager._session_now().budget.spend is None
            assert await chat.ask("again") == ["answer"]
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
