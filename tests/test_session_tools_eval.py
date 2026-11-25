"""Routed Manager evals run through the native CLI transport, without external calls."""

import pytest

from evals.prompt_regression import dialogs
from tests.db_support import DSN


CASES = (
    "chat_tasks_list", "chat_task_create", "chat_task_stop", "chat_other_create_refused",
    "chat_other_task_refused", "chat_dm_overview_refused",
)


@pytest.mark.skipif(not DSN, reason="needs a disposable PostgreSQL server")
@pytest.mark.parametrize("case", CASES)
def test_routed_dialog_uses_cli_envelopes(tmp_path, case):
    result, _ = dialogs.run(mode="fake", dialogs=dialogs.select([case]), admin_url=str(DSN),
                            out_dir=tmp_path, timeout=45)
    assert result["aborted"] is None, result
    assert [row["outcome"] for row in result["dialogs"]] == ["pass"], result["dialogs"]
    assert result["dialogs"][0]["transport"] == "lark-cli CliTransport"
