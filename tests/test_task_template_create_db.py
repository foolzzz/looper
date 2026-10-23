"""Creating a task from a Manager's task template, on a real PostgreSQL: the tool's schema variant, what the
template fixes, the merged placement, the role templates copied into the task, and every refusal leaving no task.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from looper.agents.managers_api import ManagerDefinition
from looper.agents.ops.service import scope_for
from looper.agents.ops.tasks import TOOLS, task_create_tool
from tests.db_support import DSN, run_db
from tests.test_managers_schema_db import _manager
from tests.test_ops_tasks_db import _Chat, _error, _project
from tests.test_task_agents_db import _profiles

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

MANAGER = "manager-review"
LINK = "https://github.com/o/r/pull/7"
PR_INPUT = {"key": "pr_url", "label": "Pull request link", "kind": "url",
            "pattern": r"https://github\.com/[^/]+/[^/]+/pull/[0-9]+", "hint": "https://github.com/<o>/<r>/pull/<n>"}


async def _setup(pool: Any, **manager: Any) -> None:
    await _profiles(pool)
    await _manager(pool, "review", **manager)


async def _template(key: str = "pr_review", **values: Any) -> None:
    from looper.db import managers

    row = {"name": "PR review", "description": "Review a pull request.",
           "requirement": "Review the pull request {{pr_url}}.", "planner_instructions": "Use gh.",
           "inputs": [PR_INPUT], **values}
    assert await managers.put_template(MANAGER, key, row) is not None


async def _chat(pool: Any, tmp: Path) -> _Chat:
    from looper.db import managers

    row = await managers.get_manager(MANAGER, pool=pool)
    keys = [t["key"] for t in await managers.list_templates(MANAGER)]
    chat = _Chat(tmp)
    from looper.agents.ops.service import ManagerOps

    chat.ops = ManagerOps(chat.ops.platform, url="http://127.0.0.1:9", token="tok",
                          scope=scope_for(ManagerDefinition.from_row(row), keys))
    return chat


def _create(**extra: Any) -> tuple[str, dict[str, Any]]:
    return "looper_task_create", {"requirement": "Focus on tests.", "title": "Review PR 7",
                                  "template": "pr_review", "inputs": {"pr_url": LINK}, **extra}


async def _task_ids(pool: Any) -> list[str]:
    return [r["id"] for r in await pool.fetch("SELECT id FROM tasks")]


async def _refused(pool: Any, chat: _Chat, code: str, message: str, *calls: tuple[str, dict[str, Any]]) -> None:
    before = await _task_ids(pool)
    (answer,) = await chat.say("确认", *calls)
    assert answer["status"] == "error", answer
    assert answer["error"]["code"] == code and message in answer["error"]["message"], answer
    assert await _task_ids(pool) == before


def test_the_schema_variant_only_for_a_manager_with_templates() -> None:
    original = next(tool for tool in TOOLS if tool.name == "looper_task_create")
    assert task_create_tool([]) is original
    tool = task_create_tool(["pr_review", "bugfix"])
    assert tool.schema["required"] == ["requirement", "title"]
    assert tool.schema["properties"]["template"]["enum"] == ["pr_review", "bugfix"]
    assert tool.schema["properties"]["inputs"]["type"] == "object"
    assert tool.description.endswith(" With a template, give its inputs; the template sets the project unless it "
                                     "has none.")
    assert original.schema["required"] == ["requirement", "title", "project"]
    assert "template" not in original.schema["properties"]


def test_listing_of_a_manager_without_templates_is_the_original(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _setup(pool)
        chat = await _chat(pool, tmp_path)
        listed = next(t for t in chat.ops.listing() if t["name"] == "looper_task_create")
        original = next(t for t in TOOLS if t.name == "looper_task_create").listing()
        assert json.dumps(listed, sort_keys=True) == json.dumps(original, sort_keys=True)
        await _template()
        chat = await _chat(pool, tmp_path)
        listed = next(t for t in chat.ops.listing() if t["name"] == "looper_task_create")
        assert listed["inputSchema"]["properties"]["template"]["enum"] == ["pr_review"]
        assert "project" not in listed["inputSchema"]["required"]

    run_db(test, monkeypatch)


def test_a_task_from_a_template_on_a_fixed_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        await _setup(pool, task_policy="templates_only")
        project = await _project(pool, "web-app")
        await _template(project_id=project["id"])
        chat = await _chat(pool, tmp_path)
        (done,) = await chat.say("确认", _create())
        assert done["status"] == "ok", done
        task = await tasks.get_task(done["result"]["task_id"], pool=pool)
        assert task["manager_id"] == MANAGER and task["project_id"] == project["id"]
        assert task["template"]["key"] == "pr_review" and task["template"]["inputs"][0]["value"] == LINK
        assert task["requirement"].startswith(f"Review the pull request {LINK}.\n\nFocus on tests.")
        assert "模板：PR review（Pull request link：" in chat.turns[0].blocks[-1]

    run_db(test, monkeypatch)


def test_refusals_write_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _setup(pool, task_policy="templates_only")
        project = await _project(pool, "web-app", "/w")
        await _project(pool, "other", "/o")
        reviewer = await pool.fetchval("SELECT id FROM agent_profiles WHERE id = 'agent-sonnet'")
        await _template(project_id=project["id"], work_dir="/w", workspace_mode="worktree",
                        reviewer_profile_id=reviewer)
        await _template("slow", name="Slow", requirement="{{x}}",
                        inputs=[{"key": "x", "label": "Thing", "kind": "text", "pattern": "(a+)+$"}])
        chat = await _chat(pool, tmp_path)
        plain = {"requirement": "r", "title": "t", "project": "web-app"}
        await _refused(pool, chat, "invalid", "only from its templates (pr_review, slow)",
                       ("looper_task_create", plain))
        await _refused(pool, chat, "invalid", "template must be one of: pr_review, slow", _create(template="nope"))
        # Deleted after the turn's section was written: the scope still lists it, the read does not find it.
        await pool.execute("INSERT INTO manager_task_templates (manager_id, key, name) VALUES ($1, 'gone', 'Gone')",
                           MANAGER)
        stale = await _chat(pool, tmp_path)
        await pool.execute("DELETE FROM manager_task_templates WHERE key = 'gone'")
        await _refused(pool, stale, "invalid", "no template gone; this Manager's templates are: pr_review, slow",
                       _create(template="gone"))
        await _refused(pool, chat, "invalid", "ask the user", _create(inputs={}))
        await _refused(pool, chat, "invalid", "Pull request link", _create(inputs={"pr_url": "http://example.com/x"}))
        await _refused(pool, chat, "invalid", "inputs go with a template",
                       ("looper_task_create", {**plain, "inputs": {"a": "b"}}))
        started = time.monotonic()
        await _refused(pool, chat, "invalid", "took too long",
                       _create(template="slow", inputs={"x": "a" * 64 + "!"}))
        assert time.monotonic() - started < 3
        await _refused(pool, chat, "invalid", "sets project to web-app", _create(project="other"))
        await _refused(pool, chat, "invalid", "sets work_dir to /w", _create(work_dir="/o"))
        await _refused(pool, chat, "invalid", "sets workspace_mode to worktree", _create(workspace_mode="in_place"))
        other = "agent-other"
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                           "SELECT $1, $1, runtime_id, model_id FROM agent_profiles WHERE id = $2", other, reviewer)
        await _refused(pool, chat, "invalid", "Agent Profile", _create(profiles={"reviewer": other}))

    run_db(test, monkeypatch)


def test_the_same_values_are_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        await _setup(pool)
        project = await _project(pool, "Web-App", "/w")
        await _template(project_id=project["id"], work_dir="/w", workspace_mode="in_place",
                        reviewer_profile_id="agent-sonnet")
        chat = await _chat(pool, tmp_path)
        (done,) = await chat.say("确认", _create(project="web-app", work_dir="/w/", workspace_mode="in_place",
                                              profiles={"reviewer": "agent-sonnet"}))
        assert done["status"] == "ok", done
        task = await tasks.get_task(done["result"]["task_id"], pool=pool)
        assert (task["project_id"], task["work_dir"], task["workspace_mode"]) == (project["id"], "/w", "in_place")

    run_db(test, monkeypatch)


async def _role_template(pool: Any, project_id: str, key: str = "security-review") -> None:
    from looper.db import role_templates

    await role_templates.put_template(project_id, key, {"kind": "review", "role_prompt": "Look for holes."}, pool=pool)


def test_role_templates_are_copied_into_the_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _setup(pool)
        project = await _project(pool, "web-app", "/w")
        await pool.execute("UPDATE projects SET planner_roles = 'on' WHERE id = $1", project["id"])
        await _role_template(pool, project["id"])
        await _template(project_id=project["id"], role_templates=["security-review"])
        chat = await _chat(pool, tmp_path)
        (done,) = await chat.say("确认", _create())
        assert done["status"] == "ok", done
        task_id = done["result"]["task_id"]
        role = await pool.fetchrow("SELECT * FROM task_roles WHERE task_id = $1", task_id)
        assert (role["role_key"], role["version"], role["via"], role["template_key"]) == \
            ("security-review", 1, "template", "security-review")
        entry = await pool.fetchrow("SELECT * FROM task_journal WHERE task_id = $1 AND kind = 'role_defined'",
                                    task_id)
        assert "security-review v1 from the task template PR review" in entry["summary"]
        snapshot = json.loads(await pool.fetchval("SELECT template FROM tasks WHERE id = $1", task_id))
        assert snapshot["roles"] == [{"role_key": "security-review", "kind": role["kind"],
                                      "profile_id": role["profile_id"], "role_prompt": role["role_prompt"],
                                      "harness": role["harness"], "items": list(role["items"]),
                                      "template_key": "security-review"}]

    run_db(test, monkeypatch)


def test_role_template_failures_write_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _setup(pool)
        project = await _project(pool, "web-app", "/w")
        await _role_template(pool, project["id"])
        await _template(project_id=project["id"], role_templates=["security-review"])
        chat = await _chat(pool, tmp_path)
        await _refused(pool, chat, "conflict", "Planner-defined roles off", _create())
        await pool.execute("UPDATE projects SET planner_roles = 'on' WHERE id = $1", project["id"])
        await pool.execute("UPDATE manager_task_templates SET role_templates = '{security-review,security-review}'")
        await _refused(pool, chat, "conflict", "security-review twice", _create())
        await pool.execute("UPDATE manager_task_templates SET role_templates = '{security-review}'")
        await pool.execute("DELETE FROM project_role_templates")
        await _refused(pool, chat, "conflict", "no role template security-review", _create())

    run_db(test, monkeypatch)


def test_a_template_without_a_project_asks_before_a_new_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import projects, tasks

    async def test(pool: Any) -> None:
        await _setup(pool)
        await _template(work_dir="/srv/tpl")
        chat = await _chat(pool, tmp_path)
        (asked,) = await chat.say("确认", _create(project="Fresh"))
        assert asked["status"] == "needs_confirmation"
        assert "/srv/tpl" in chat.turns[0].blocks[0]
        (done,) = await chat.say("确认新建", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert done["status"] == "ok", done
        project = await projects.find_project_by_name("fresh", pool=pool)
        assert project["work_dir"] == "/srv/tpl"
        assert (await tasks.get_task(done["result"]["task_id"], pool=pool))["work_dir"] == "/srv/tpl"

    run_db(test, monkeypatch)


def test_the_placement_merges_the_template_and_the_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        await _setup(pool)
        fixed = await _project(pool, "web-app", "/w")
        await _project(pool, "q", "/d")
        await _template(project_id=fixed["id"])
        await _template("tpl-dir", name="Dir", work_dir="/t")
        chat = await _chat(pool, tmp_path)
        # (a) the project is the template's, the directory the call's
        (done,) = await chat.say("确认", _create(work_dir="/e"))
        assert (await tasks.get_task(done["result"]["task_id"], pool=pool))["work_dir"] == "/e"
        # (b) the directory is the template's, the project the call's
        (done,) = await chat.say("确认", _create(template="tpl-dir", project="q"))
        assert (await tasks.get_task(done["result"]["task_id"], pool=pool))["work_dir"] == "/t"

    run_db(test, monkeypatch)


def test_a_template_profile_that_cannot_run_its_role_any_more(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _setup(pool)
        project = await _project(pool, "web-app", "/w")
        await _template(project_id=project["id"], reviewer_profile_id="agent-sonnet")
        await pool.execute("UPDATE agent_runtimes SET capabilities = '{}' WHERE id = 'rt-one-off'")
        await pool.execute("UPDATE agent_profiles SET runtime_id = 'rt-one-off' WHERE id = 'agent-sonnet'")
        chat = await _chat(pool, tmp_path)
        await _refused(pool, chat, "conflict", "PR review's reviewer Agent Profile", _create())

    run_db(test, monkeypatch)


def test_a_deleted_manager_creates_no_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _setup(pool)
        project = await _project(pool, "web-app", "/w")
        await _template(project_id=project["id"])
        chat = await _chat(pool, tmp_path)
        await pool.execute("UPDATE managers SET enabled = false, deleted_at = now() WHERE id = $1", MANAGER)
        before = await _task_ids(pool)
        (answer,) = await chat.say("确认", _create())
        assert answer["status"] == "error", answer
        assert await _task_ids(pool) == before

    run_db(test, monkeypatch)


def test_the_manager_is_told_of_its_templates_only_when_it_has_some(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    from looper.agents.tiers.manager import TEMPLATES_CONTRACT, ManagerAgent, ManagerConfig

    async def test(pool: Any) -> None:
        from looper.db import managers

        await _setup(pool, task_policy="templates_only")
        project = await _project(pool, "web-app", "/w")
        row = await managers.get_manager(MANAGER, pool=pool)
        agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path, broadcaster=None,
                             definition=ManagerDefinition.from_row(row))
        assert await agent._templates_section() == [] and agent._has_templates is False
        await _template(project_id=project["id"])
        lines = await agent._templates_section()
        assert lines[0].startswith("Task templates (create tasks with one of these; template is its key):")
        assert lines[1] == "  - pr_review: PR review (project web-app): Review a pull request."
        assert lines[2].startswith('      inputs: pr_url "Pull request link" (required; https://')
        assert agent._has_templates is True
        text = TEMPLATES_CONTRACT.format(only=" and do not create a task")
        assert text.endswith("say so and do not create a task.")

    run_db(test, monkeypatch)
