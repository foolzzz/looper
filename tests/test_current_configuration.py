"""The portable format gate runs before destination access on every direct path."""

import asyncio
import json

import pytest

from looper.agents import config_transfer as transfer
from tests.db_support import DSN, run_db, v1_api


@pytest.mark.parametrize("supplied", [None, "loopx-config/v1", "looper-config/v1", "looper-config/v2",
                                      "looper-config/v3", "looper-config/v5", "unknown"])
@pytest.mark.parametrize("path", ["file", "preview", "apply"])
def test_only_current_format_reaches_destination(supplied, path, tmp_path):
    data = {"format": supplied} if supplied is not None else {}
    with pytest.raises(transfer.ConfigFileError) as error:
        if path == "file":
            file = tmp_path / "config.json"
            file.write_text(json.dumps(data))
            transfer.read_file(file)
        else:
            # No connection is available: rejecting the format must precede every DB/target operation.
            asyncio.run(transfer.import_config(None, data, apply=path == "apply"))
    message = str(error.value)
    assert repr(supplied) in message and transfer.FORMAT in message and "v2.13.165" in message
    assert message.index("Import old files") < message.index("upgrade") < message.index("export v4")


@pytest.mark.parametrize("field", ["agents", "loopx_version"])
def test_current_format_does_not_accept_old_top_level_aliases(field, tmp_path):
    file = tmp_path / "config.json"
    file.write_text(json.dumps({"format": transfer.FORMAT, field: [] if field == "agents" else "2.6.0"}))
    with pytest.raises(transfer.ConfigFileError, match=field):
        transfer.read_file(file)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_configuration_cannot_import_ledger_events(monkeypatch, tmp_path):
    from looper.db import ledger
    from tests import ledger_support as sup

    async def check(pool):
        item = await sup.add_own(pool, "decision", title="Keep the synthetic decision")

        async def snapshot():
            return (list(await pool.fetch("SELECT * FROM ledger_items ORDER BY id")),
                    list(await pool.fetch("SELECT * FROM ledger_events ORDER BY id")))

        before = await snapshot()
        assert len(before[0]) == len(before[1]) == 1  # positive read control
        document = {"format": transfer.FORMAT}
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("config.import_preview", {"document": document})
            assert status == 200, answer  # the same valid bundle reaches the destination
            import_id = answer["result"]["import_id"]
            status, answer = await call("config.import_cancel", {"import_id": import_id})
            assert status == 200, answer
            injections = {"ledger_items": [{"id": item["number"], "status": "done"}],
                          "ledger_events": [{"item_id": item["number"], "action": "confirm", "actor_kind": "user"}],
                          "actor_kind": "user", "actor_id": "operator", "via": "dashboard"}
            for field, value in injections.items():
                status, answer = await call("config.import_preview", {"document": {**document, field: value}})
                assert status == 400 and field in str(answer), answer
                assert await snapshot() == before
        assert (await ledger.get_item(item["number"], db=pool))["status"] == "open"

    run_db(check, monkeypatch, timeout=30)
