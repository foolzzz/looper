"""The operator's card-button notes reach their next user turn on the real Manager path: the speaker is classified
before ``begin_turn`` reads it, with the turn-context switch on or off."""
import pytest

from looper.agents import receipts_settings
from looper.agents.turn_context import SpeakerKind
from tests.db_support import DSN, run_db
from tests.lark_cli_stand_in import OPEN_ID
from tests.test_manager_turn_context_db import capture, message, rig_with_ops, seed

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("channel", ["lark", "telegram"])
@pytest.mark.parametrize("enabled", [False, True], ids=["switch_off", "switch_on"])
def test_operator_button_note_reaches_the_next_user_turn(monkeypatch, tmp_path, enabled, channel):
    async def check(pool):
        await seed(pool, enabled=enabled)
        monkeypatch.setattr(receipts_settings, "_enabled", True)
        rig = rig_with_ops(tmp_path)
        ops = rig.manager.ops
        begin = ops._begin_turn
        at_begin = []

        def planted(turn, binding=None, *, session_key="main"):
            at_begin.append(turn.speaker)
            if not turn.autonomous:
                ops._chat_sessions[turn.chat_key] = session_key
                ops._note_button(turn.chat_key, turn.sender, "c-1", "it was cancelled; it did not run")
            return begin(turn, binding, session_key=session_key)

        monkeypatch.setattr(ops, "_begin_turn", planted)
        seen = capture(monkeypatch)
        sender = OPEN_ID if channel == "lark" else "42"
        try:
            await rig.manager._handle(message(channel, sender=sender, group=False))
            prepared, = seen
            assert at_begin == [SpeakerKind.OPERATOR] and prepared.turn.speaker is SpeakerKind.OPERATOR
            assert "looper-button-notes" in prepared.turn_text, prepared.turn_text[:400]
        finally:
            await rig.close()

    run_db(check, monkeypatch, timeout=40)
