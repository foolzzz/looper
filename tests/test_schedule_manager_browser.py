"""Schedule ownership through the built dashboard and synthetic v1 responses."""

from pathlib import Path

import pytest

from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401

pytestmark = pytest.mark.installed


@pytest.fixture
def dashboard(dashboard_url):
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
