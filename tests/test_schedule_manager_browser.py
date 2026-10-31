"""Schedule ownership through the built dashboard and synthetic v1 responses."""

from pathlib import Path

import pytest

from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401

pytestmark = pytest.mark.installed


@pytest.fixture
def dashboard(dashboard_url):  # noqa: F811
    playwright = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")

    with playwright.sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_default_timeout(5000)
        project = {"project_id": "proj-example", "name": "Example", "work_dir": "/srv/example",
                   "description": "", "background": "", "task_count": 0, "active_count": 0, "schedule_count": 0}
        state = {"calls": [], "schedules": [], "scenarios": True, "refuse": False, "owner_error": False}
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        limits = {"name_max_chars": 80, "requirement_max_chars": 10000, "default_timezone": "UTC",
                  "min_interval_seconds": 300, "min_interval_gated_seconds": 60, "max_parallel_runs_limit": 10,
                  "concurrency_max": 100, "gate_command_max_chars": 2000, "gate_timeout_max_seconds": 300,
                  "gate_timeout_default_seconds": 30, "run_timeout_max_seconds": 86400,
                  "roles": ["planner", "worker", "reviewer"], "notify_targets": []}

        def respond(route):
            path = route.request.url.split("/api/v1/")[-1]
            body = route.request.post_data_json or {}
            state["calls"].append((path, body))
            if path == "managers/list":
                if state["owner_error"]:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Owner list unavailable"}})
                    return
                managers = [{"id": "manager", "name": "Default", "enabled": True}]
                if state["scenarios"]:
                    managers += [{"id": "manager-review", "name": "Review", "enabled": True},
                                 {"id": "manager-paused", "name": "Paused", "enabled": False}]
                result = {"managers": managers, "max_enabled": 8}
            elif path == "schedule_admin/create":
                if state["refuse"]:
                    route.fulfill(status=409, json={"status": "error", "error": {
                        "code": "conflict", "message": "Owner is no longer available"}})
                    return
                owner = body["fields"].get("manager_id", "manager")
                schedule = {**body["fields"], "schedule_id": "sched-example", "project_id": project["project_id"],
                    "project_name": project["name"], "manager_id": owner,
                    "manager_name": {"manager": "Default", "manager-review": "Review", "manager-paused": "Paused"}[owner],
                    "description": "Every hour (UTC)", "enabled": False, "next_fire_at": None, "last_run": None,
                    "work_dir": "", "max_parallel_runs": 3, "run_budget_usd": None, "daily_budget_usd": None,
                    "profile_overrides": {}, "task_limits": {"worker": None, "reviewer": None},
                    "gate_command": None, "gate_timeout_seconds": None, "notify_target": None,
                    "spent_today_usd": 0, "consecutive_failures": 0,
                    "planner_session": {"active": False}, "alerts": {"failures_since": None, "stale_since": None}}
                state["schedules"].append(schedule)
                result = {"schedule": schedule}
            elif path == "schedule_admin/update":
                assert "manager_id" not in body["fields"]
                state["schedules"][0].update(body["fields"])
                result = {"schedule": state["schedules"][0]}
            else:
                result = {"project_admin/list": {"projects": [project]}, "system/status": {},
                    "system/overview": {}, "ops/gates/list": {"gates": [], "total": 0},
                    "chat_settings/get": {"stop_enabled": False},
                    "registry/view": {"profiles": [], "models": [], "runtimes": [], "providers": []},
                    "schedule_admin/project_list": {"schedules": state["schedules"], "limits": limits},
                    "schedule_admin/list": {"schedules": state["schedules"]},
                    "schedule_admin/get": {"schedule": state["schedules"][0] if state["schedules"] else None},
                    "schedule_admin/runs": {"runs": [], "next_before": None,
                                            "routine_skips": {"skipped_gate": 0, "skipped_overlap": 0}},
                    "schedule_admin/notifications": {"notifications": []},
                    "schedule_admin/preview": {"valid": True, "description": "Every hour (UTC)", "next": []},
                }.get(path)
                if result is None:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside schedule ownership fixture"}})
                    return
            route.fulfill(json={"status": "ok", "result": result})

        context.route("**/api/**", respond)
        try:
            yield page, dashboard_url, state
            assert not errors
        finally:
            context.close()
            browser.close()


def new_schedule(page, base):
    page.goto(f"{base}/#projects/proj-example/schedules")
    page.get_by_role("button", name="New schedule", exact=True).click()
    page.get_by_label("Name *", exact=True).fill("Hourly review")
    page.get_by_label("Rule (cron) *", exact=True).fill("0 * * * *")
    page.get_by_label("Requirement *", exact=True).fill("Review synthetic reports.")


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_scenario_owner_create_reload_and_immutable_edit(dashboard, tmp_path, width):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    new_schedule(page, base)
    owner = page.get_by_label("Owner", exact=True)
    expect(owner).to_have_value("manager")
    expect(owner.locator("option")).to_have_text(["Default", "Review", "Paused (disabled)"])
    owner.select_option("manager-review")
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_text("Owner: Review. Ownership cannot be changed.", exact=True)).to_be_visible()
    assert state["schedules"][0]["manager_id"] == "manager-review"
    page.reload()
    expect(page.get_by_text("Owner: Review. Ownership cannot be changed.", exact=True)).to_be_visible()
    expect(page.get_by_label("Owner", exact=True)).to_have_count(0)
    page.get_by_label("Name *", exact=True).fill("Updated review")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    assert state["schedules"][0]["manager_id"] == "manager-review"
    page.goto(f"{base}/#schedules")
    expect(page.get_by_text("Owner: Review", exact=True)).to_be_visible()
    page.get_by_role("link", name="Updated review", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Updated review")).to_be_visible()
    expect(page.get_by_text("Owner: Review", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot = tmp_path / f"schedule-owner-{width}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(f"Screenshot: {screenshot}")


def test_failed_create_keeps_owner_and_draft(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["refuse"] = True
    new_schedule(page, base)
    page.get_by_label("Owner", exact=True).select_option("manager-paused")
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Owner is no longer available")
    expect(page.get_by_label("Name *", exact=True)).to_have_value("Hourly review")
    expect(page.get_by_label("Owner", exact=True)).to_have_value("manager-paused")
    assert not state["schedules"]


def test_default_only_creation_omits_owner_control_and_field(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["scenarios"] = False
    new_schedule(page, base)
    expect(page.get_by_role("button", name="Create", exact=True)).to_be_enabled()
    expect(page.get_by_label("Owner", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_text("Owner: Default. Ownership cannot be changed.", exact=True)).to_be_visible()
    create = next(body for path, body in state["calls"] if path == "schedule_admin/create")
    assert "manager_id" not in create["fields"]


def test_owner_load_failure_is_visible_and_can_retry_without_losing_draft(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["owner_error"] = True
    new_schedule(page, base)
    expect(page.get_by_role("alert")).to_contain_text("Owner list unavailable")
    expect(page.get_by_role("button", name="Create", exact=True)).to_be_disabled()
    state["owner_error"] = False
    page.get_by_role("button", name="Retry owners", exact=True).click()
    expect(page.get_by_label("Owner", exact=True)).to_be_visible()
    expect(page.get_by_label("Name *", exact=True)).to_have_value("Hourly review")


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_command_environment_multiline_round_trip(dashboard, tmp_path, width):
    import json

    from playwright.sync_api import expect

    page, base, state = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    new_schedule(page, base)
    page.get_by_role("radio", name="Command", exact=False).check()
    page.get_by_label("Command *", exact=True).fill("printf ready")
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_text("Type: Command", exact=True)).to_be_visible()
    # Simulate a schedule supplied by the API/CLI, including values that look like new entries.
    original = {"SETTINGS": "REGION=us\nTIER=gold", "EMPTY": "",
                "LITERAL": '  C:\\reports\\n="quoted"  '}
    state["schedules"][0]["env"] = original.copy()
    page.reload()
    page.get_by_label("Name *", exact=True).fill("Renamed command")
    with page.expect_response(lambda r: r.url.endswith("/schedule_admin/update")) as response:
        page.get_by_role("button", name="Save", exact=True).click()
    assert response.value.request.post_data_json["fields"] == {"name": "Renamed command"}
    assert state["schedules"][0]["env"] == original

    env = page.get_by_label("Environment", exact=True)
    assert json.loads(env.input_value()) == original
    updated = {**original, "SETTINGS": "REGION=eu\nTIER=silver\n", "CRLF": "a\r\nb\r", "ADDED": " x=y "}
    env.fill(json.dumps(updated))
    with page.expect_response(lambda r: r.url.endswith("/schedule_admin/update")) as response:
        page.get_by_role("button", name="Save", exact=True).click()
    assert response.value.request.post_data_json["fields"] == {"env": updated}
    page.reload()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    assert json.loads(env.input_value()) == updated
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    env.scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f"command-environment-{width}.png"), full_page=True)

    for invalid in ('{"SETTINGS":', '{"SETTINGS": 1}', '{"SETTINGS": null}', '{"BAD-KEY": "value"}'):
        env.fill(invalid)
        expect(env).to_have_attribute("aria-invalid", "true")
        expect(page.locator("form").get_by_role("alert")).to_be_visible()
        expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    env.fill("{}")
    with page.expect_response(lambda r: r.url.endswith("/schedule_admin/update")) as response:
        page.get_by_role("button", name="Save", exact=True).click()
    assert response.value.request.post_data_json["fields"] == {"env": {}}
    page.reload()
    expect(env).to_have_value("")


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_command_form_create_edit_and_agent_regression(tmp_path, staged_dashboard_dir, width):
    """Exercise form serialization against a fresh database and a real serve process."""
    import os

    playwright = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")
    expect, sync_playwright = playwright.expect, playwright.sync_playwright

    from tests.test_llm_models_dashboard import _running_dashboard

    admin_url = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("UNTESTED: disposable PostgreSQL is not configured")
    with _running_dashboard(tmp_path, admin_url) as server, sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": width, "height": 900})
        try:
            page = context.new_page()
            page.set_default_timeout(10000)

            def snapshot(name):
                form = page.locator("form").filter(has=page.get_by_label("Name *", exact=True))
                expect(form).not_to_contain_text("Checking the rule…")
                height = form.evaluate("element => element.scrollHeight") + 800
                page.set_viewport_size({"width": width, "height": height})
                form.evaluate("""element => {
                    for (let node = element; node; node = node.parentElement) node.scrollTop = 0;
                }""")
                page.locator("textarea").evaluate_all("elements => elements.forEach(element => element.scrollTop = 0)")
                page.screenshot(path=str(tmp_path / f"{name}-{width}.png"), full_page=True)
                page.set_viewport_size({"width": width, "height": 900})

            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            project = server.api.post("/api/v1/project_admin/list", {})["result"]["projects"][0]
            pid = project["project_id"]
            server.api.post("/api/v1/project_admin/update", {"project_id": pid, "fields": {
                "name": "Example", "work_dir": "/srv/example"}})
            base = server.api.base_url
            page.goto(f"{base}/#projects/{pid}/schedules", wait_until="domcontentloaded")
            page.get_by_role("button", name="New schedule", exact=True).click()
            expect(page.get_by_label("Requirement *", exact=True)).to_be_visible()
            page.get_by_role("radio", name="Command", exact=False).check()
            expect(page.get_by_label("Requirement *", exact=True)).to_have_count(0)
            expect(page.get_by_role("radio", name="Use the default", exact=False)).to_have_count(0)
            page.get_by_label("Time zone", exact=True).fill("UTC")
            page.get_by_label("Name *", exact=True).fill("Daily report")
            page.get_by_label("Rule (cron) *", exact=True).fill("* * * * *")
            command = "printf '%s\\n' " + "x" * 400
            page.get_by_label("Command *", exact=True).fill(command)
            env = page.get_by_label("Environment", exact=True)
            env.fill("# synthetic configuration\nBROKEN")
            expect(page.locator("form").get_by_role("alert")).to_contain_text("Line 2 is not KEY=VALUE")
            expect(page.get_by_role("button", name="Create", exact=True)).to_be_disabled()
            env.fill("REPORT=old\nREPORT=daily=summary\nZONE=west")
            expect(page.get_by_text("the last value is used.", exact=False)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            snapshot("command-create")
            page.get_by_text("Alerts", exact=True).click()
            page.get_by_label("Success deadline", exact=True).fill("08:00")
            page.get_by_label("Alert after N failures in a row", exact=True).fill("5")
            page.get_by_label("Alert after no success for (minutes)", exact=True).fill("5.5")
            page.get_by_label("Alert after N skipped firings in a row", exact=True).fill("0")
            with page.expect_response(lambda r: r.url.endswith("/schedule_admin/create")) as response:
                page.get_by_role("button", name="Create", exact=True).click()
            assert response.value.status == 200, response.value.text()
            fields = response.value.request.post_data_json["fields"]
            assert fields["kind"] == "command" and fields["run_timeout_seconds"] is None
            assert "requirement" not in fields and "profile_overrides" not in fields
            saved = response.value.json()["result"]["schedule"]
            assert saved["env"] == {"REPORT": "daily=summary", "ZONE": "west"}
            assert saved["alert_policy"]["stale_after_seconds"] == {
                "value": 330, "default": False, "source": "schedule"}
            sid = saved["schedule_id"]
            expect(page.get_by_text("Type: Command", exact=True)).to_be_visible()
            expect(page.get_by_role("radio", name="Agent task", exact=False)).to_have_count(0)
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_label("Command *", exact=True)).to_have_value(command)
            expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            snapshot("command-edit")
            page.get_by_label("Command *", exact=True).fill("printf updated")
            with page.expect_response(lambda r: r.url.endswith("/schedule_admin/update")) as response:
                page.get_by_role("button", name="Save", exact=True).click()
            assert response.value.status == 200
            assert response.value.request.post_data_json["fields"] == {"command": "printf updated"}
            page.goto(f"{base}/#schedules/{sid}", wait_until="domcontentloaded")
            expect(page.get_by_text("printf updated", exact=True)).to_be_visible()
            expect(page.get_by_text("REPORT=daily=summary", exact=True)).to_be_visible()
            expect(page.get_by_text("Requirement", exact=True)).to_have_count(0)
            page.goto(f"{base}/#schedules", wait_until="domcontentloaded")
            expect(page.get_by_text("Command", exact=True)).to_be_visible()
            page.goto(f"{base}/#projects/{pid}/schedules", wait_until="domcontentloaded")
            page.get_by_role("button", name="New schedule", exact=True).click()
            page.get_by_label("Time zone", exact=True).fill("UTC")
            page.get_by_label("Name *", exact=True).fill("Agent report")
            page.get_by_label("Rule (cron) *", exact=True).fill("0 9 * * *")
            page.get_by_label("Requirement *", exact=True).fill("Review the daily reports.")
            expect(page.get_by_label("Command *", exact=True)).to_have_count(0)
            expect(page.get_by_label("Spend cap per run (USD)", exact=True)).to_be_visible()
            expect(page.get_by_role("radio", name="Use the default", exact=False)).to_be_checked()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            snapshot("agent-create")
            with page.expect_response(lambda r: r.url.endswith("/schedule_admin/create")) as response:
                page.get_by_role("button", name="Create", exact=True).click()
            assert response.value.status == 200, response.value.text()
            fields = response.value.request.post_data_json["fields"]
            assert fields == {"kind": "agent", "name": "Agent report", "cron": "0 9 * * *",
                              "timezone": "UTC", "requirement": "Review the daily reports.",
                              "overlap": "skip", "catch_up": "once", "run_timeout_seconds": "default",
                              "notify": "failures", "user_gates": "off"}
            assert not errors
        finally:
            context.close()
            browser.close()
