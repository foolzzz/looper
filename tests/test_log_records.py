"""A reader of a log that is still being appended to must never see a half-written record."""
import json
from pathlib import Path

from tests.db_support import complete_records


def test_a_half_written_last_record_is_not_parsed():
    done = json.dumps({"variant": "a", "text": "x" * 6000}) + "\n"
    assert complete_records(done + '{"variant": "a", "te') == [{"variant": "a", "text": "x" * 6000}]
    assert complete_records(done) == [{"variant": "a", "text": "x" * 6000}]
    assert complete_records("") == []


def test_stand_in_logs_read_only_finished_records(tmp_path: Path):
    from tests import test_drain_serve_db, test_task_roles_serve_db, test_user_gates_serve_db
    for module, row in ((test_drain_serve_db, {"kind": "worker", "variant": "v"}),
                        (test_task_roles_serve_db, {"variant": "v"}),
                        (test_user_gates_serve_db, {"variant": "v"})):
        name = "events.jsonl"
        (tmp_path / name).write_text(json.dumps(row) + "\n" + '{"variant": "v", "pro')
        read = module._events(tmp_path, "worker", "v") if module is test_drain_serve_db \
            else module._log(tmp_path, name, "v")
        assert read == [row]
