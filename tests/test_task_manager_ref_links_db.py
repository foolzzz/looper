"""A direct session's turn is refused, before and after it runs, while the ref storage holds a symbolic link."""
from __future__ import annotations

import shlex

from looper.agents import attention, task_manager_refs as refs, task_manager_safety
from looper.agents.tiers import task_manager as tm
from looper.db import task_manager_refs as store
from tests.db_support import run_db
from tests.task_manager_support import Sink, submit_and_wait
from tests.test_task_manager_refs_db import events_of, make_task, native  # noqa: F401  (fixture)

MARKER = "OUTSIDE_MARKER_91ab"
PATH = ".git/refs/heads/x"


def outside_file(tmp_path):
    target = tmp_path / MARKER
    target.write_text(MARKER + "\n")
    return target


def _no_outside_text(sink, tmp_path):
    said = repr(sink.finals) + repr(sink.events)
    assert MARKER not in said and str(tmp_path / MARKER) not in said


async def _needs_you(db, task_id):
    items, total = await attention._task_manager_refs(None)
    item, = items
    assert total == 1 and item["href"] == f"#tasks/{task_id}"
    assert item["facts"] == {"refs": [PATH], "count": 1, "reason": "ref_symlink"}
    assert "符号链接" in item["title"]
    line = attention._line("task_manager_refs", item, item["since"])
    assert PATH in line and "符号链接" in line and MARKER not in line + repr(item)
    return item


def test_a_link_present_before_a_turn_refuses_it_and_needs_you_once(monkeypatch, tmp_path, native):
    task_manager_safety.refusal_counts.clear()

    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        (directory / ".git" / "refs" / "heads" / "x").symlink_to(outside_file(tmp_path))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            for number in (1, 2):
                sink = Sink()
                result = await pool.submit(task_id, "reply", message_id=f"m{number}", sink=sink)
                assert result.state == "refused" and result.reason is tm.RefuseReason.REF_SYMLINK
                await sink.done()
                (text, outcome), = sink.finals
                assert outcome == "refused" and PATH in text
                assert events_of(sink, "refused") == [{"reason": tm.RefuseReason.REF_SYMLINK}]
                _no_outside_text(sink, tmp_path)
            assert native.records("launch") == [] and native.records("input") == []
            assert await db.fetchval("SELECT count(*) FROM task_manager_ref_reports") == 1
            assert sum(count for key, count in task_manager_safety.refusal_counts.items()
                       if key[0] == "ref_symlink") == 2
            item = await _needs_you(db, task_id)
            # Resolved while the link is still there: the next refusal lists it again.
            await store.resolve_report(int(item["key"].split(":")[1]), "operator")
            sink = Sink()
            await pool.submit(task_id, "reply", message_id="m3", sink=sink)
            await sink.done()
            await _needs_you(db, task_id)
            (directory / ".git" / "refs" / "heads" / "x").unlink()
            after = await submit_and_wait(pool, task_id, "reply", message_id="m4")
            assert after.finals == [("onetwo", "success")]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=90)


def test_a_link_made_by_a_turn_refuses_it_when_it_ends(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        target = outside_file(tmp_path)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            sink = await submit_and_wait(pool, task_id, f"sh:ln -s {shlex.quote(str(target))} {PATH}", message_id="m1")
            (text, outcome), = sink.finals
            assert outcome == "refused" and text.startswith("onetwo") and PATH in text
            assert events_of(sink, "refused") == [{"reason": tm.RefuseReason.REF_SYMLINK}]
            assert not events_of(sink, "turn_summary") and not events_of(sink, "ref_report")
            assert not [p for p in events_of(sink, "sandbox_notice") if p["notice"] == "refs_unreadable"]
            _no_outside_text(sink, tmp_path)
            await _needs_you(db, task_id)
            assert await db.fetchval("SELECT message_id FROM task_manager_ref_reports") == "m1"
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=90)


def test_a_link_made_between_the_preparation_and_the_snapshot_is_the_same_refusal(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        (directory / ".git" / "refs" / "heads" / "x").symlink_to(outside_file(tmp_path))
        sink = Sink()
        try:
            await refs.begin_turn({"id": task_id}, directory, sink=sink)
        except tm.Refused as refused:
            assert refused.reason is tm.RefuseReason.REF_SYMLINK and PATH in str(refused)
            assert MARKER not in str(refused)
        else:
            raise AssertionError("begin_turn accepted a linked ref")
        await _needs_you(db, task_id)
        assert await db.fetchval("SELECT count(*) FROM task_manager_branches") == 0 and sink.events == []
    run_db(check, monkeypatch, timeout=60)
