"""Manager schedule template arguments, displayed approval and persistence on disposable PostgreSQL."""
from __future__ import annotations

import pytest

from looper.agents import schedules
from looper.agents.managers_api import ManagerDefinition
from looper.agents.ops.service import ManagerOps, scope_for
from looper.db import managers
from tests.db_support import DSN, run_db
from tests.test_manager_config_tools_db import _ok
from tests.test_manager_schedule_tools_db import _confirm, _starting_state
from tests.test_ops_tasks_db import _Chat, _error, _project

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
OWNER = "manager-reports"
TOOL = "looper_schedules"
VALUES = {"url": "https://example.org/report", "note": "Keep the totals"}
FIELDS = {"name": "report", "cron": "0 9 * * 1-5", "timezone": "UTC", "requirement": "Inspect sources",
          "template_key": "report", "template_inputs": VALUES}


async def _setup(pool, tmp_path):
    project = await _project(pool, work_dir=str(tmp_path))
    await pool.execute("INSERT INTO managers (id,name,system_prompt,tools,task_policy) "
                       "VALUES ($1,'Reports','Report sources','all','templates_only')", OWNER)
    await managers.put_template(OWNER, "report", {"name": "Source report", "inputs": [
        {"key": "url", "label": "Source", "kind": "url"},
        {"key": "note", "label": "Note", "required": False}]})
    await managers.put_template(OWNER, "empty", {"name": "Empty report"})
    chat = _Chat(tmp_path)
    chat.ops = ManagerOps(chat.ops.platform, url="http://127.0.0.1:9", token="tok",
                          scope=scope_for(ManagerDefinition.from_row(await managers.get_manager(OWNER)),
                                          ("report", "empty")))
    return chat, project


async def _create(chat):
    (created,) = await chat.say("Create a source report schedule", (TOOL, {
        "action": "create", "project": "calc", "fields": FIELDS}))
    assert created["status"] == "ok", created
    return created["result"]["schedule"]["schedule_id"]


async def _read(sid):
    return (await _ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]


def _selection(view):
    return view["template_key"], view["template_name"], view["template_inputs"]


def test_template_schedule_is_shown_disabled_then_confirmed_in_a_later_turn(monkeypatch, tmp_path):
    async def test(pool):
        chat, _ = await _setup(pool, tmp_path)
        created, enable, run = await chat.say("Create and start a report", (TOOL, {
            "action": "create", "project": "calc", "fields": FIELDS}),
            (TOOL, {"action": "enable", "schedule": "report"}),
            (TOOL, {"action": "run", "schedule": "report"}))
        assert created["status"] == "ok", created
        sid = created["result"]["schedule"]["schedule_id"]
        for answer in (enable, run):
            assert _error(answer) == "conflict" and "created in this turn" in answer["error"]["message"]
        assert await _starting_state(pool, sid) == (False, 0)
        assert await pool.fetchval("SELECT manager_id FROM schedules WHERE id=$1", sid) == OWNER
        expected = ("report", "Source report", VALUES)
        assert _selection(created["result"]["schedule"]) == _selection(await _read(sid)) == expected
        configuration = "\n".join(chat.turns[-1].blocks)
        for value in ("report", "Source report", *VALUES.values(), "要启用吗"):
            assert value in configuration
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE confirmation_id IS NOT NULL") == 0
        listed, got = await chat.say("Read schedules", (TOOL, {"action": "list"}),
                                     (TOOL, {"action": "get", "schedule": sid}))
        assert _selection(listed["result"]["schedules"][0]) == _selection(got["result"]["schedule"]) == expected
        (asked,) = await chat.say("Enable report", (TOOL, {"action": "enable", "schedule": sid}))
        assert asked["status"] == "needs_confirmation", asked
        for value in ("Source report", "report", *VALUES.values()):
            assert value in "\n".join(chat.turns[-1].blocks)
        assert await _starting_state(pool, sid) == (False, 0)
        assert (await _confirm(chat, asked))["status"] == "ok"
        assert await _starting_state(pool, sid) == (True, 0)
    run_db(test, monkeypatch, timeout=120)


@pytest.mark.parametrize("values", [{**VALUES, "note": "Include subtotals"}, {"url": VALUES["url"]}])
def test_inputs_only_update_displays_real_before_after_including_removed_values(monkeypatch, tmp_path, values):
    async def test(pool):
        chat, _ = await _setup(pool, tmp_path)
        sid = await _create(chat)
        (changed,) = await chat.say("Change the inputs", (TOOL, {
            "action": "update", "schedule": sid, "fields": {"template_inputs": values}}))
        assert changed["status"] == "ok", changed
        assert changed["result"]["changed"] == ["template_inputs"]
        block = "\n".join(chat.turns[-1].blocks)
        before, after = block.split(" → ")
        assert VALUES["note"] in before and VALUES["note"] not in after
        assert "没有变化" not in block and VALUES["url"] in before and VALUES["url"] in after
        if "note" in values:
            assert values["note"] in after
        assert _selection(await _read(sid)) == ("report", "Source report", values)
    run_db(test, monkeypatch, timeout=120)


@pytest.mark.parametrize("patch,key,values", [({"name": "renamed"}, "report", VALUES),
    ({"template_key": "empty"}, "empty", {}), ({"template_key": None}, None, {})])
def test_selection_omission_replacement_and_explicit_clear(monkeypatch, tmp_path, patch, key, values):
    async def test(pool):
        chat, _ = await _setup(pool, tmp_path)
        sid = await _create(chat)
        if key is None:
            await pool.execute("UPDATE managers SET task_policy='free' WHERE id=$1", OWNER)
        (answer,) = await chat.say("Update selection", (TOOL, {"action": "update", "schedule": sid, "fields": patch}))
        assert answer["status"] == "ok", answer
        view = await _read(sid)
        assert (view["template_key"], view["template_inputs"]) == (key, values)
        if key != "report":
            block = "\n".join(chat.turns[-1].blocks)
            assert "Source report" in block and VALUES["note"] in block and " → " in block
    run_db(test, monkeypatch, timeout=120)


@pytest.mark.parametrize("patch,reason", [({"template_key": None}, "requires a template"),
    ({"template_key": "foreign"}, "has no template"), ({"template_key": "bad key"}, "must match"),
    ({"template_key": "report", "template_inputs": {}}, "needs Source"),
    ({"template_inputs": {"unknown": "value"}}, "no input"),
    ({"template_inputs": {"url": 7}}, "static strings"),
    ({"template_inputs": {"url": "file:///tmp/source"}}, "http or https"),
    ({"template_inputs": None}, "static strings"), ({"manager_id": "manager"}, "unknown key")])
def test_invalid_owner_template_input_combinations_leave_schedule_unchanged(monkeypatch, tmp_path, patch, reason):
    async def test(pool):
        chat, _ = await _setup(pool, tmp_path)
        await managers.put_template("manager", "foreign", {"name": "Private foreign report"})
        await managers.put_template("manager", "report", {"name": "Different owner report"})
        sid = await _create(chat)
        before = await _read(sid)
        (answer,) = await chat.say("Change selection", (TOOL, {"action": "update", "schedule": sid, "fields": patch}))
        assert _error(answer) in {"invalid", "conflict"} and reason in answer["error"]["message"]
        assert await _read(sid) == before
        assert "Private foreign report" not in str(answer) and "Different owner report" not in str(chat.turns)
    run_db(test, monkeypatch, timeout=120)


def test_scoped_manager_does_not_display_another_owners_template_selection(monkeypatch, tmp_path):
    async def test(pool):
        chat, project = await _setup(pool, tmp_path)
        await managers.put_template("manager", "foreign", {"name": "Foreign private template", "inputs": [
            {"key": "note", "label": "Private note"}]})
        foreign = (await schedules.create_schedule(project["id"], {**FIELDS, "name": "foreign",
            "template_key": "foreign", "template_inputs": {"note": "Private static value"}}))["schedule"]
        assert _selection(await _read(foreign["schedule_id"])) == (
            "foreign", "Foreign private template", {"note": "Private static value"})
        listed, got = await chat.say("Read foreign schedule", (TOOL, {"action": "list"}),
            (TOOL, {"action": "get", "schedule": "foreign"}))
        assert _selection(listed["result"]["schedules"][0]) == _selection(got["result"]["schedule"]) == (None, None, {})
        (asked,) = await chat.say("Enable foreign schedule", (TOOL, {"action": "enable", "schedule": "foreign"}))
        assert asked["status"] == "needs_confirmation", asked
        output = str(chat.turns) + str((listed, got, asked))
        assert "Foreign private template" not in output and "Private static value" not in output
    run_db(test, monkeypatch, timeout=120)


def test_scoped_manager_refuses_another_owners_template_change_before_writing(monkeypatch, tmp_path):
    """A real template change on another owner's schedule must not commit and then read back as no change."""
    async def test(pool):
        chat, project = await _setup(pool, tmp_path)
        await managers.put_template("manager", "foreign", {"name": "Foreign private template", "inputs": [
            {"key": "note", "label": "Private note"}]})
        await managers.put_template("manager", "optional", {"name": "Optional foreign template"})
        foreign = (await schedules.create_schedule(project["id"], {**FIELDS, "name": "foreign",
            "template_key": "foreign", "template_inputs": {"note": "Private static value"}}))["schedule"]
        sid = foreign["schedule_id"]
        before = await _read(sid)
        patches = ({"template_inputs": {"note": "Replaced private value"}}, {"template_key": "optional"},
                   {"name": "should-not-stick", "template_inputs": {"note": "Replaced private value"}})
        for patch in patches:
            (answer,) = await chat.say("Change the other owner's template", (TOOL, {
                "action": "update", "schedule": sid, "fields": patch}))
            assert _error(answer) == "forbidden" and "another Manager" in answer["error"]["message"]
            assert await _read(sid) == before
            shown = str(answer) + "\n" + "\n".join(chat.turns[-1].blocks)
            assert "没有变化" not in shown
            for secret in ("Private static value", "Foreign private template", "Optional foreign template",
                           "Replaced private value"):
                assert secret not in shown
        (renamed,) = await chat.say("Rename the other schedule", (TOOL, {
            "action": "update", "schedule": sid, "fields": {"name": "foreign-renamed"}}))
        assert renamed["status"] == "ok", renamed
        assert renamed["result"]["changed"] == ["name"]
        block = "\n".join(chat.turns[-1].blocks)
        assert "没有变化" not in block and "foreign-renamed" in block
        assert "Private static value" not in block and "Foreign private template" not in block
        stored = await _read(sid)
        assert stored["name"] == "foreign-renamed"
        assert (stored["template_key"], stored["template_inputs"]) == ("foreign", {"note": "Private static value"})
    run_db(test, monkeypatch, timeout=120)


def test_templates_only_owner_refuses_free_form_create(monkeypatch, tmp_path):
    async def test(pool):
        chat, _ = await _setup(pool, tmp_path)
        fields = {k: v for k, v in FIELDS.items() if not k.startswith("template_")}
        (answer,) = await chat.say("Create a free form schedule", (TOOL, {
            "action": "create", "project": "calc", "fields": fields}))
        assert _error(answer) == "invalid" and "requires a template" in answer["error"]["message"]
        assert await pool.fetchval("SELECT count(*) FROM schedules") == 0
    run_db(test, monkeypatch, timeout=120)
