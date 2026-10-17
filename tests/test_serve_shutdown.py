"""How ``looper serve`` stops: the first SIGINT or SIGTERM starts the shutdown, a shutdown that runs past its deadline
exits at once, and so does a second signal."""

from __future__ import annotations

import asyncio

from looper.agents import serve


def test_a_shutdown_that_ends_in_time_exits_normally() -> None:
    exits: list[int] = []

    async def shut_down() -> None:
        await asyncio.sleep(0.05)

    asyncio.run(serve.shut_down_within(shut_down(), deadline=1.0, exit_now=exits.append))
    assert exits == []


def test_a_shutdown_past_its_deadline_exits_at_once() -> None:
    exits: list[int] = []

    async def _run() -> float:
        async def hangs() -> None:
            await asyncio.Event().wait()  # an agent that never stops

        began = asyncio.get_running_loop().time()
        await serve.shut_down_within(hangs(), deadline=0.2, exit_now=exits.append)
        return asyncio.get_running_loop().time() - began

    took = asyncio.run(_run())
    assert exits == [1] and took < 1


def test_the_first_signal_starts_the_shutdown_and_a_second_exits_at_once() -> None:
    exits: list[int] = []

    async def _run() -> bool:
        stop = asyncio.Event()
        handler = serve.stop_signal_handler(stop, exit_now=exits.append)
        handler()
        started, exits_after_first = stop.is_set(), list(exits)
        handler()
        return started and exits_after_first == []

    assert asyncio.run(_run())
    assert exits == [1]
