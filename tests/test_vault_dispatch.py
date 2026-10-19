"""Vault tool dispatch checks without a database or a running server."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_planner_vault_dispatch_checks_turn_and_live_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import vault
    from looper.agents.ops import vault as tools
    from looper.agents.ops import planner

    async def run() -> None:
        config = vault.VaultConfig(tmp_path, ("codex-mem",), None)
        access = AsyncMock(return_value=(config, ""))
        monkeypatch.setattr(vault, "for_task", access)
        monkeypatch.setattr(planner, "_roles_on", AsyncMock(return_value=False))
        perform = AsyncMock(return_value={"bytes": 1})
        refusal = AsyncMock()
        monkeypatch.setattr(tools, "run_tool", perform)
        monkeypatch.setattr(tools, "note_refusal", refusal)
        ops = planner.PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9")
        token = ops.issue("task-1")
        header = f"Bearer {token}"
        assert tools.TOOL_NAMES <= {t["name"] for t in await ops.listing(header)}
        args = {"path": "codex-mem/build.md", "content": "x", "reason": "keep it"}
        assert (await ops.call(header, tools.WRITE, args))["error"]["code"] == "needs_user_turn"
        perform.assert_not_awaited()
        refusal.assert_awaited_once()
        ops.begin_turn(token)
        assert (await ops.call(header, tools.WRITE, args))["status"] == "ok"
        assert perform.await_args.kwargs["who"] == "planner"
        assert perform.await_args.kwargs["task_id"] == "task-1"
        usage = perform.await_args.kwargs["usage"]
        assert (await ops.call(header, tools.READ, {"path": "codex-mem/build.md"}))["status"] == "ok"
        assert perform.await_args.kwargs["usage"] is usage
        access.return_value = (None, "this task's project has the knowledge vault off")
        assert (await ops.call(header, tools.READ, {"path": "codex-mem/build.md"}))["error"]["code"] == "forbidden"
        assert not tools.TOOL_NAMES & {t["name"] for t in await ops.listing(header)}

    asyncio.run(run())


def test_run_vault_dispatch_uses_issued_tools_and_live_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import vault
    from looper.agents.ops import room, vault as tools
    from looper.agents.room import MemberKind, Seat

    async def run() -> None:
        config = vault.VaultConfig(tmp_path, ("codex-mem",), None)
        access = AsyncMock(return_value=(config, ""))
        monkeypatch.setattr(vault, "for_task", access)
        perform, refusal = AsyncMock(return_value={"bytes": 1}), AsyncMock()
        monkeypatch.setattr(tools, "run_tool", perform)
        monkeypatch.setattr(tools, "note_refusal", refusal)
        seat = Seat("worker@s1", "worker", MemberKind.WORK, run="worker-1")
        ops = room.RoomOps(SimpleNamespace(), url="http://127.0.0.1:9")
        token = ops.issue("task-1", seat, room=False, vault=config)
        header = f"Bearer {token}"
        assert {t["name"] for t in ops.listing(header)} == tools.TOOL_NAMES
        args = {"path": "codex-mem/build.md", "content": "x", "reason": "keep it"}
        assert (await ops.call(header, tools.WRITE, args))["status"] == "ok"
        assert perform.await_args.kwargs["who"] == "worker@s1"
        assert perform.await_args.kwargs["worker"] == "worker-1"
        assert (await ops.call(header, room.POST, {"kind": "info", "body": "x"}))["error"]["code"] == "forbidden"
        access.return_value = (vault.VaultConfig(tmp_path, (), None), "")
        assert (await ops.call(header, tools.WRITE, args))["error"]["code"] == "forbidden"
        refusal.assert_awaited_once()
        readonly = ops.issue("task-1", seat, room=False, vault=vault.VaultConfig(tmp_path, (), None))
        access.return_value = (config, "")
        assert (await ops.call(f"Bearer {readonly}", tools.WRITE, args))["error"]["code"] == "forbidden"
        ops.revoke_run(seat.run)
        assert not ops.authorized(header)

    asyncio.run(run())


def test_concurrent_writes_of_one_token_cannot_exceed_its_byte_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import vault
    from looper.agents.ops import ErrorCode, OpError, vault as tools

    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def write(config, path, content, expected_sha256, reason, **kwargs):
            entered.set()
            await release.wait()
            return {"path": path, "bytes": len(content.encode("utf-8")), "commit": "a" * 40}

        monkeypatch.setattr(vault, "write", write)
        monkeypatch.setattr(tools, "_activity", AsyncMock())
        config = vault.VaultConfig(tmp_path, ("codex-mem",), None)
        usage = tools.VaultUsage(written_bytes=192 * 1024)

        async def call(path: str):
            return await tools.run_tool(config, tools.WRITE, {
                "path": path, "content": "x" * (64 * 1024), "reason": "keep note"},
                task_id="task-1", who="worker@s1", worker="worker-1", usage=usage)

        first = asyncio.create_task(call("codex-mem/first.md"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = asyncio.create_task(call("codex-mem/second.md"))
        await asyncio.sleep(0)  # both calls reached the quota boundary before the first write returns
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=2)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, OpError) and result.code is ErrorCode.RATE_LIMITED for result in results) == 1
        assert usage.written_bytes == vault.RUN_WRITE_MAX_BYTES
        tools._activity.assert_awaited_once()

    asyncio.run(run())


def test_expected_sha256_schema_explicitly_accepts_null_for_a_new_note() -> None:
    from looper.agents.ops import check_arguments, vault as tools

    write = next(tool for tool in tools.TOOLS if tool.name == tools.WRITE)
    assert set(write.schema["properties"]["expected_sha256"]["type"]) == {"string", "null"}
    arguments = {"path": "codex-mem/new.md", "content": "new note", "reason": "keep note",
                 "expected_sha256": None}
    assert check_arguments(write.schema, arguments) == arguments


@pytest.mark.parametrize("value", [42, True, []], ids=["integer", "boolean", "array"])
def test_nullable_expected_sha256_still_refuses_non_strings(value: object) -> None:
    from looper.agents.ops import ErrorCode, OpError, check_arguments, vault as tools

    write = next(tool for tool in tools.TOOLS if tool.name == tools.WRITE)
    with pytest.raises(OpError) as caught:
        check_arguments(write.schema, {"path": "codex-mem/new.md", "content": "note", "reason": "keep note",
                                       "expected_sha256": value})
    assert caught.value.code is ErrorCode.INVALID


def test_unchanged_note_neither_uses_the_tokens_budget_nor_records_a_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import vault
    from looper.agents.ops import vault as tools

    async def run() -> None:
        result = {"path": "codex-mem/build.md", "bytes": 4, "commit": None, "unchanged": True}
        monkeypatch.setattr(vault, "write", AsyncMock(return_value=result))
        activity = AsyncMock()
        monkeypatch.setattr(tools, "_activity", activity)
        usage = tools.VaultUsage(written_bytes=1024)
        returned = await tools.run_tool(vault.VaultConfig(tmp_path, ("codex-mem",), None), tools.WRITE,
                                        {"path": result["path"], "content": "note", "reason": "keep note"},
                                        task_id="task-1", who="planner", worker="planner-task-1", usage=usage)
        assert returned == result
        assert usage.written_bytes == 1024
        activity.assert_not_awaited()

    asyncio.run(run())


def test_activity_failure_does_not_undo_a_real_write_or_its_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import vault
    from looper.agents.ops import vault as tools
    from looper.db import activity

    async def run() -> None:
        (tmp_path / "codex-mem").mkdir()
        monkeypatch.setattr(vault, "_git_root", AsyncMock(return_value=None))
        record = AsyncMock(side_effect=RuntimeError("activity is unavailable"))
        monkeypatch.setattr(activity, "insert_activity", record)
        usage = tools.VaultUsage()
        content = "Build with make gen."
        result = await tools.run_tool(vault.VaultConfig(tmp_path, ("codex-mem",), None), tools.WRITE,
                                      {"path": "codex-mem/build.md", "content": content, "reason": "keep note"},
                                      task_id="task-1", who="planner", worker="planner-task-1", usage=usage)
        assert (tmp_path / "codex-mem/build.md").read_text() == content
        assert result["bytes"] == len(content.encode("utf-8"))
        assert usage.written_bytes == result["bytes"]
        record.assert_awaited_once()
        assert record.await_args.kwargs["summary"].startswith("Vault: planner wrote codex-mem/build.md")

    asyncio.run(run())


def test_cancelling_a_write_waits_for_its_budget_and_activity_before_returning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import vault
    from looper.agents.ops import vault as tools

    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        finished = False

        async def write(config, path, content, expected_sha256, reason, **kwargs):
            nonlocal finished
            entered.set()
            await release.wait()
            finished = True
            return {"path": path, "bytes": len(content.encode("utf-8")), "commit": "a" * 40}

        monkeypatch.setattr(vault, "write", write)
        activity = AsyncMock()
        monkeypatch.setattr(tools, "_activity", activity)
        usage = tools.VaultUsage()
        pending = asyncio.create_task(tools.run_tool(
            vault.VaultConfig(tmp_path, ("codex-mem",), None), tools.WRITE,
            {"path": "codex-mem/build.md", "content": "note", "reason": "keep note"},
            task_id="task-1", who="planner", worker="planner-task-1", usage=usage))
        await asyncio.wait_for(entered.wait(), timeout=2)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=2)
        assert finished
        assert usage.written_bytes == 4
        activity.assert_awaited_once_with(
            "task-1", "planner-task-1", "Vault: planner wrote codex-mem/build.md (commit aaaaaaaaaaaa): keep note")

    asyncio.run(run())
