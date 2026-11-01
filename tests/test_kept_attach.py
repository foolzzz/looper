"""Reattach a detached byte stream without mixing replay and live events."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from looper.agents import keeper, proc_tree
from looper.agents.kept_run import AdoptFailure, AttachError, DetachKind, KeptRun, KeptStream
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.keeper.protocol import ErrorCode, KeeperError
from tests.runtime_model_helpers import builtin_launch
from tests.test_kept_run import eventually, exercise, opened


async def attach(directory):
    return await KeptRun.attach(directory, runtime=CLAUDE_CODE, launch=builtin_launch(),
                                epoch=2, on_stale=lambda reason: None, label="adopt test")


def stored_run(tmp_path: Path, events: list[dict], *, consumed: int | None = None) -> Path:
    directory = tmp_path / ".local" / "runs" / "run"
    directory.mkdir(parents=True)
    data = b"".join(json.dumps(event).encode() + b"\n" for event in events)
    keeper.write_json(directory / keeper.MANIFEST, {
        "manifest_version": 1, "command": ["unused"], "work_dir": str(tmp_path),
        "runtime_type": "claude_code", "run_id": "run",
    })
    keeper.write_json(directory / keeper.STATE, {
        "format": 1, "state": "exited", "exit_code": 7, "signal": None,
    })
    keeper.write_json(directory / keeper.DETACH, {
        "format": 1, "consumed": {"stdout": len(data) if consumed is None else consumed},
        "stdin_offset": 14,
    })
    (directory / keeper.STDOUT).write_bytes(data)
    return directory


def test_file_replay_finishes_before_live_events_are_readable(tmp_path):
    first, second = {"type": "system"}, {"type": "result", "result": "done"}
    offset = len(json.dumps(first).encode()) + 1
    directory = stored_run(tmp_path, [first, second], consumed=offset)

    async def run():
        handle = await attach(directory)
        try:
            assert not (directory / keeper.DETACH).exists()
            assert handle.replay_until == offset
            assert handle._process.stdin.is_closing()
            seen = [(event, handle.replaying) async for event in handle.channel.events()]
            assert seen == [(first, True), (second, False)]
            assert handle.replay_finished.is_set()
            assert await handle.wait() == 7
            await handle.end()
            assert await handle.wait() == 7
        finally:
            handle.release()
        assert not directory.exists()

    exercise(run())


def test_file_replay_yields_between_chunks(tmp_path, monkeypatch):
    directory = stored_run(tmp_path, [{"type": "system", "data": "x" * (keeper.CHUNK * 3)}])

    async def run():
        ticks = []
        original = KeptStream.feed

        def feed(stream, offset, data):
            original(stream, offset, data)
            asyncio.get_running_loop().call_soon(ticks.append, offset)

        monkeypatch.setattr(KeptStream, "feed", feed)
        handle = await attach(directory)
        try:
            assert len(ticks) == 4
            assert len([event async for event in handle.channel.events()]) == 1
            assert handle.replay_finished.is_set()
        finally:
            handle.release()

    exercise(run())


@pytest.mark.parametrize("damage, failure", [
    ("manifest", AdoptFailure.FILES), ("keeper", AdoptFailure.FILES),
    ("detach", AdoptFailure.FILES), ("offset", AdoptFailure.FILES),
    ("boundary", AdoptFailure.FILES), ("short", AdoptFailure.LOG_SHORT),
])
def test_invalid_files_refuse_attachment_without_removing_detach(tmp_path, damage, failure):
    directory = stored_run(tmp_path, [{"type": "system"}])
    if damage in ("manifest", "keeper", "detach"):
        (directory / f"{damage}.json").write_text("not json")
    elif damage == "short":
        (directory / keeper.STDOUT).write_bytes(b"")
    else:
        keeper.write_json(directory / keeper.DETACH, {
            "format": 1, "consumed": {"stdout": -1 if damage == "offset" else 1},
        })

    async def run():
        with pytest.raises(AttachError) as error:
            await attach(directory)
        assert error.value.failure is failure
        assert (directory / keeper.DETACH).exists()

    exercise(run())


def test_live_attach_replays_then_steers_at_the_keepers_stdin_offset(tmp_path):
    async def run():
        async with opened(tmp_path) as original:
            events = []

            async def collect_original():
                async for event in original.channel.events():
                    events.append(event)

            reader = asyncio.create_task(collect_original())
            adopted = None
            try:
                await original.channel.start("before detach")
                await eventually(lambda: len(events) == 2 and original._process.stdout.idle.is_set())
                outcome = await original.detach(epoch=1, timeout=.3)
                assert outcome.kind is DetachKind.DETACHED
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                before = (original.run_dir / keeper.STDIN).read_bytes()
                adopted = await attach(original.run_dir)
                replay = []

                async def collect_adopted():
                    async for event in adopted.channel.events():
                        replay.append((event, adopted.replaying))

                adopted_reader = asyncio.create_task(collect_adopted())
                try:
                    await adopted.replay_finished.wait()
                    assert adopted._process.stdin.offset == len(before)
                    assert await adopted.channel.send("after detach", uuid="steering")
                    await eventually(lambda: len(replay) == 3)
                    adopted.channel.end_input()
                    await adopted_reader
                    assert [event for event, _ in replay[:2]] == events
                    assert [replaying for _, replaying in replay] == [True, True, False]
                    stdin = (original.run_dir / keeper.STDIN).read_bytes()
                    assert stdin.startswith(before)
                    assert json.loads(stdin[len(before):])["uuid"] == "steering"
                    assert replay[-1][0]["message"]["content"] == "after detach"
                    assert await adopted.wait() == 0
                finally:
                    adopted_reader.cancel()
                    await asyncio.gather(adopted_reader, return_exceptions=True)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                if adopted:
                    await adopted.end()
                if original._keeper_process.returncode is not None:
                    await original._keeper_process.wait()

    exercise(run())


def test_a_zero_replay_boundary_starts_live(tmp_path):
    directory = stored_run(tmp_path, [{"type": "system"}], consumed=0)

    async def run():
        handle = await attach(directory)
        try:
            assert not handle.replaying
            assert handle.replay_finished.is_set()
            assert len([event async for event in handle.channel.events()]) == 1
        finally:
            handle.release()

    exercise(run())


def test_attach_reads_files_after_the_keeper_exits_during_the_gap(tmp_path):
    async def run():
        async with opened(tmp_path) as original:
            reader = asyncio.create_task(original._process.stdout.read())
            adopted = None
            try:
                await reader
                idle_reader = asyncio.create_task(original._process.stdout.read())
                try:
                    await original._process.stdout.idle.wait()
                    assert (await original.detach(epoch=1, timeout=.3)).kind is DetachKind.DETACHED
                finally:
                    idle_reader.cancel()
                    await asyncio.gather(idle_reader, return_exceptions=True)
                # A new controller closes stdin, then disappears after the child exits.
                from looper.agents.keeper.client import KeeperClient
                client = KeeperClient()
                try:
                    await client.connect(original.run_dir / keeper.SOCKET)
                    await client.initialize(epoch=1, looper_version="test")
                    await client.stdin_close()
                    await original._keeper_process.wait()
                finally:
                    await client.close()
                    await client.wait_closed()
                adopted = await attach(original.run_dir)
                assert adopted._file_mode
                events = [event async for event in adopted.channel.events()]
                assert len(events) == 1
                assert events[0]["type"] == "system"
                assert await adopted.wait() == 0
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                if adopted:
                    await adopted.end()

    exercise(run())


def test_a_stale_attachment_fences_without_ending_the_child(tmp_path):
    from tests.test_kept_run import live

    async def run():
        async with opened(tmp_path) as original:
            async def consume():
                async for _ in original.channel.events():
                    pass

            reader = asyncio.create_task(consume())
            try:
                await original._process.stdout.idle.wait()
                assert (await original.detach(epoch=1, timeout=.3)).kind is DetachKind.DETACHED
                reasons = []
                with pytest.raises(KeeperError) as error:
                    await KeptRun.attach(original.run_dir, runtime=CLAUDE_CODE, launch=builtin_launch(),
                                         epoch=0, on_stale=reasons.append, label="stale test")
                assert error.value.code == ErrorCode.STALE_CONTROLLER
                assert len(reasons) == 1
                assert live(original.pid)
                assert (original.run_dir / keeper.DETACH).exists()
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                state = keeper.read_json(original.run_dir / keeper.STATE)
                await proc_tree.end_orphan(original.pid, state["child_started"], label="stale test cleanup")
                await original._keeper_process.wait()

    exercise(run())
