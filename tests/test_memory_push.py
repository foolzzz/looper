"""Code-rendered memory notices contain the exact changes a DM may confirm."""
from datetime import datetime, timezone

import pytest

from looper.agents import memory_push


def row(**fields):
    return {"id": 42, "kind": "preference", "statement": "Conclusion first", "source_kind": "manager_turn",
            "created_at": datetime(2026, 11, 22, 14, tzinfo=timezone.utc), "untrusted": False,
            "flagged": False, "hard_rule": False, "state": "pending", "note_path": "mem/prefs/a.md",
            "commit_sha": None, "reason": None, **fields}


def test_complete_boundary_and_long_reply_has_no_confirm():
    for size in (1500, 1501):
        text, complete = memory_push.push_text(row(), {"large_removal": False, "diff_stats": {}},
                                             ["+ " + "x" * (size - 2)])
        assert complete is (size == 1500)
        assert ("回复「记」" in text) is complete
        assert ("内容较长，请到记忆页确认" in text) is not complete


@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize("markdown", [False, True])
def test_change_markdown_cannot_hide_urls(long, markdown):
    line = "+ [safe label](https://unsafe.example.invalid) ![image](https://image.example.invalid) `code`"
    lines = [line] + (["+ " + "x" * 1500] if long else [])
    text, complete = memory_push.push_text(row(), {}, lines, markdown=markdown)
    assert complete is not long
    if markdown:
        assert r"\[safe label\]\(https://unsafe\.example\.invalid\)" in text
        assert r"\!\[image\]\(https://image\.example\.invalid\)" in text
        assert r"\`code\`" in text
    else:
        assert line in text


def test_flags_are_code_rendered_and_mentions_removed():
    text, complete = memory_push.push_text(row(untrusted=True, flagged=True, hard_rule=True,
        statement="@all conclusion first"), {"large_removal": True, "diff_stats": {"a": {"removed": 14}}}, ["+ @all line"])
    assert complete and "@" not in text
    for fixed in ("来自飞书消息，未经确认", "来源里有要求助手做事的话", "会删掉这篇笔记 14 行", "可能还要写进全局指令"):
        assert fixed in text


def test_final_states_have_fixed_public_safe_text():
    assert memory_push.final_text(row(state="written", commit_sha="a" * 40)) == "✅ M-0042 已写入 mem/prefs/a.md（commit aaaaaaaa）"
    assert "处理后回「重试 M-0042」" in memory_push.final_text(row(state="blocked", reason="dirty_target"))
    assert "dirty_target" not in memory_push.final_text(row(state="blocked", reason="dirty_target"))
    assert memory_push.final_text(row(state="rejected")) == "❌ M-0042 已驳回"
    assert memory_push.final_text(row(state="expired")) == "⌛ M-0042 已过期"
    assert memory_push.final_text(row(state="conflict", reason="base_changed")).startswith("⚠️ M-0042")


def test_replace_note_renders_only_changes_with_context_and_validated_base(tmp_path):
    import hashlib
    import pytest
    from looper.agents import memory_write

    original = '\n'.join(['unchanged'] * 300 + ['old'] + ['unchanged'] * 300) + '\n'
    revised = original.replace('\nold\n', '\nnew\n')
    (tmp_path / 'note.md').write_text(original)
    change = {'op': 'replace_note', 'path': 'note.md', 'base_sha256': hashlib.sha256(original.encode()).hexdigest(),
              'content': revised}
    lines = memory_push.changed_lines(tmp_path, [change])
    assert lines == ['写入 note.md：', ' unchanged', '-old', '+new', ' unchanged']
    text, complete = memory_push.push_text(row(), {'large_removal': False}, lines,
                                          dashboard_url='https://memory.example.invalid')
    assert complete and text.count('unchanged') == 2
    (tmp_path / 'note.md').write_text('unreviewed content')
    with pytest.raises(memory_write.WriteFailure):
        memory_push.changed_lines(tmp_path, [change])


def test_long_notice_has_an_absolute_dashboard_link():
    text, complete = memory_push.push_text(row(), {}, ['+ ' + 'x' * 1500],
                                          dashboard_url='https://memory.example.invalid')
    assert not complete
    assert 'https://memory.example.invalid/#/memory?candidate=M-0042' in text
