"""Project directory moves through authenticated admin and compact API operations."""

import pytest

from looper.agents import api_v1, operator_notes, schedules
from looper.db import projects, tasks
from tests.db_support import DSN, run_db, v1_api as _api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def isolated_notes_root(monkeypatch, tmp_path):
    monkeypatch.setattr(operator_notes, "_root", str(tmp_path / "operator-notes"))


async def _schedule(project_id, kind, directory):
    return await schedules.create_schedule(project_id, {
        "name": f"{kind} schedule", "kind": kind, "cron": "0 * * * *", "timezone": "UTC",
        "work_dir": str(directory), "requirement": "Read reports" if kind == "agent" else "",
        **({"command": "git status"} if kind == "command" else {}),
    })


@pytest.mark.parametrize("client", [True, False], ids=["client", "operator"])
@pytest.mark.parametrize("compact", [False, True], ids=["admin", "compact"])
def test_move_confirms_clients_and_runs_for_operator_with_schedule_readback(tmp_path, monkeypatch, client, compact):
    old, new = tmp_path / "source", tmp_path / "destination"
    old.mkdir()
    (old / "reports").mkdir()
    (old / "notes.txt").write_text("hello")

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(old), pool=pool)
        command = await _schedule(project["id"], "command", old)
        agent = await _schedule(project["id"], "agent", old / "reports")
        path = "/api/v1/ops/projects/move" if compact else "project_admin.move_dir"
        body = {"project": "Example" if compact else project["id"], "to": str(new)}
        async with _api(tmp_path, pool) as call:
            status, answer = await call(path, body, client=client)
            if client:
                assert status == 202 and answer["status"] == "needs_confirmation", answer
                impact = answer["impact"]
                for text in ("Example", str(old), str(new), "command schedule", "agent schedule", "5"):
                    assert text in impact, impact
                assert "文件" in impact and "字节" in impact and "符号链接" in impact, impact
                assert "中断" in impact, impact
                assert old.is_dir() and not old.is_symlink() and not new.exists()
                assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(old)
                status, answer = await call("confirm", {"confirmation_id": answer["confirmation_id"]}, client=True)
            assert status == 200 and answer["status"] == "ok", answer
            result = answer["result"]
            operation = api_v1.find("POST", "/api/v1/ops/projects/move" if compact else
                                    "/api/v1/project_admin/move_dir")
            api_v1.validate_arguments(operation.result, result)
            assert result["project"]["work_dir"] == result["new_dir"] == str(new)
            assert result["old_dir"] == str(old) and result["link_old"] is True
            assert old.is_symlink() and (old / "notes.txt").read_text() == "hello"
            assert (new / "notes.txt").read_text() == "hello"
            for made, destination in ((command, new), (agent, new / "reports")):
                saved = await schedules.get_schedule(made["schedule"]["schedule_id"])
                assert saved["schedule"]["work_dir"] == str(destination)
    run_db(run, monkeypatch, timeout=30)


def test_admin_move_resolves_name_and_omitted_destination_and_disables_link(tmp_path, monkeypatch):
    from looper.agents import projects as core

    old, root = tmp_path / "source", tmp_path / "projects"
    old.mkdir()
    root.mkdir()
    (old / "notes").write_text("keep")

    async def run(pool):
        monkeypatch.setattr(core, "_root", str(root))
        project = await projects.create_project(name="Example Project", work_dir=str(old), pool=pool)
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin.move_dir", {"project": "example project", "link_old": False})
            assert status == 200, answer
            assert answer["result"]["new_dir"] == str(root / "example-project")
            assert answer["result"]["link_old"] is False and not old.exists()
            assert (root / "example-project" / "notes").read_text() == "keep"
            assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(root / "example-project")
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("change", ["schedule", "directory", "name"])
def test_client_move_confirmation_binds_exact_project_and_affected_configuration(tmp_path, monkeypatch, change):
    old, new = tmp_path / "source", tmp_path / "destination"
    old.mkdir()
    (old / "notes").write_text("keep")

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(old), pool=pool)
        made = await _schedule(project["id"], "command", old)
        async with _api(tmp_path, pool) as call:
            status, asked = await call("project_admin.move_dir", {"project": "Example", "to": str(new)}, client=True)
            assert status == 202, asked
            if change == "schedule":
                await schedules.update_schedule(made["schedule"]["schedule_id"], {"command": "git diff"})
            elif change == "directory":
                await projects.update_project(project["id"], work_dir=str(tmp_path / "elsewhere"), pool=pool)
            else:
                await projects.update_project(project["id"], name="Renamed", pool=pool)
                # The original name can now identify a different project; confirmation must never follow it.
                await projects.create_project(name="Example", pool=pool)
            status, answer = await call("confirm", {"confirmation_id": asked["confirmation_id"]}, client=True)
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert old.is_dir() and not old.is_symlink() and not new.exists()
            assert (old / "notes").read_text() == "keep"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("body", [{}, {"project": "missing", "to": 1},
    {"project": "missing", "link_old": "true"}, {"project": "missing", "unexpected": True}])
def test_invalid_move_requests_refused_before_confirmation(tmp_path, monkeypatch, body):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin.move_dir", body, client=True)
            assert status == 400 and answer["status"] == "error", answer
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
@pytest.mark.parametrize("action", ["preview_move_dir", "move_dir"])
@pytest.mark.parametrize("refusal,expected_status,message", [
    ("destination-exists", 409, "to already exists; choose a path that does not exist"),
    ("busy", 409, "the project has a task, command schedule run or gate in progress; wait for it to finish"),
    ("relative-destination", 400, "to must be an absolute directory"),
])
def test_admin_move_keeps_specific_refusal_message_and_status(
        tmp_path, monkeypatch, client, action, refusal, expected_status, message):
    old, new = tmp_path / "source", tmp_path / "destination"
    old.mkdir()
    (old / "notes").write_text("keep")
    if refusal == "destination-exists":
        new.mkdir()

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(old), pool=pool)
        if refusal == "busy":
            await tasks.create_task(task_id="active-move", requirement="Continue next turn",
                                    project_id=project["id"], work_dir=str(old), status="executing", pool=pool)
        destination = "relative" if refusal == "relative-destination" else str(new)
        async with _api(tmp_path, pool) as call:
            status, answer = await call(f"project_admin.{action}", {
                "project": project["id"], "to": destination}, client=client)
            assert status == expected_status and answer["status"] == "error", answer
            assert answer["error"]["message"] == message, answer
            assert answer["error"]["code"] == ("conflict" if expected_status == 409 else "invalid"), answer
            assert old.is_dir() and not old.is_symlink()
            assert (old / "notes").read_text() == "keep"
            assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(old)

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
def test_move_preview_is_readonly_and_returns_versioned_confirmation_impact(tmp_path, monkeypatch, client):
    old, new = tmp_path / "source", tmp_path / "destination"
    old.mkdir()
    (old / "notes").write_text("keep")

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(old), pool=pool)
        before = await projects.get_project(project["id"], pool=pool)
        async with _api(tmp_path, pool) as call:
            body = {"project": project["id"], "to": str(new)}
            status, answer = await call("project_admin.preview_move_dir", body, client=client)
            assert status == 200 and answer["status"] == "ok", answer
            preview = answer["result"]
            assert preview["old_dir"] == str(old) and preview["new_dir"] == str(new)
            assert preview["move_version"] and str(old) in preview["impact"] and str(new) in preview["impact"]
            assert old.is_dir() and not old.is_symlink() and not new.exists()
            assert await projects.get_project(project["id"], pool=pool) == before
            # A client-supplied token cannot bypass the server's own confirmation snapshot.
            status, answer = await call("project_admin.move_dir", {**body, "move_version": "stale"}, client=client)
            if client:
                assert status == 202 and answer["status"] == "needs_confirmation", answer
                status, answer = await call("confirm", {"confirmation_id": answer["confirmation_id"]}, client=True)
                assert status == 200 and answer["result"]["new_dir"] == str(new), answer
            else:
                assert status == 409 and answer["error"]["code"] == "conflict", answer
                assert old.is_dir() and not old.is_symlink() and not new.exists()
                status, answer = await call("project_admin.move_dir", {**body, "move_version": preview["move_version"]})
                assert status == 200 and answer["result"]["new_dir"] == str(new), answer
    run_db(run, monkeypatch, timeout=30)


def test_dashboard_move_rejects_configuration_changed_since_preview(tmp_path, monkeypatch):
    old, new = tmp_path / "source", tmp_path / "destination"
    old.mkdir()
    (old / "notes").write_text("keep")

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(old), pool=pool)
        made = await _schedule(project["id"], "command", old)
        async with _api(tmp_path, pool) as call:
            body = {"project": project["id"], "to": str(new), "link_old": True}
            status, answer = await call("project_admin.preview_move_dir", body)
            assert status == 200, answer
            await schedules.update_schedule(made["schedule"]["schedule_id"], {"command": "git diff"})
            status, answer = await call("project_admin.move_dir", {
                **body, "move_version": answer["result"]["move_version"]})
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert answer["error"]["message"] == (
                "the project or its schedules changed; run the move again to ask for confirmation"), answer
            assert old.is_dir() and not old.is_symlink() and not new.exists()
            assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(old)
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("client", [False, True], ids=["dashboard", "manager"])
@pytest.mark.parametrize("change", ["active-task", "template-path"])
def test_move_rechecks_task_and_path_dependencies_after_preview(tmp_path, monkeypatch, client, change):
    old, new = tmp_path / "source", tmp_path / "destination"
    old.mkdir()
    (old / "notes").write_text("keep")

    async def run(pool):
        project = await projects.create_project(name="Example", work_dir=str(old), pool=pool)
        async with _api(tmp_path, pool) as call:
            body = {"project": project["id"], "to": str(new), "link_old": False}
            action = "project_admin.move_dir" if client else "project_admin.preview_move_dir"
            status, preview = await call(action, body, client=client)
            assert status == (202 if client else 200), preview
            if change == "active-task":
                await tasks.create_task(task_id="between-turns", requirement="Continue next turn",
                                        project_id=project["id"], work_dir=str(old) + "-other",
                                        status="executing", pool=pool)
            else:
                await pool.execute("INSERT INTO manager_task_templates (manager_id,key,name,project_id,work_dir) "
                                   "VALUES ('manager','recorded','Recorded',$1,$2)", project["id"], str(old))
            if client:
                status, answer = await call("confirm", {"confirmation_id": preview["confirmation_id"]}, client=True)
            else:
                status, answer = await call("project_admin.move_dir", {
                    **body, "move_version": preview["result"]["move_version"]})
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert old.is_dir() and not old.is_symlink() and not new.exists()
            assert (old / "notes").read_text() == "keep"
            assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(old)

    run_db(run, monkeypatch, timeout=30)
