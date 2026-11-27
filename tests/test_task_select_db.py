"""tasks.select against a disposable database: directory, id, merge request, text, ambiguity."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents.action_levels import ActionLevel
from looper.agents.ops import ErrorCode, OpError
from looper.agents.v1_domains import task_select as domain
from looper.db import projects, task_worktrees, tasks
from looper.db.task_changes import add_supplement
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

_HINT_TEXT = "Nothing matched. Search every task with all, or give the task id."
_HINT_ID = "No task has this id."
_HINT_RECENT = "No task had activity in the last 14 days."
_KEYS = {"kind", "task", "candidates", "more", "searched", "hint"}
_ROW = {"task_id", "title", "project_name", "status", "group", "last_activity_at"}


async def _select(arguments):
    result = await domain.run(None, domain.OPERATIONS[0], arguments, None, {})
    assert set(result) == _KEYS
    return result


def _ids(result):
    return [row["task_id"] for row in result["candidates"]]


def _one(result, task_id):
    assert result["kind"] == "one" and result["task"]["task_id"] == task_id
    assert result["candidates"] == [] and result["more"] == 0
    assert result["searched"] is None and result["hint"] is None
    assert set(result["task"]) == _ROW


def _candidates(result, ids, more=0):
    assert result["kind"] == "candidates" and result["task"] is None
    assert _ids(result) == ids and result["more"] == more
    assert result["searched"] is None and result["hint"] is None
    assert all(set(row) == _ROW for row in result["candidates"])


def _none(result, fields, window, see_all, hint):
    assert result["kind"] == "none" and result["task"] is None
    assert result["candidates"] == [] and result["more"] == 0
    assert result["searched"] == {"fields": fields, "window_days": window, "all": see_all}
    assert result["hint"] == hint


async def _task(pool, project_id, task_id, **fields):
    await tasks.create_task(task_id=task_id, project_id=project_id, requirement=fields.pop("requirement", "work"),
                            pool=pool, **fields)
    return task_id


async def _activity(pool, task_id, when):
    await pool.execute("INSERT INTO activity (task_id, kind, created_at) VALUES ($1, 'note', $2)", task_id, when)


async def _backdate(pool, task_ids, interval):
    await pool.execute(f"UPDATE tasks SET updated_at = now() - interval '{interval}' WHERE id = ANY($1::text[])",
                       list(task_ids))


async def _project(pool):
    return (await projects.create_project(name="select", pool=pool))["id"]


async def _stamp(pool, task_ids):
    # One shared, recent timestamp relative to the database clock: the default search window is 90 days back
    # from now(), so a fixed date would fall out of it once the real date passed.
    await _backdate(pool, task_ids, "10 days")


async def _wait(pool, task_id, number, repo="acme/widgets"):
    step_id = f"step-{task_id}"
    await pool.execute(
        "INSERT INTO task_steps (id, task_id, step_index, instruction) VALUES ($1, $2, 0, 'wait')",
        step_id, task_id)
    spec = {"check": {"type": "github_pr", "repo": repo, "number": number, "ready_when": "merged"}}
    await pool.execute(
        "INSERT INTO task_waits (step_id, task_id, topic, spec) VALUES ($1, $2, 'pr', $3::jsonb)",
        step_id, task_id, json.dumps(spec))


async def _worktree(pool, task_id, path, *, removed=False):
    await task_worktrees.save({
        "task_id": task_id, "repo_dir": path, "subdir": "", "path": path, "branch": "task",
        "base_commit": "abc", "links": [], "state": "removed" if removed else "active",
        "result_commit": None, "removed_at": datetime.now(timezone.utc) if removed else None,
    }, pool=pool)


def test_relative_cwd_is_rejected(monkeypatch):
    async def check(pool):
        with pytest.raises(OpError) as raised:
            await _select({"cwd": "relative/path"})
        assert raised.value.code is ErrorCode.INVALID and str(raised.value) == "cwd must be an absolute path"

    run_db(check, monkeypatch)


@pytest.mark.parametrize("kind,see_all", [
    ("empty", False),
    ("empty", True),
    ("sibling", False),
    ("removed", False),
])
def test_directory_miss_uses_the_recent_window(kind, see_all, monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        if kind == "sibling":
            root, other = tmp_path / "proj", tmp_path / "proj-other"
            root.mkdir()
            other.mkdir()
            await _task(pool, project_id, "task-proj", work_dir=str(root), title="proj", status="running")
            await _backdate(pool, ["task-proj"], "30 days")
            cwd = other
        elif kind == "removed":
            gone = tmp_path / "gone"
            gone.mkdir()
            await _task(pool, project_id, "task-gone", title="gone", status="completed")
            await _worktree(pool, "task-gone", str(gone), removed=True)
            await _backdate(pool, ["task-gone"], "30 days")
            cwd = gone
        else:
            cwd = tmp_path / "nowhere"
            cwd.mkdir()
        _none(await _select({"cwd": str(cwd), "all": see_all}),
              ["last_activity_at"], 14, see_all, _HINT_RECENT)

    run_db(check, monkeypatch)


@pytest.mark.parametrize("selection", ["", '""', "   "])
def test_blank_selection_uses_the_working_directory(selection, monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        root = tmp_path / "proj"
        root.mkdir()
        await _task(pool, project_id, "task-here", work_dir=str(root), title="here", status="running")
        found = await _select({"selection": selection, "cwd": str(root)})
        _one(found, "task-here")
        assert found["task"]["group"] == "in_progress" and found["task"]["project_name"] == "select"
        assert found["task"]["last_activity_at"]

    run_db(check, monkeypatch)


@pytest.mark.parametrize("source", ["work_dir", "workspace_dir", "worktree", "symlink", "dotdot"])
def test_stored_directory_resolves_to_one_task(source, monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        target = tmp_path / "target"
        target.mkdir()
        if source == "symlink":
            alias = tmp_path / "alias"
            alias.symlink_to(target, target_is_directory=True)
            stored = str(alias)
        elif source == "dotdot":
            stored = str(target / "missing" / "..")
        else:
            stored = str(target)
        task_id = "task-dir"
        if source == "workspace_dir":
            await _task(pool, project_id, task_id, workspace_dir=stored, title="dir", status="running")
        elif source == "worktree":
            await _task(pool, project_id, task_id, title="dir", status="running")
            await _worktree(pool, task_id, stored)
        else:
            await _task(pool, project_id, task_id, work_dir=stored, title="dir", status="running")
        _one(await _select({"cwd": str(target)}), task_id)

    run_db(check, monkeypatch)


@pytest.mark.parametrize("source", ["work_dir", "workspace_dir", "worktree"])
@pytest.mark.parametrize("change", ["retarget", "create"])
@pytest.mark.parametrize("count,more", [(2, 0), (11, 1)])
def test_changed_symlink_keeps_complete_directory_candidates(source, change, count, more, monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        old, target, alias = tmp_path / "old", tmp_path / "target", tmp_path / "alias"
        old.mkdir()
        target.mkdir()
        if change == "retarget":
            alias.symlink_to(old, target_is_directory=True)
        ids = [f"task-{index:012x}" for index in range(1, count + 1)]
        fields = {source: str(alias)} if source != "worktree" else {}
        await _task(pool, project_id, ids[0], title="dynamic", status="running", **fields)
        if source == "worktree":
            await _worktree(pool, ids[0], str(alias))
        for task_id in ids[1:]:
            await _task(pool, project_id, task_id, work_dir=str(target), title="target", status="running")
        await _stamp(pool, ids)
        if change == "retarget":
            alias.unlink()
        alias.symlink_to(target, target_is_directory=True)

        _candidates(await _select({"cwd": str(target)}), ids[:10], more=more)

    run_db(check, monkeypatch)


@pytest.mark.parametrize("source", ["work_dir", "workspace_dir", "worktree"])
def test_retargeted_symlink_no_longer_matches_its_old_target(source, monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        old, target, alias = tmp_path / "old", tmp_path / "target", tmp_path / "alias"
        old.mkdir()
        target.mkdir()
        alias.symlink_to(old, target_is_directory=True)
        fields = {source: str(alias)} if source != "worktree" else {}
        await _task(pool, project_id, "task-dynamic", title="dynamic", status="running", **fields)
        if source == "worktree":
            await _worktree(pool, "task-dynamic", str(alias))
        await _backdate(pool, ["task-dynamic"], "30 days")
        alias.unlink()
        alias.symlink_to(target, target_is_directory=True)

        _none(await _select({"cwd": str(old)}), ["last_activity_at"], 14, False, _HINT_RECENT)

    run_db(check, monkeypatch)


def test_deepest_directory_wins(monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        root, leaf = tmp_path / "proj", tmp_path / "proj" / "sub" / "leaf"
        leaf.mkdir(parents=True)
        await _task(pool, project_id, "task-shallow", work_dir=str(root), title="shallow", status="running")
        await _task(pool, project_id, "task-deep", work_dir=str(leaf.parent), title="deep", status="running")
        _one(await _select({"cwd": str(leaf)}), "task-deep")

    run_db(check, monkeypatch)


def test_one_open_task_in_a_directory_is_selected(monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        pair = tmp_path / "pair"
        pair.mkdir()
        await _task(pool, project_id, "task-open", work_dir=str(pair), title="open", status="running")
        await _task(pool, project_id, "task-shut", work_dir=str(pair), title="shut", status="completed")
        now = datetime.now(timezone.utc)
        await _activity(pool, "task-open", now - timedelta(days=40))
        await _activity(pool, "task-shut", now - timedelta(days=31))
        _one(await _select({"cwd": str(pair)}), "task-open")

    run_db(check, monkeypatch)


def test_ended_tasks_in_a_directory_stay_candidates(monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        both = tmp_path / "both"
        both.mkdir()
        await _task(pool, project_id, "task-endnew", work_dir=str(both), title="newer", status="completed")
        await _task(pool, project_id, "task-endold", work_dir=str(both), title="older", status="completed")
        now = datetime.now(timezone.utc)
        await _activity(pool, "task-endnew", now - timedelta(days=31))
        await _activity(pool, "task-endold", now - timedelta(days=40))
        _candidates(await _select({"cwd": str(both)}), ["task-endnew", "task-endold"])

    run_db(check, monkeypatch)


def test_directory_lookup_uses_the_canonical_indexes(monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        root = tmp_path / "indexed"
        root.mkdir()
        await _task(pool, project_id, "task-indexed", work_dir=str(root), workspace_dir=str(root),
                    title="indexed", status="running")
        await _worktree(pool, "task-indexed", str(root))
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL enable_seqscan = off")
            rows = await conn.fetch("EXPLAIN " + domain._DIRECTORY_HITS, domain._ancestors(str(root)))
        plan = "\n".join(row["QUERY PLAN"] for row in rows)
        for index in ("idx_tasks_select_work_dir_real", "idx_tasks_select_workspace_dir_real",
                      "idx_task_worktrees_select_path_real", "idx_tasks_select_work_dir_recheck",
                      "idx_tasks_select_workspace_dir_recheck", "idx_task_worktrees_select_path_recheck"):
            assert index in plan
        assert "Seq Scan" not in plan

    run_db(check, monkeypatch)


def test_directory_candidates_are_capped(monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        many = tmp_path / "many"
        many.mkdir()
        ids = [f"task-m{index:011x}" for index in range(1, 12)]
        for task_id in ids:
            await _task(pool, project_id, task_id, work_dir=str(many), title="many", status="completed")
        await _stamp(pool, ids)
        _candidates(await _select({"cwd": str(many)}), ids[:10], more=1)

    run_db(check, monkeypatch)


def test_one_recent_task_stays_a_candidate(monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        cwd = tmp_path / "nowhere"
        cwd.mkdir()
        await _task(pool, project_id, "task-000000000001", title="recent", status="running")
        await _activity(pool, "task-000000000001", datetime.now(timezone.utc) - timedelta(hours=1))
        found = await _select({"cwd": str(cwd)})
        _candidates(found, ["task-000000000001"])
        assert found["kind"] != "one"

    run_db(check, monkeypatch)


def test_recent_list_is_capped_at_twenty(monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        cwd = tmp_path / "nowhere"
        cwd.mkdir()
        now = datetime.now(timezone.utc)
        hour = now - timedelta(hours=1)
        for index in range(1, 21):
            task_id = f"task-{index:012x}"
            await _task(pool, project_id, task_id, title=f"recent {index}", status="running")
            await _activity(pool, task_id, hour)
        await _task(pool, project_id, "task-recentend", title="ended recent", status="completed")
        await _activity(pool, "task-recentend", now - timedelta(minutes=1))
        shown = ["task-recentend", *[f"task-{index:012x}" for index in range(1, 20)]]
        listed = await _select({"selection": "   ", "cwd": str(cwd)})
        _candidates(listed, shown, more=1)
        assert "task-000000000014" not in _ids(listed)

    run_db(check, monkeypatch)


def test_recent_window_excludes_older_activity(monkeypatch, tmp_path):
    async def check(pool):
        project_id = await _project(pool)
        cwd = tmp_path / "nowhere"
        cwd.mkdir()
        await _task(pool, project_id, "task-0000000000aa", title="old", status="running")
        await _backdate(pool, ["task-0000000000aa"], "20 days")
        _none(await _select({"cwd": str(cwd)}), ["last_activity_at"], 14, False, _HINT_RECENT)

    run_db(check, monkeypatch)



def test_task_id_hit_ignores_the_activity_window(monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        await _task(pool, project_id, "task-abc123def456", title="named", status="running")
        await _backdate(pool, ["task-abc123def456"], "200 days")
        found = await _select({"selection": "TASK-ABC123DEF456"})
        _one(found, "task-abc123def456")
        assert found["task"]["group"] == "in_progress" and found["task"]["status"] == "running"

    run_db(check, monkeypatch)


@pytest.mark.parametrize("see_all", [False, True])
def test_task_id_miss_reports_the_searched_fields(see_all, monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        await _task(pool, project_id, "task-abc123def456", title="named", status="running")
        _none(await _select({"selection": "task-000000000000", "all": see_all}),
              ["task_id"], None, see_all, _HINT_ID)

    run_db(check, monkeypatch)


async def _mr_world(pool):
    project_id = await _project(pool)
    await _task(pool, project_id, "task-aaa", title="wait", requirement="work", status="running")
    await _task(pool, project_id, "task-bbb", title="note", requirement="please review !12", status="running")
    await _task(pool, project_id, "task-ccc", title="link",
                requirement="see https://github.com/other/repo/pull/12 please", status="running")
    await _wait(pool, "task-aaa", 12, repo="Owner/Name")
    now = datetime.now(timezone.utc)
    await _activity(pool, "task-bbb", now)
    await _activity(pool, "task-aaa", now - timedelta(hours=2))
    await _activity(pool, "task-ccc", now - timedelta(hours=1))


@pytest.mark.parametrize("selection,ids,absent", [
    ("https://github.com/owner/name/pull/12", ["task-aaa", "task-bbb"], ["task-ccc"]),
    ("12", ["task-bbb", "task-ccc", "task-aaa"], []),
    ("https://github.com/other/repo/pull/12", ["task-ccc", "task-bbb"], ["task-aaa"]),
])
def test_mr_repo_narrows_the_same_number(selection, ids, absent, monkeypatch):
    async def check(pool):
        await _mr_world(pool)
        found = await _select({"selection": selection})
        _candidates(found, ids)
        assert all(task_id not in _ids(found) for task_id in absent)

    run_db(check, monkeypatch)


def test_mr_prose_number_is_not_a_reference(monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        await _task(pool, project_id, "task-ddd", title="prose", requirement="the count is 12 today", status="running")
        _none(await _select({"selection": "12"}),
              ["github_pr", "title", "requirement", "original_user_text", "supplements"], 90, False, _HINT_TEXT)

    run_db(check, monkeypatch)


@pytest.mark.parametrize("field,text,selection", [
    ("requirement", "please review !12", "12"),
    ("original_user_text", "see #78", "#78"),
    ("supplement", "ship !77", "77"),
])
def test_mr_reference_in_one_field(field, text, selection, monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        fields = {"title": "work", "requirement": "work", "status": "running"}
        if field != "supplement":
            fields[field] = text
        await _task(pool, project_id, "task-ref", **fields)
        if field == "supplement":
            await add_supplement("task-ref", text, via="dashboard", pool=pool)
        _one(await _select({"selection": selection}), "task-ref")

    run_db(check, monkeypatch)


@pytest.mark.parametrize("left,right", [
    ("１２", "００１２"),
    ("١٢", "0012"),
    ("٠٠١٢", "１２"),
    ("\U0001E951\U0001E952", "１２"),
])
def test_unicode_wait_numbers_stay_ambiguous(left, right, monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        await _task(pool, project_id, "task-00000000000a", title="left", requirement="work", status="running")
        await _task(pool, project_id, "task-00000000000b", title="right", requirement="work", status="running")
        await _task(pool, project_id, "task-00000000000c", title="other", requirement="work", status="running")
        await _wait(pool, "task-00000000000a", left)
        await _wait(pool, "task-00000000000b", right)
        await _wait(pool, "task-00000000000c", 13)
        await _stamp(pool, ["task-00000000000a", "task-00000000000b", "task-00000000000c"])
        found = await _select({"selection": "12"})
        _candidates(found, ["task-00000000000a", "task-00000000000b"])
        assert found["kind"] != "one" and "task-00000000000c" not in _ids(found)

    run_db(check, monkeypatch)


def test_text_requires_every_token(monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        await _task(pool, project_id, "task-alpha", title="alpha", requirement="unrelated", status="running")
        await _task(pool, project_id, "task-both", title="alpha beta case", requirement="work", status="running")
        _one(await _select({"selection": "alpha beta"}), "task-both")
        assert "task-alpha" not in _ids(await _select({"selection": "alpha beta"}))

    run_db(check, monkeypatch)


def test_text_ranks_title_before_requirement(monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        await _task(pool, project_id, "task-title", title="red blue open", requirement="work", status="running")
        await _task(pool, project_id, "task-ended", title="red blue ended", requirement="work", status="completed")
        await _task(pool, project_id, "task-req", title="red", requirement="blue note", status="running")
        now = datetime.now(timezone.utc)
        await _activity(pool, "task-title", now - timedelta(days=3))
        await _activity(pool, "task-ended", now - timedelta(days=1))
        await _activity(pool, "task-req", now)
        _candidates(await _select({"selection": "red blue"}), ["task-title", "task-ended", "task-req"])

    run_db(check, monkeypatch)


@pytest.mark.parametrize("see_all,ids", [
    (False, ["task-act"]),
    (True, ["task-act", "task-old"]),
])
def test_text_window_follows_activity(see_all, ids, monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        await _task(pool, project_id, "task-act", title="unique needle", requirement="work", status="running")
        await _task(pool, project_id, "task-old", title="unique needle", requirement="work", status="running")
        now = datetime.now(timezone.utc)
        await _backdate(pool, ["task-act"], "100 days")
        await _activity(pool, "task-act", now - timedelta(days=1))
        await _activity(pool, "task-old", now - timedelta(days=100))
        await pool.execute("UPDATE tasks SET updated_at = now() - interval '1 day' WHERE id = 'task-old'")
        found = await _select({"selection": "unique needle", "all": see_all})
        if see_all:
            _candidates(found, ids)
        else:
            _one(found, ids[0])

    run_db(check, monkeypatch)


def test_text_percent_and_underscore_are_literal(monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        await _task(pool, project_id, "task-wild", title="100%_done", requirement="work", status="running")
        await _task(pool, project_id, "task-like", title="100XXdone", requirement="work", status="running")
        found = await _select({"selection": "100%_done"})
        _one(found, "task-wild")
        assert found["task"]["task_id"] != "task-like"

    run_db(check, monkeypatch)


def test_text_ambiguity_lists_both_candidates(monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        ids = ["task-pair-a", "task-pair-b"]
        for task_id in ids:
            await _task(pool, project_id, task_id, title="amber quartz", status="running")
        await _stamp(pool, ids)
        _candidates(await _select({"selection": "amber quartz"}), ids)

    run_db(check, monkeypatch)


def test_text_candidates_are_capped_with_exact_more(monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        ids = [f"task-{index:012x}" for index in range(1, 12)]
        for task_id in ids:
            await _task(pool, project_id, task_id, title="shared token", requirement="other", status="running")
        await _stamp(pool, ids)
        _candidates(await _select({"selection": "shared token"}), ids[:10], more=1)

    run_db(check, monkeypatch)


@pytest.mark.parametrize("see_all", [False, True])
def test_text_miss_reports_the_searched_fields(see_all, monkeypatch):
    async def check(pool):
        project_id = await _project(pool)
        await _task(pool, project_id, "task-other", title="unrelated", status="running")
        _none(await _select({"selection": "zzzz-not-present", "all": see_all}),
              ["title", "requirement"], None if see_all else 90, see_all, _HINT_TEXT)

    run_db(check, monkeypatch)


def test_operator_only(monkeypatch, tmp_path):
    async def check(pool):
        operation = domain.OPERATIONS[0]
        assert operation.operator_only is True and operation.tool is None
        assert operation.risk == "read" and operation.level is ActionLevel.INSIDE and operation.cli is False
        async with v1_api(tmp_path, pool) as call:
            status, body = await call("tasks.select", {"selection": "alpha"}, client=True)
        assert status == 403 and body["error"]["code"] == "forbidden"
        assert body["error"]["message"] == "operator identity is required"

    run_db(check, monkeypatch)


def test_two_open_tasks_in_the_deepest_directory_stay_candidates(monkeypatch, tmp_path):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        root, sub, leaf = tmp_path / "proj", tmp_path / "proj" / "sub", tmp_path / "proj" / "sub" / "leaf"
        leaf.mkdir(parents=True)
        await _task(pool, project_id, "task-shallow", work_dir=str(root), title="shallow", status="running")
        await _task(pool, project_id, "task-open-a", work_dir=str(sub), title="open a", status="running")
        await _task(pool, project_id, "task-open-b", work_dir=str(sub), title="open b", status="running")
        await _stamp(pool, ["task-shallow", "task-open-a", "task-open-b"])
        found = await _select({"cwd": str(leaf)})
        _candidates(found, ["task-open-a", "task-open-b"])
        assert found["kind"] != "one" and "task-shallow" not in _ids(found)

    run_db(check, monkeypatch)


@pytest.mark.parametrize("titles, kind, ids", [
    (("ＡＢＣ", "abc"), "candidates", ["task-00000000000a", "task-00000000000b"]),
    (("ＡＢＣ",), "one", ["task-00000000000a"]),
])
def test_normalized_text_stays_ambiguous(titles, kind, ids, monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        task_ids = ["task-00000000000a", "task-00000000000b"][:len(titles)]
        for task_id, title in zip(task_ids, titles):
            await _task(pool, project_id, task_id, title=title, requirement="other", status="running")
        await _stamp(pool, task_ids)
        found = await _select({"selection": "abc"})
        if kind == "one":
            _one(found, ids[0])
        else:
            _candidates(found, ids)
            assert found["kind"] != "one"

    run_db(check, monkeypatch)


@pytest.mark.parametrize("where", ["title", "requirement", "original_user_text", "supplement"])
def test_fullwidth_mr_reference_matches(where, monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        fields = {"title": "work", "requirement": "work", "status": "running"}
        if where in {"title", "requirement", "original_user_text"}:
            fields[where] = "请看！１２"
        await _task(pool, project_id, "task-fwmr", **fields)
        if where == "supplement":
            await add_supplement("task-fwmr", "请看！１２", via="dashboard", pool=pool)
        _one(await _select({"selection": "12"}), "task-fwmr")

    run_db(check, monkeypatch)


@pytest.mark.parametrize("requirement, selection, kind", [
    ("review !12 https://[/pull/12", "12", "one"),
    ("see https://[/pull/12 only", "12", "none"),
])
def test_malformed_stored_url_does_not_abort_mr_search(requirement, selection, kind, monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        await _task(pool, project_id, "task-badurl", title="work", requirement=requirement, status="running")
        found = await _select({"selection": selection})
        if kind == "one":
            _one(found, "task-badurl")
        else:
            _none(found, ["github_pr", "title", "requirement", "original_user_text", "supplements"],
                  90, False, _HINT_TEXT)

    run_db(check, monkeypatch)


@pytest.mark.parametrize("selection, see_all, kind, task_id", [
    ("12", False, "none", None),
    ("12", True, "one", "task-oldmr"),
    ("424242", False, "none", None),
    ("44", False, "one", "task-titlemr"),
])
def test_mr_window_all_miss_and_title(selection, see_all, kind, task_id, monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        await _task(pool, project_id, "task-oldmr", title="old", requirement="review !12", status="running")
        await _task(pool, project_id, "task-titlemr", title="ship !44", requirement="work", status="running")
        await _backdate(pool, ["task-oldmr"], "100 days")
        found = await _select({"selection": selection, "all": see_all})
        if kind == "one":
            _one(found, task_id)
        else:
            _none(found, ["github_pr", "title", "requirement", "original_user_text", "supplements"],
                  None if see_all else 90, see_all, _HINT_TEXT)

    run_db(check, monkeypatch)


def test_no_model_call(monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        await _task(pool, project_id, "task-needle", title="needle", requirement="needle", status="running")

        def boom(*_args, **_kwargs):
            raise AssertionError("model called")

        monkeypatch.setattr("looper.agents.model_call.call", boom)
        result = await _select({"selection": "needle"})
        assert result["kind"] == "one" and result["task"]["task_id"] == "task-needle"

    run_db(check, monkeypatch)
