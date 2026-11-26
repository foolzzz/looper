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


@pytest.mark.parametrize(("case_id", "sender", "reply"), [
    ("scoped_operator_group_contract", "operator", "这个群里之前说过：SCOPED_THIS_CHAT_HISTORY。"),
    ("scoped_shared_speakers", "member", "这条会话打不开附件，请把需要的内容贴出来。"),
])
@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_scoped_inputs_on_native_cli_transport(tmp_path: Path, case_id: str, sender: str, reply: str) -> None:
    case, = dialogs.select([case_id])
    result, _ = dialogs.run(mode="fake", dialogs=(case,), admin_url=str(DSN), out_dir=tmp_path,
                            agent="claude-code/opus")
    assert result["aborted"] is None
    row, = result["dialogs"]
    assert row["outcome"] == "pass", row["problems"]
    assert row["transport"] == "lark-cli CliTransport"
    turn, = row["turns"]
    assert turn["sender"] == sender
    assert turn["calls"] == []
    # The stand-in returns this only after checking its actual system prompt and complete turn input.
    assert turn["reply"] == reply
