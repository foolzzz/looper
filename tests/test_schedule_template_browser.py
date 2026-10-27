"""Schedule template controls through the built dashboard and fake API transports."""

import pytest

from tests.test_schedule_manager_browser import dashboard as _dashboard, dashboard_url, new_schedule  # noqa: F401

dashboard = _dashboard

pytestmark = pytest.mark.installed


def template(key, name, label="Report URL", *, work_dir=""):
    return {"key": key, "name": name, "description": f"Instructions for {name}",
            "project_id": None, "work_dir": work_dir, "workspace_mode": None,
            "requirement": "Review {{report_url}}", "planner_instructions": "Review carefully.",
            "profiles": {}, "role_templates": [], "updated_at": "2026-10-01T00:00:00Z",
            "inputs": [{"key": "report_url", "label": label, "kind": "url", "required": True,
                        "pattern": "", "hint": "A public report URL, saved for every run."}]}


def managers():
    return [{"id": "manager", "name": "Default", "enabled": True, "templates": []},
            {"id": "manager-review", "name": "Review", "enabled": True, "templates": [
                template("report", "Report review"), template("summary", "Summary review")]},
            {"id": "manager-paused", "name": "Paused", "enabled": False, "templates": [
                template("report", "Paused report", "Paused URL")]}]


def use_templates(page):
    page.route("**/api/v1/managers/list", lambda route: route.fulfill(
        json={"status": "ok", "result": {"managers": managers(), "max_enabled": 8}}))


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_template_inputs_save_reload_details_and_keyboard(dashboard, tmp_path, width):
    from playwright.sync_api import expect

    page, base, state = dashboard
    use_templates(page)
    page.set_viewport_size({"width": width, "height": 900})
    new_schedule(page, base)
    page.get_by_label("Owner", exact=True).select_option("manager-review")
    select = page.get_by_label("Task template", exact=True)
    select.select_option("report")
    select.focus()
    select.press("Tab")
    expect(select).to_have_value("report")
    field = page.get_by_label("Report URL *", exact=True)
    expect(field).to_be_focused()
    expect(field).to_have_attribute("aria-required", "true")
    expect(page.get_by_text("A public report URL, saved for every run.", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Create", exact=True)).to_be_disabled()
    field.fill("https://example.org/report?literal={{unchanged}}")
    field.press("Tab")
    expect(field).not_to_be_focused()
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_text("Owner: Review. Ownership cannot be changed.", exact=True)).to_be_visible()
    schedule = state["schedules"][0]
    schedule["template_name"] = "Report review"
    assert schedule["template_key"] == "report"
    assert schedule["template_inputs"] == {"report_url": "https://example.org/report?literal={{unchanged}}"}
    page.reload()
    expect(page.get_by_label("Task template", exact=True)).to_have_value("report")
    expect(page.get_by_label("Report URL *", exact=True)).to_have_value(schedule["template_inputs"]["report_url"])
    page.goto(f"{base}/#schedules/sched-example")
    expect(page.get_by_role("heading", level=1, name="Hourly review")).to_be_visible()
    expect(page.get_by_role("definition").filter(has_text="Report review")).to_be_visible()
    expect(page.get_by_text("report_url: https://example.org/report?literal={{unchanged}}", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot = tmp_path / f"schedule-template-{width}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(f"Screenshot: {screenshot}")


def test_switching_owner_and_template_clears_old_inputs(dashboard):
    from playwright.sync_api import expect

    page, base, _ = dashboard
    use_templates(page)
    new_schedule(page, base)
    owner = page.get_by_label("Owner", exact=True)
    owner.select_option("manager-review")
    select = page.get_by_label("Task template", exact=True)
    select.select_option("report")
    page.get_by_label("Report URL *", exact=True).fill("https://example.org/old")
    select.select_option("summary")
    expect(page.get_by_label("Report URL *", exact=True)).to_have_value("")
    page.get_by_label("Report URL *", exact=True).fill("https://example.org/summary")
    owner.select_option("manager-paused")
    expect(select).to_have_value("")
    expect(page.get_by_label("Report URL *", exact=True)).to_have_count(0)
    expect(page.get_by_text("This owner is disabled.", exact=False)).to_be_visible()
    select.select_option("report")
    expect(page.get_by_label("Paused URL *", exact=True)).to_have_value("")
    owner.select_option("manager")
    expect(select).to_have_value("")
    expect(page.get_by_text("This owner has no task templates.", exact=False)).to_be_visible()


def test_server_field_error_keeps_template_and_static_draft(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    use_templates(page)
    page.route("**/api/v1/schedule_admin/create", lambda route: route.fulfill(status=400,
        json={"status": "error", "error": {"code": "invalid", "message": "Input Report URL is not allowed"}}))
    new_schedule(page, base)
    page.get_by_label("Owner", exact=True).select_option("manager-review")
    page.get_by_label("Task template", exact=True).select_option("report")
    page.get_by_label("Report URL *", exact=True).fill("https://example.org/refused")
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Input Report URL is not allowed")
    expect(page.get_by_label("Task template", exact=True)).to_have_value("report")
    expect(page.get_by_label("Report URL *", exact=True)).to_have_value("https://example.org/refused")
    assert not state["schedules"]


def test_late_owner_response_cannot_replace_reopened_form_selection(dashboard):
    from playwright.sync_api import expect

    page, base, _ = dashboard
    held = []

    def owners(route):
        if not held:
            held.append(route)
        else:
            route.fulfill(json={"status": "ok", "result": {"managers": managers(), "max_enabled": 8}})

    page.route("**/api/v1/managers/list", owners)
    new_schedule(page, base)
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("button", name="New schedule", exact=True).click()
    page.get_by_label("Owner", exact=True).select_option("manager-review")
    page.get_by_label("Task template", exact=True).select_option("summary")
    page.get_by_label("Report URL *", exact=True).fill("https://example.org/current")
    held[0].fulfill(json={"status": "ok", "result": {"managers": managers()[:1], "max_enabled": 8}})
    expect(page.get_by_label("Task template", exact=True)).to_have_value("summary")
    expect(page.get_by_label("Report URL *", exact=True)).to_have_value("https://example.org/current")


def test_no_scenario_manager_has_no_template_selector(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["scenarios"] = False
    new_schedule(page, base)
    expect(page.get_by_label("Task template", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Create", exact=True).click()
    create = next(body for path, body in state["calls"] if path == "schedule_admin/create")
    assert "template_key" not in create["fields"]
    assert "template_inputs" not in create["fields"]


def test_unavailable_saved_template_can_be_cleared(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    use_templates(page)
    new_schedule(page, base)
    page.get_by_label("Owner", exact=True).select_option("manager-review")
    page.get_by_label("Task template", exact=True).select_option("report")
    page.get_by_label("Report URL *", exact=True).fill("https://example.org/report")
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_text("Owner: Review. Ownership cannot be changed.", exact=True)).to_be_visible()
    rows = managers()
    rows[1]["templates"] = []
    page.route("**/api/v1/managers/list", lambda route: route.fulfill(
        json={"status": "ok", "result": {"managers": rows, "max_enabled": 8}}))
    page.reload()
    select = page.get_by_label("Task template", exact=True)
    expect(select).to_have_value("report")
    expect(page.get_by_text("This template is unavailable.", exact=False)).to_be_visible()
    select.select_option("")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    assert state["schedules"][0]["template_key"] is None
    assert state["schedules"][0]["template_inputs"] == {}
