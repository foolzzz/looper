"""tasks.select against a disposable database: directory, id, merge request, text, ambiguity."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents.action_levels import ActionLevel
from looper.agents.ops import ErrorCode, OpError
from looper.agents.v1_domains import task_select as domain
from looper.db import projects, tasks
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


def test_empty_selection(monkeypatch, tmp_path):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        root, sub = tmp_path / "proj", tmp_path / "proj" / "sub"
        leaf, target, alias = sub / "leaf", tmp_path / "target", tmp_path / "alias"
        space, live, gone = tmp_path / "space", tmp_path / "live", tmp_path / "gone"
        pair, both, many, other, nowhere = (tmp_path / "pair", tmp_path / "both", tmp_path / "many",
                                             tmp_path / "proj-other", tmp_path / "nowhere")
        for path in (leaf, target, space, live, gone, pair, both, many, other, nowhere):
            path.mkdir(parents=True)
        alias.symlink_to(target, target_is_directory=True)

        await _task(pool, project_id, "task-shallow", work_dir=str(root), title="shallow", status="running")
        await _task(pool, project_id, "task-deep", work_dir=str(sub), title="deep", status="running")
        await _task(pool, project_id, "task-link", work_dir=str(alias), title="link", status="running")
        await _task(pool, project_id, "task-space", workspace_dir=str(space), title="space", status="running")
        await _task(pool, project_id, "task-tree", title="tree", status="running")
        await _task(pool, project_id, "task-gone", title="gone", status="completed")
        await _task(pool, project_id, "task-proj", work_dir=str(root), title="proj", status="running")
        await _task(pool, project_id, "task-open", work_dir=str(pair), title="open", status="running")
        await _task(pool, project_id, "task-shut", work_dir=str(pair), title="shut", status="completed")
        await _task(pool, project_id, "task-endnew", work_dir=str(both), title="newer", status="completed")
        await _task(pool, project_id, "task-endold", work_dir=str(both), title="older", status="completed")
        await _task(pool, project_id, "task-empty", title="empty", status="running")
        many_ids = [f"task-m{index:011x}" for index in range(1, 12)]
        for task_id in many_ids:
            await _task(pool, project_id, task_id, work_dir=str(many), title="many", status="completed")
        directory_ids = ["task-shallow", "task-deep", "task-link", "task-space", "task-tree", "task-gone",
                         "task-proj", "task-open", "task-shut", "task-endnew", "task-endold", "task-empty", *many_ids]
        await _backdate(pool, directory_ids, "30 days")
        await pool.execute(
            "INSERT INTO task_worktrees (task_id, repo_dir, path, branch, base_commit) VALUES ($1, $2, $2, 'task', 'abc')",
            "task-tree", str(live))
        await pool.execute(
            "INSERT INTO task_worktrees (task_id, repo_dir, path, branch, base_commit, state, removed_at) "
            "VALUES ($1, $2, $2, 'task', 'abc', 'removed', now())", "task-gone", str(gone))
        now = datetime.now(timezone.utc)
        await _activity(pool, "task-open", now - timedelta(days=40))
        await _activity(pool, "task-shut", now - timedelta(days=31))
        await _activity(pool, "task-endnew", now - timedelta(days=31))
        await _activity(pool, "task-endold", now - timedelta(days=40))

        with pytest.raises(OpError) as raised:
            await _select({"cwd": "relative/path"})
        assert raised.value.code is ErrorCode.INVALID and str(raised.value) == "cwd must be an absolute path"

        assert (await _select({"cwd": str(nowhere)}))["kind"] == "none"
        _none(await _select({"selection": "", "cwd": str(nowhere), "all": True}),
              ["last_activity_at"], 14, True, _HINT_RECENT)
        _none(await _select({"cwd": str(other)}), ["last_activity_at"], 14, False, _HINT_RECENT)
        _none(await _select({"cwd": str(gone)}), ["last_activity_at"], 14, False, _HINT_RECENT)

        found = await _select({"selection": '""', "cwd": str(leaf)})
        _one(found, "task-deep")
        assert found["task"]["group"] == "in_progress" and found["task"]["project_name"] == "select"
        assert found["task"]["last_activity_at"]
        _one(await _select({"cwd": str(target)}), "task-link")
        _one(await _select({"cwd": str(space)}), "task-space")
        _one(await _select({"cwd": str(live)}), "task-tree")
        _one(await _select({"cwd": str(pair)}), "task-open")
        _candidates(await _select({"cwd": str(both)}), ["task-endnew", "task-endold"])
        _candidates(await _select({"cwd": str(many)}), many_ids[:10], more=1)

        await _task(pool, project_id, "task-000000000001", title="recent", status="running")
        hour = now - timedelta(hours=1)
        await _activity(pool, "task-000000000001", hour)
        alone = await _select({"cwd": str(nowhere)})
        _candidates(alone, ["task-000000000001"])
        assert alone["kind"] != "one"

        for index in range(2, 21):
            task_id = f"task-{index:012x}"
            await _task(pool, project_id, task_id, title=f"recent {index}", status="running")
            await _activity(pool, task_id, hour)
        await _task(pool, project_id, "task-recentend", title="ended recent", status="completed")
        await _activity(pool, "task-recentend", now - timedelta(minutes=1))
        await _task(pool, project_id, "task-0000000000aa", title="old", status="running")
        await _backdate(pool, ["task-0000000000aa"], "20 days")

        shown = ["task-recentend", *[f"task-{index:012x}" for index in range(1, 20)]]
        listed = await _select({"selection": "   ", "cwd": str(nowhere)})
        _candidates(listed, shown, more=1)
        assert "task-000000000014" not in _ids(listed) and "task-0000000000aa" not in _ids(listed)
        assert "task-deep" not in _ids(listed)

    run_db(check, monkeypatch)


def test_task_id(monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        await _task(pool, project_id, "task-abc123def456", title="named", status="running")
        await _backdate(pool, ["task-abc123def456"], "200 days")
        found = await _select({"selection": "TASK-ABC123DEF456"})
        _one(found, "task-abc123def456")
        assert found["task"]["group"] == "in_progress" and found["task"]["status"] == "running"
        _none(await _select({"selection": "task-000000000000"}), ["task_id"], None, False, _HINT_ID)
        _none(await _select({"selection": "task-000000000000", "all": True}), ["task_id"], None, True, _HINT_ID)

    run_db(check, monkeypatch)


def test_mr_matches_references_not_bare_numbers(monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        await _task(pool, project_id, "task-aaa", title="wait", requirement="work", status="running")
        await _task(pool, project_id, "task-bbb", title="note", requirement="please review !12", status="running")
        await _task(pool, project_id, "task-ccc", title="link",
                    requirement="see https://github.com/other/repo/pull/12 please", status="running")
        await _task(pool, project_id, "task-ddd", title="prose", requirement="the count is 12 today", status="running")
        await _task(pool, project_id, "task-eee", title="extra", requirement="work", status="running")
        await _task(pool, project_id, "task-fff", title="origin", requirement="work",
                    original_user_text="see #78", status="running")
        await add_supplement("task-eee", "ship !77", via="dashboard", pool=pool)
        await pool.execute("INSERT INTO task_steps (id, task_id, step_index, instruction) VALUES ('step-a', 'task-aaa', 0, 'wait')")
        spec = json.dumps({"check": {"type": "github_pr", "repo": "Owner/Name", "number": 12, "ready_when": "merged"}})
        await pool.execute(
            "INSERT INTO task_waits (step_id, task_id, topic, spec) VALUES ('step-a', 'task-aaa', 'pr', $1::jsonb)", spec)
        now = datetime.now(timezone.utc)
        await _activity(pool, "task-bbb", now)
        await _activity(pool, "task-aaa", now - timedelta(hours=2))
        await _activity(pool, "task-ccc", now - timedelta(hours=1))

        url = await _select({"selection": "https://github.com/owner/name/pull/12"})
        _candidates(url, ["task-aaa", "task-bbb"])
        assert "task-ccc" not in _ids(url) and "task-ddd" not in _ids(url)

        bare = await _select({"selection": "12"})
        _candidates(bare, ["task-bbb", "task-ccc", "task-aaa"])
        assert "task-ddd" not in _ids(bare)

        other = await _select({"selection": "https://github.com/other/repo/pull/12"})
        _candidates(other, ["task-ccc", "task-bbb"])
        assert "task-aaa" not in _ids(other) and "task-ddd" not in _ids(other)

        _one(await _select({"selection": "77"}), "task-eee")
        _one(await _select({"selection": "#78"}), "task-fff")

    run_db(check, monkeypatch)


def test_text_matches_every_token_and_ranks(monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        await _task(pool, project_id, "task-alpha", title="alpha", requirement="unrelated", status="running")
        await _task(pool, project_id, "task-both", title="alpha beta case", requirement="work", status="running")
        await _task(pool, project_id, "task-title", title="red blue open", requirement="work", status="running")
        await _task(pool, project_id, "task-ended", title="red blue ended", requirement="work", status="completed")
        await _task(pool, project_id, "task-req", title="red", requirement="blue note", status="running")
        await _task(pool, project_id, "task-wild", title="100%_done", requirement="work", status="running")
        await _task(pool, project_id, "task-like", title="100XXdone", requirement="work", status="running")
        await _task(pool, project_id, "task-act", title="unique needle", requirement="work", status="running")
        await _task(pool, project_id, "task-old", title="unique needle", requirement="work", status="running")
        now = datetime.now(timezone.utc)
        await _activity(pool, "task-title", now - timedelta(days=3))
        await _activity(pool, "task-ended", now - timedelta(days=1))
        await _activity(pool, "task-req", now)
        await _backdate(pool, ["task-act"], "100 days")
        await _activity(pool, "task-act", now - timedelta(days=1))
        await _activity(pool, "task-old", now - timedelta(days=100))
        await pool.execute("UPDATE tasks SET updated_at = now() - interval '1 day' WHERE id = 'task-old'")

        _one(await _select({"selection": "alpha beta"}), "task-both")
        assert "task-alpha" not in _ids(await _select({"selection": "alpha beta"}))
        _candidates(await _select({"selection": "red blue"}), ["task-title", "task-ended", "task-req"])
        _one(await _select({"selection": "unique needle"}), "task-act")
        _candidates(await _select({"selection": "unique needle", "all": True}), ["task-act", "task-old"])
        _one(await _select({"selection": "100%_done"}), "task-wild")
        assert (await _select({"selection": "100%_done"}))["task"]["task_id"] != "task-like"

    run_db(check, monkeypatch)


def test_ambiguity_lists_candidates_and_says_when_none(monkeypatch):
    async def check(pool):
        project_id = (await projects.create_project(name="select", pool=pool))["id"]
        stamp = datetime(2026, 11, 1, tzinfo=timezone.utc)
        await _task(pool, project_id, "task-pair-a", title="amber quartz", status="running")
        await _task(pool, project_id, "task-pair-b", title="amber quartz", status="running")
        await pool.execute("UPDATE tasks SET updated_at = $2 WHERE id = ANY($1::text[])",
                           ["task-pair-a", "task-pair-b"], stamp)
        pair = await _select({"selection": "amber quartz"})
        _candidates(pair, ["task-pair-a", "task-pair-b"])
        assert pair["kind"] != "one"

        ids = [f"task-{index:012x}" for index in range(1, 12)]
        for task_id in ids:
            await _task(pool, project_id, task_id, title="shared token", requirement="other", status="running")
        await pool.execute("UPDATE tasks SET updated_at = $2 WHERE id = ANY($1::text[])", ids, stamp)
        listed = await _select({"selection": "shared token"})
        _candidates(listed, ids[:10], more=1)
        assert ids[-1] not in _ids(listed)

        _none(await _select({"selection": "zzzz-not-present"}),
              ["title", "requirement"], 90, False, _HINT_TEXT)
        _none(await _select({"selection": "zzzz-not-present", "all": True}),
              ["title", "requirement"], None, True, _HINT_TEXT)

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
