"""Scoped refusals through a disposable server and the native Lark CLI stand-in."""

from pathlib import Path

import pytest

from evals.prompt_regression import dialogs
from tests.db_support import DSN


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_scoped_member_guess_refused_on_native_cli_transport(tmp_path: Path) -> None:
    case = next(case for case in dialogs.FAKE_DIALOGS if case.id == "scoped_member_guess_refused")
    result, _ = dialogs.run(mode="fake", dialogs=(case,), admin_url=str(DSN), out_dir=tmp_path,
                            agent="claude-code/opus")
    assert result["aborted"] is None
    row, = result["dialogs"]
    assert row["outcome"] == "pass", row["problems"]
    assert row["transport"] == "lark-cli CliTransport"
    turn, = row["turns"]
    assert turn["sender"] == "member"
    assert turn["calls"] == ["looper_task_create:error (forbidden)"]
