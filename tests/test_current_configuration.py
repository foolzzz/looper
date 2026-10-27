"""The portable format gate runs before destination access on every direct path."""

import asyncio
import json

import pytest

from looper.agents import config_transfer as transfer


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
