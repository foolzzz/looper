"""The bridge reader: which bytes of a chat file are new, and what a complete line says."""

import json
import os

import pytest

from looper.agents import bridge_reader as reader
from tests import msgflow_support as sup


def read(path, state=None, **options):
    return reader.scan_file(path, state, **options)


def ids(scan):
    return [m.external_id for m in scan.messages]


def test_reads_the_new_lines_after_the_saved_offset(tmp_path):
    path = sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("m2"))
    first = read(path)
    assert ids(first) == ["m1", "m2"] and not first.rescanned and not first.unchanged
    sup.append(tmp_path, "oc_a", sup.message("m3"))
    second = read(path, first.state)
    assert ids(second) == ["m3"] and not second.rescanned
    assert second.state.offset == path.stat().st_size


def test_a_file_that_did_not_change_is_skipped(tmp_path):
    path = sup.append(tmp_path, "oc_a", sup.message("m1"))
    first = read(path)
    again = read(path, first.state)
    assert again.unchanged and again.messages == []


def test_an_unterminated_last_line_waits_for_its_newline(tmp_path):
    whole = sup.line(sup.message("m2"))
    path = sup.append(tmp_path, "oc_a", sup.message("m1"), partial=whole[:20])
    first = read(path)
    assert ids(first) == ["m1"]
    assert first.state.offset < path.stat().st_size
    with path.open("ab") as handle:
        handle.write(whole[20:])
    assert ids(read(path, first.state)) == ["m2"]


@pytest.mark.parametrize("damage", ["shrink", "new_inode", "other_line_before_offset"])
def test_a_rewritten_file_is_read_again_from_the_start(tmp_path, damage):
    path = sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("m2"), sup.message("m3"))
    first = read(path)
    if damage == "shrink":
        path.write_bytes(sup.line(sup.message("m1")))
        os.utime(path, ns=(1, 1))
    elif damage == "new_inode":
        body = path.read_bytes()
        replacement = path.with_suffix(".new")
        replacement.write_bytes(body + sup.line(sup.message("m4")))
        os.replace(replacement, path)
    else:
        # A longer file than the offset, but the line that ends at the offset is another message.
        path.write_bytes(sup.line(sup.message("m1")) + sup.line(sup.message("m2")) + sup.line(sup.message("mX"))
                         + sup.line(sup.message("m4")))
    again = read(path, first.state)
    assert again.rescanned and not again.unchanged
    assert ids(again)[0] == "m1" and again.state.offset == path.stat().st_size


def test_duplicate_lines_and_blank_or_corrupt_lines_do_not_stop_the_read(tmp_path):
    path = sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("m1"),
                      partial=b"\n{not json}\n" + sup.line(sup.message("m2")))
    scan = read(path)
    assert ids(scan) == ["m1", "m1", "m2"] and scan.bad_lines == 1


def test_a_line_longer_than_the_read_window_is_still_read(tmp_path):
    path = sup.append(tmp_path, "oc_a", sup.message("m1", text="x" * 5000), sup.message("m2"))
    first = read(path, max_bytes=100)
    assert ids(first)[0] == "m1" and first.state.offset > 100


def test_reading_in_windows_resumes_where_the_last_one_ended(tmp_path):
    path = sup.append(tmp_path, "oc_a", *(sup.message(f"m{n}", text="y" * 200) for n in range(6)))
    seen, state = [], None
    for _ in range(10):
        scan = read(path, state, max_bytes=500)
        seen += ids(scan)
        state = scan.state
        if state.offset == path.stat().st_size:
            break
    assert seen == [f"m{n}" for n in range(6)]


def test_a_baseline_starts_a_file_at_its_end_and_the_next_scan_reads_only_what_follows(tmp_path):
    whole = sup.line(sup.message("m3"))
    path = sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("m2"), partial=whole[:10])
    base = reader.baseline_file(path)
    assert base.last_id == "m2" and base.offset < path.stat().st_size
    with path.open("ab") as handle:
        handle.write(whole[10:])
    scan = read(path, base)
    assert ids(scan) == ["m3"] and not scan.rescanned


def test_a_scan_that_stopped_at_its_window_says_more_follows(tmp_path):
    path = sup.append(tmp_path, "oc_a", *(sup.message(f"m{n}", text="y" * 200) for n in range(6)))
    first = read(path, max_bytes=500)
    assert first.more and not first.incomplete_tail
    done = read(path, first.state, max_bytes=10**6)
    assert not done.more
    assert read(sup.append(tmp_path, "oc_b", sup.message("x"), partial=b"{").parent / "oc_b.ndjson").incomplete_tail


def test_the_fields_of_an_event_come_from_the_message(tmp_path):
    raw = sup.message("m1", text="see", sender=sup.BOB, mentions=[(sup.ME, "Me"), (sup.ALICE, "Alice")],
                      thread_id="omt_1", root_id="om_root", parent_id="om_parent")
    path = sup.append(tmp_path, "oc_a", raw)
    (message,) = read(path).messages
    assert (message.sender_id, message.sender_kind) == (sup.BOB, "user")
    assert message.mention_ids == [sup.ME, sup.ALICE]
    assert message.text == "see @Me @Alice"
    assert (message.thread_id, message.root_id, message.parent_id) == ("omt_1", "om_root", "om_parent")
    assert message.sent_at == sup.NOW.replace(minute=55, hour=11)
    assert message.body["message_id"] == "m1" and message.body_bytes == len(sup.line(raw)) - 1


@pytest.mark.parametrize("sender_type,kind", [("user", "user"), ("app", "app"), ("anonymous", "other")])
def test_sender_kind_follows_the_sender_type(tmp_path, sender_type, kind):
    path = sup.append(tmp_path, "oc_a", sup.message("m1", sender_type=sender_type))
    assert read(path).messages[0].sender_kind == kind


@pytest.mark.parametrize("content,expected", [
    ({"text": "hi @_user_1", "_m": [("@_user_1", "Ann")]}, "hi @Ann"),
    ({"text": "to @_all"}, "to @所有人"),
    ({"title": "Card title", "elements": []}, "Card title"),
    ({"content": [[{"tag": "text", "text": "a"}, {"tag": "at", "user_name": "Bo"}], [{"tag": "text", "text": "b"}]]},
     "a@Bo\nb"),
    ({}, "[text]"),
])
def test_text_extraction(content, expected):
    import json

    mentions = [{"key": key, "name": name} for key, name in content.pop("_m", [])]
    raw = {"message_id": "m1", "msg_type": "text", "create_time": "1000", "sender": {"id": "ou", "sender_type": "user"},
           "body": {"content": json.dumps(content)}, "mentions": mentions}
    assert reader.extract_text(raw) == expected


def test_a_nul_character_never_reaches_the_database(tmp_path):
    path = sup.append(tmp_path, "oc_a", sup.message("m1", text="a\u0000b"))
    (message,) = read(path).messages
    assert "\x00" not in message.text and "\x00" not in json.dumps(message.body, ensure_ascii=False)


def test_a_line_over_the_cap_is_skipped_and_counted_without_being_held(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "MAX_LINE_BYTES", 1000)
    path = sup.append(tmp_path, "oc_a", sup.message("m1"), sup.message("huge", text="h" * 5000), sup.message("m2"))
    seen, bad, state = [], 0, None
    for _ in range(10):
        scan = read(path, state, max_bytes=300)
        seen += ids(scan)
        bad += scan.bad_lines
        state = scan.state
        if not scan.more:
            break
    assert seen == ["m1", "m2"] and bad == 1


def test_a_number_json_allows_but_the_database_refuses_makes_a_bad_line(tmp_path):
    path = sup.append(tmp_path, "oc_a", sup.message("m1"),
                      partial=b'{"message_id": "nan", "create_time": "1000", "sender": {}, "x": NaN}\n')
    scan = read(path)
    assert ids(scan) == ["m1"] and scan.bad_lines == 1
