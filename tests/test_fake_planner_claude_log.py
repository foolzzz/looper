"""The fake Planner Claude's turn log is read only up to its last committed line."""
import json


def test_turn_log_reader_skips_a_half_written_tail(planner_claude):
    claude = planner_claude("ok")
    log = claude.state / "turns.jsonl"
    first = json.dumps({"pid": 1, "text": "first"})
    tail = json.dumps({"pid": 2, "text": "[WAIT] a long prompt"})
    log.write_text(first + "\n" + tail[:20])
    assert claude.turns() == [{"pid": 1, "text": "first"}]
    log.write_text(first + "\n" + tail + "\n")
    assert [t["pid"] for t in claude.turns()] == [1, 2]
