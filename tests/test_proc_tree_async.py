"""Slow process queries must not stall unrelated work on the event loop."""

import asyncio
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from looper.agents import proc_tree


def test_slow_ps_does_not_block_spawn_track_or_begin_end(tmp_path, monkeypatch):
    run = subprocess.run

    def slow_ps(argv, **kwargs):
        if argv[0] == "ps":
            time.sleep(1.5)
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", slow_ps)
    monkeypatch.setattr(proc_tree, "_records", tmp_path)
    monkeypatch.setattr(proc_tree, "_server", (os.getpid(), "owner"))
    monkeypatch.setattr(proc_tree, "_recorded", {})
    monkeypatch.setattr(proc_tree, "_ending", set())
    monkeypatch.setattr(proc_tree, "_owner_run_dir", None)
    monkeypatch.setattr(proc_tree, "_spawns_fenced", False)
    # Keep the measurement about process queries; durable-record fsync has its own I/O cost.
    monkeypatch.setattr(proc_tree, "_write_record", lambda directory, pid, *args, **kwargs: directory / f"{pid}.json")
    gaps = []

    async def run_test():
        async def heartbeat():
            previous = time.monotonic()
            while True:
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - previous)
                previous = now

        beating = asyncio.create_task(heartbeat())
        process = None
        try:
            await asyncio.sleep(0.04)
            process = await proc_tree.spawn("/bin/sleep", "5")
            await proc_tree.track(process)
            await proc_tree.begin_end(process)
            await asyncio.sleep(0.04)
        finally:
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await process.wait()
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)

    asyncio.run(run_test())
    maximum = max(gaps)
    print(f"maximum heartbeat gap: {maximum:.3f} s")
    assert maximum < 0.3


def test_periodic_trees_share_one_process_listing_per_window(monkeypatch):
    listings = []

    def ps(argv, **kwargs):
        if "-A" in argv:
            listings.append(threading.current_thread().name)
            time.sleep(0.03)  # Let the other readers join this query.
            output = "101 1 101 S\n102 1 102 S\n103 1 103 S\n"
        else:
            output = "".join(f"{pid} started\n" for pid in argv[-1].split(","))
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(subprocess, "run", ps)

    async def run_test():
        trees = await asyncio.gather(*(proc_tree.track(SimpleNamespace(pid=pid, returncode=None))
                                       for pid in (101, 102, 103)))
        listings.clear()
        await asyncio.gather(*(tree.refresh() for tree in trees))
        assert len(listings) == 1
        assert all(tree.seen == {tree.process.pid: "started"} for tree in trees)
        await proc_tree.shared_snapshot()
        assert len(listings) == 1
        await asyncio.sleep(0.55)
        await asyncio.gather(*(tree.refresh() for tree in trees))
        assert len(listings) == 2
        # Destructive preparation bypasses the periodic cache.
        await trees[0].refresh(force=True)
        assert len(listings) == 3

    asyncio.run(run_test())
    assert all(name.startswith("looper-ps") for name in listings)


def test_cancelling_spawn_during_registration_reaps_the_unpublished_process(tmp_path, monkeypatch):
    monkeypatch.setattr(proc_tree, "_records", tmp_path)
    monkeypatch.setattr(proc_tree, "_server", (os.getpid(), "owner"))
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.1)
    monkeypatch.setattr(proc_tree, "_spawns_fenced", False)

    async def run_test():
        entered = asyncio.Event()
        created = []
        create = asyncio.create_subprocess_exec
        run_ps = proc_tree.run_ps

        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs)
            created.append(process)
            return process

        async def pause_registration(func, *args, **kwargs):
            if func is proc_tree._start_time:
                entered.set()
                await asyncio.Event().wait()
            return await run_ps(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        monkeypatch.setattr(proc_tree, "run_ps", pause_registration)
        spawning = asyncio.create_task(proc_tree.spawn("/bin/sleep", "60"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            spawning.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(spawning, 3)
            assert created[0].returncode is not None
            assert created[0] not in proc_tree._owned
        finally:
            spawning.cancel()
            await asyncio.gather(spawning, return_exceptions=True)
            for process in created:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    asyncio.run(run_test())


def test_cancelling_one_shared_reader_keeps_the_query_for_other_readers(monkeypatch):
    release = threading.Event()
    calls = []

    async def run_test():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def snapshot():
            calls.append(1)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise TimeoutError("process query was not released")
            return {101: (1, 101)}

        monkeypatch.setattr(proc_tree, "_snapshot", snapshot)
        first = asyncio.create_task(proc_tree.shared_snapshot())
        second = asyncio.create_task(proc_tree.shared_snapshot())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not second.done()
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        assert await second == {101: (1, 101)}
        assert await proc_tree.shared_snapshot() == {101: (1, 101)}
        assert calls == [1]

    asyncio.run(run_test())


def test_failed_shared_observation_is_retried(monkeypatch):
    answers = iter((None, {101: (1, 101)}))
    monkeypatch.setattr(proc_tree, "_snapshot", lambda: next(answers))

    async def run_test():
        assert await proc_tree.shared_snapshot() is None
        assert await proc_tree.shared_snapshot() == {101: (1, 101)}

    asyncio.run(run_test())


def test_four_slow_ps_queries_leave_default_executor_available(monkeypatch):
    release = threading.Event()
    threads = []

    async def run_test():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        entered = asyncio.Event()

        def ps(argv, **kwargs):
            threads.append(threading.current_thread().name)
            if len(threads) == 4:
                loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise TimeoutError("process queries were not released")
            return subprocess.CompletedProcess(argv, 0, "101 1 101 S\n", "")

        monkeypatch.setattr(subprocess, "run", ps)
        queries = [asyncio.create_task(proc_tree.run_ps(proc_tree._snapshot)) for _ in range(4)]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "file I/O slot"), 0.3) == "file I/O slot"
        finally:
            release.set()
            await asyncio.gather(*queries)

    asyncio.run(run_test())
    assert len(threads) == 4
    assert all(name.startswith("looper-ps") for name in threads)


def test_cancelled_preparation_sends_term_before_kill_to_observed_processes(tmp_path, monkeypatch):
    process = SimpleNamespace(pid=101, returncode=None)
    table = {101: (1, 101), 102: (101, 102)}
    signals = []
    monkeypatch.setattr(proc_tree, "_snapshot", lambda: dict(table))
    monkeypatch.setattr(proc_tree, "_recorded", {101: (tmp_path / "101.json", "started")})
    monkeypatch.setattr(proc_tree, "_ending", set())

    def signal_tree(process, tree, sig):
        signals.append((dict(tree), sig))
        if sig == proc_tree.signal.SIGKILL:
            table.clear()

    monkeypatch.setattr(proc_tree, "_signal", signal_tree)

    async def run_test():
        entered = asyncio.Event()

        async def record(*args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(proc_tree, "_record_tree", record)
        ending = asyncio.create_task(proc_tree._end(process, label="turn", known={}))
        await asyncio.wait_for(entered.wait(), 1)
        ending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ending

    asyncio.run(run_test())
    assert signals == [({101: 101, 102: 102}, sig)
                       for sig in (proc_tree.signal.SIGTERM, proc_tree.signal.SIGKILL)]
