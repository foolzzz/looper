"""DM confirmations wake only the running memory writer."""

import asyncio

from looper.agents import memory_write


def test_wake_tracks_writer_lifecycle(tmp_path, monkeypatch):
    async def check():
        service = memory_write.MemoryWriteService(tmp_path)

        async def idle():
            await service._woken.wait()

        monkeypatch.setattr(service, "_loop", idle)
        memory_write.wake()
        assert not service._woken.is_set()
        await service.start()
        memory_write.wake()
        assert service._woken.is_set()
        await service.stop()
        service._woken.clear()
        memory_write.wake()
        assert not service._woken.is_set()

    asyncio.run(check())
