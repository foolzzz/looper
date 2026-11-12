"""Bot resource downloads stay message-bound, private and bounded."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lark_platform_fake import FakeLarkPlatform
from looper.agents import chat_attachments
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark_api import LarkApi, LarkApiError
from looper.agents.channels.lark_bot import make_adapter

PNG = b"\x89PNG\r\n\x1a\n"
RESOURCE_PATH = "/open-apis/im/v1/messages/om_resource/resources/img_resource"


@pytest.mark.parametrize("scenario", ["length", "chunked", "unauthorized", "invalid_json_token", "missing", "scope"])
def test_resource_limits_and_token_refresh_through_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    monkeypatch.setattr(chat_attachments, "IMAGE_MAX_BYTES", 128)

    async def run() -> None:
        async with FakeLarkPlatform() as fake:
            data = PNG + b"x" * 129 if scenario in {"length", "chunked"} else PNG
            if scenario not in {"missing", "scope"}:
                fake.resources[("om_resource", "img_resource")] = (data, "image/png")
            if scenario == "chunked":
                fake.chunked_resources.add(("om_resource", "img_resource"))
            if scenario in {"unauthorized", "invalid_json_token"}:
                fake.fail_next(RESOURCE_PATH, 99991663, "Invalid access token", 401 if scenario == "unauthorized" else 200)
            if scenario == "scope":
                fake.fail_next(RESOURCE_PATH, 99991672, "access denied", 403)
            bus = MessageBus(runtime_root=tmp_path)
            adapter = make_adapter({"app_id": fake.app_id}, "test-secret", runtime_root=tmp_path,
                allowlist=SenderAllowlist("channel-lark_bot", frozenset({"ou_sender"})), on_rejected=None,
                bot_open_id=fake.bot["open_id"], base_url=fake.base_url)
            try:
                await adapter.start(bus)
                await fake.push_message(message_type="image", content=json.dumps({"image_key": "img_resource"}),
                                        message_id="om_resource")
                message = await asyncio.wait_for(bus.get_queue("manager").get(), 3)
                item, = message.payload["attachments"]
                requests = [request for request in fake.requests if "/resources/" in request["path"]]
                assert all(request["query"] == {"type": ["image"]} and request["authenticated"] for request in requests)
                expected = "too_large" if scenario in {"length", "chunked"} else "failed" if scenario in {"missing", "scope"} else "saved"
                assert item["state"] == expected
                directory = tmp_path / "chat-attachments" / "channel-lark_bot" / "om_resource"
                assert not list(directory.glob("*.part"))
                if expected == "saved":
                    assert Path(item["path"]).read_bytes() == PNG
                    assert fake.token_requests == 2 and len(requests) == 2
                else:
                    assert not list(directory.iterdir())
                    assert item["told"] is True
                    if scenario == "scope":
                        assert "im:message:readonly" in item["reason"]
                    assert len([sent for sent in fake.sent if sent["kind"] == "send"]) == 1
            finally:
                await adapter.stop()
                await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 8))


@pytest.mark.parametrize(("status", "code"), [(401, 42), (400, 99991661), (200, 99991663)])
def test_download_retries_invalid_token_only_once(tmp_path: Path, status: int, code: int) -> None:
    async def run() -> None:
        async with FakeLarkPlatform() as fake:
            for _ in range(2):
                fake.fail_next(RESOURCE_PATH, code, "invalid token", status)
            api = LarkApi(fake.app_id, "test-secret", base_url=fake.base_url)
            try:
                with pytest.raises(LarkApiError) as caught:
                    await api.download("om_resource", "img_resource", "image", tmp_path / "1.part", max_bytes=1024)
                assert caught.value.code == code
                assert fake.token_requests == 2
                assert not (tmp_path / "1.part").exists()
            finally:
                await api.aclose()

    asyncio.run(asyncio.wait_for(run(), 5))


def test_json_resource_is_preserved_and_existing_file_is_not_overwritten(tmp_path: Path) -> None:
    async def run() -> None:
        async with FakeLarkPlatform() as fake:
            content = json.dumps({"code": 404, "text": "attachment"}).encode()
            fake.resources[("om_resource", "file_resource")] = (content, "application/json")
            api = LarkApi(fake.app_id, "test-secret", base_url=fake.base_url)
            part = tmp_path / "1.part"
            try:
                await api.download("om_resource", "file_resource", "file", part, max_bytes=1024)
                assert part.read_bytes() == content
                assert part.stat().st_mode & 0o777 == 0o600
                with pytest.raises(FileExistsError):
                    await api.download("om_resource", "file_resource", "file", part, max_bytes=1024)
                assert part.read_bytes() == content
            finally:
                await api.aclose()

    asyncio.run(asyncio.wait_for(run(), 5))
