"""The collector's separate, explicitly profiled read-only CLI contract."""

import asyncio
import json
from datetime import timedelta

import pytest

from looper.agents.channels import lark_cli
from looper.agents.message_collect import CollectorError, CollectorErrorKind
from looper.agents.message_collect_lark import LarkUserCollector
from tests import msgflow_support as sup


def test_adapter_uses_only_profiled_read_commands_and_exact_millisecond_filtering(monkeypatch):
    calls = []

    async def answer(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ["lark-cli", "--profile", "synthetic"]
        if "status" in argv:
            return {"state": "ready", "user_open_id": sup.ME, "refresh_expires_at": None}
        if "+messages-search" in argv:
            return {
                "data": {
                    "messages": [
                        {
                            "message_id": "m",
                            "chat_id": "oc_synthetic",
                            "create_time": str(int(sup.NOW.timestamp() * 1000)),
                        }
                    ],
                    "has_more": False,
                }
            }
        if "+chat-list" in argv:
            assert argv[argv.index("--types") + 1] == "p2p,group"
            return {
                "data": {
                    "chats": [{"chat_id": "oc_synthetic", "chat_mode": "p2p", "name": "Synthetic"}],
                    "has_more": False,
                }
            }
        if "+search-user" in argv:
            return {"data": {"users": [{"open_id": sup.ME, "name": "Operator"}]}}
        assert argv[3:6] == ["api", "GET", "/open-apis/im/v1/messages"]
        assert argv[argv.index("--as") + 1] == "user"
        params = json.loads(argv[argv.index("--params") + 1])
        assert params["start_time"] == str(int((sup.NOW - timedelta(milliseconds=1)).timestamp()))
        assert params["end_time"] == str(int(sup.NOW.timestamp()) + 1)
        return {"data": {"items": [sup.message("m", sent_at=sup.NOW)], "has_more": False}}

    monkeypatch.setattr(lark_cli, "run_json", answer)

    async def check():
        backend = LarkUserCollector()
        assert (await backend.auth_status("synthetic")).user_open_id == sup.ME
        assert (await backend.search("synthetic", sup.NOW, sup.NOW + timedelta(seconds=1), "")).items[
            0
        ].message_id == "m"
        assert (
            len(
                (
                    await backend.list_messages(
                        "synthetic",
                        "chat",
                        "oc_synthetic",
                        sup.NOW - timedelta(milliseconds=1),
                        sup.NOW + timedelta(milliseconds=1),
                        "asc",
                        "",
                    )
                ).items
            )
            == 1
        )
        assert (await backend.list_chats("synthetic", "")).items[0].kind == "p2p"
        assert await backend.lookup_people("synthetic", [sup.ME]) == {sup.ME: "Operator"}
        with pytest.raises(CollectorError):
            await backend.auth_status("")
        with pytest.raises(CollectorError):
            await backend.lookup_people("synthetic", [str(i) for i in range(31)])

    asyncio.run(check())
    assert len(calls) == 5


@pytest.mark.parametrize(
    "raw", [{}, {"state": "unknown"}, {"data": {"state": "ready"}}, {"state": "ready", "user_open_id": []}]
)
def test_unknown_auth_envelope_is_protocol_failure(monkeypatch, raw):
    async def answer(*args, **kwargs):
        return raw

    monkeypatch.setattr(lark_cli, "run_json", answer)

    async def check():
        with pytest.raises(CollectorError) as failure:
            await LarkUserCollector().auth_status("synthetic")
        assert failure.value.kind == CollectorErrorKind.PROTOCOL

    asyncio.run(check())


def test_cancelled_call_kills_and_reaps_its_local_cli_process(tmp_path):
    import os
    import sys

    script = tmp_path / "stand_in.py"
    marker = tmp_path / "pid"
    script.write_text(
        "import os,pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(30)\n"
    )

    async def check():
        task = asyncio.create_task(lark_cli.run([sys.executable, str(script), str(marker)]))
        for _ in range(200):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists()
        pid = int(marker.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(check())
