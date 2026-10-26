"""Manager filters in the built dashboard, using public-safe synthetic v1 responses."""
from pathlib import Path

import pytest

from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401

pytestmark = pytest.mark.installed
ROOT = Path(__file__).resolve().parents[1]


def task(task_id, owner="manager", *, created="2026-10-24T00:00:00Z"):
    return {"task_id": task_id, "name": f"Synthetic {task_id}", "requirement": "Synthetic task",
            "goal_id": "", "status": "failed", "group": "attention", "phase": "failed",
            "planner_id": None, "correlation_id": "", "created_at": created, "completed_at": created,
            "project_id": "demo", "project_name": "Demo", "work_dir": "", "cost": None,
            "schedule_id": "schedule-demo", "schedule_name": "Synthetic schedule", "manager_id": owner,
            "manager_name": owner, "continues_schedule_session": False}


def fulfill(route, rows, *, cursor=None, counts=None):
    route.fulfill(json={"status": "ok", "result": {
        "tasks": rows, "counts": counts if counts is not None else {"failed": len(rows)},
        "next_before": cursor}})


@pytest.fixture
def dashboard(dashboard_url):  # noqa: F811
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.set_default_timeout(5000)
        page.clock.install()
        state = {"calls": [], "held": [], "hold": False, "error": False,
                 "managers": [{"id": "manager", "name": "Manager", "builtin": True, "enabled": True},
                              {"id": "scenario-a", "name": "Scenario A", "builtin": False, "enabled": True},
                              {"id": "scenario-b", "name": "Scenario B", "builtin": False, "enabled": False}]}
        rows = [task("default-new"), task("scenario-new", "scenario-a"),
                task("scenario-old", "scenario-a", created="2026-10-23T00:00:00Z"),
                task("disabled-new", "scenario-b")]
        state["rows"] = rows

        def api(route):
            path = route.request.url.split("/api/v1/")[-1]
            body = route.request.post_data_json or {}
            state["calls"].append((path, body))
            if path == "managers/list":
                result = {"managers": state["managers"], "max_enabled": 10}
            elif path == "project_admin/list":
                result = {"projects": [{"project_id": "demo", "name": "Demo", "work_dir": ""}]}
            elif path == "tasks/page":
                if state["hold"]:
                    state["held"].append(route)
                    page.evaluate("document.documentElement.dataset.heldTaskRequests = "
                                  "String(Number(document.documentElement.dataset.heldTaskRequests || 0) + 1)")
                    return
                if state["error"]:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Synthetic list unavailable"}})
                    return
                selected = [row for row in rows if not body.get("manager_id") or
                            row["manager_id"] == body["manager_id"]]
                counts = {"failed": len(selected)} if selected else {}
                if body.get("before"):
                    selected = selected[1:]
                fulfill(route, selected[:1], cursor=selected[0]["task_id"] if len(selected) > 1 else None,
                        counts=counts)
                return
            else:
                route.fulfill(status=503, json={"status": "error", "error": {
                    "code": "unavailable", "message": "Outside synthetic fixture"}})
                return
            route.fulfill(json={"status": "ok", "result": result})

        page.route("**/api/**", api)
        try:
            yield page, dashboard_url, state
        finally:
            for route in state["held"]:
                try:
                    route.abort()
                except playwright.Error:
                    pass  # Already fulfilled.
            context.close()
            browser.close()


def test_manager_filter_composes_and_survives_refresh_pagination_and_back(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.goto(f"{base}/#tasks?project=demo&group=attention&schedule=schedule-demo&status=failed&query=Synthetic")
    selector = page.get_by_role("combobox", name="Manager", exact=True)
    selector.select_option("scenario-a")
    expect(page.get_by_text("Synthetic scenario-new", exact=True)).to_be_visible()
    filters = {"project_id": "demo", "manager_id": "scenario-a", "group": "attention",
               "schedule_id": "schedule-demo", "status": "failed", "query": "Synthetic"}
    assert ("tasks/page", filters) in state["calls"]
    expect(page.get_by_text("2 tasks · Needs attention", exact=True)).to_be_visible()
    page.get_by_role("button", name="Load more", exact=False).click()
    expect(page.get_by_text("Synthetic scenario-old", exact=True)).to_be_visible()
    assert ("tasks/page", {**filters, "before": "scenario-new"}) in state["calls"]
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_text("Synthetic scenario-old", exact=True)).to_be_visible()
    original = page.url
    page.get_by_text("Synthetic scenario-new", exact=True).click()
    expect(page.get_by_role("button", name="Back to tasks", exact=True)).to_be_visible()
    page.get_by_role("button", name="Back to tasks", exact=True).click()
    expect(page).to_have_url(original)
    expect(selector).to_have_value("scenario-a")
    page.reload()
    expect(selector).to_have_value("scenario-a")
    expect(page.get_by_text("Synthetic scenario-new", exact=True)).to_be_visible()
    page.go_back()
    expect(page.get_by_role("button", name="Back to tasks", exact=True)).to_be_visible()


@pytest.mark.parametrize("pending", ["initial", "refresh", "more"])
def test_old_filter_response_cannot_replace_or_append_new_filter(dashboard, pending):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["hold"] = pending == "initial"
    with page.expect_request(lambda request: request.url.endswith("/tasks/page")):
        page.goto(f"{base}/#tasks?manager=scenario-a")
    selector = page.get_by_role("combobox", name="Manager", exact=True)
    if pending != "initial":
        expect(page.get_by_text("Synthetic scenario-new", exact=True)).to_be_visible()
        state["hold"] = True
        with page.expect_request(lambda request: request.url.endswith("/tasks/page")):
            page.get_by_role("button", name="Refresh" if pending == "refresh" else "Load more", exact=False).click()
    expect(page.locator("html")).to_have_attribute("data-held-task-requests", "1")
    assert state["held"]
    old = state["held"][-1]
    state["hold"] = False
    selector.select_option("scenario-b")
    expect(page.get_by_text("Synthetic disabled-new", exact=True)).to_be_visible()
    with page.expect_response(lambda response: response.request == old.request):
        fulfill(old, [task("late-old", "scenario-a")], cursor="late-cursor", counts={"failed": 99})
    page.clock.run_for(50)
    expect(page.get_by_text("Synthetic late-old", exact=True)).to_have_count(0)
    expect(page.get_by_text("Synthetic disabled-new", exact=True)).to_be_visible()
    expect(page.get_by_text("1 total tasks", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Load more", exact=False)).to_have_count(0)
    # A subsequent refresh is a positive control that current-owner results still apply.
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_text("Synthetic disabled-new", exact=True)).to_be_visible()
    expect(page.get_by_text("Synthetic late-old", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Load more", exact=False)).to_have_count(0)
    expect(page.get_by_text("1 total tasks", exact=True)).to_be_visible()


def test_latest_refresh_wins_when_responses_finish_in_reverse_order(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.goto(f"{base}/#tasks?manager=scenario-a")
    expect(page.get_by_text("Synthetic scenario-new", exact=True)).to_be_visible()
    state["hold"] = True
    for _ in range(2):
        with page.expect_request(lambda request: request.url.endswith("/tasks/page")):
            page.get_by_role("button", name="Refresh", exact=True).click()
    assert len(state["held"]) == 2
    fulfill(state["held"][1], [task("fresh-result", "scenario-a")])
    expect(page.get_by_text("Synthetic fresh-result", exact=True)).to_be_visible()
    with page.expect_response(lambda response: response.request == state["held"][0].request):
        fulfill(state["held"][0], [task("stale-result", "scenario-a")])
    page.clock.run_for(50)
    expect(page.get_by_text("Synthetic fresh-result", exact=True)).to_be_visible()
    expect(page.get_by_text("Synthetic stale-result", exact=True)).to_have_count(0)


def test_default_only_install_hides_selector_until_explicit_filter(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["managers"] = state["managers"][:1]
    page.goto(f"{base}/#tasks")
    expect(page.get_by_text("Synthetic default-new", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="Manager", exact=True)).to_have_count(0)
    assert all("manager_id" not in body for path, body in state["calls"] if path == "tasks/page")
    page.goto(f"{base}/#tasks?manager=manager")
    expect(page.get_by_role("combobox", name="Manager", exact=True)).to_have_value("manager")
    expect(page.get_by_text("1 total tasks", exact=True)).to_be_visible()
    assert ("tasks/page", {"manager_id": "manager"}) in state["calls"]


def test_unavailable_owner_keeps_empty_filter_until_removed(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.goto(f"{base}/#tasks?manager=removed-owner")
    selector = page.get_by_role("combobox", name="Manager", exact=True)
    expect(selector).to_have_value("removed-owner")
    expect(selector.locator("option:checked")).to_have_text("removed-owner (unavailable)")
    expect(page.get_by_role("status")).to_contain_text("The Manager filter is still applied")
    expect(page.get_by_text("No tasks yet", exact=True)).to_be_visible()
    assert ("tasks/page", {"manager_id": "removed-owner"}) in state["calls"]
    page.get_by_role("button", name="Remove Manager filter", exact=True).click()
    expect(selector).to_have_value("")
    expect(page.get_by_text("Synthetic default-new", exact=True)).to_be_visible()
    expect(page).to_have_url(f"{base}/#tasks")


def test_disabled_manager_keyboard_narrow_and_error_states(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.goto(f"{base}/#tasks")
    selector = page.get_by_role("combobox", name="Manager", exact=True)
    expect(selector.locator('option[value="scenario-b"]')).to_have_text("Scenario B (disabled)")
    selector.focus()
    page.keyboard.type("Scenario B")
    expect(selector).to_be_focused()
    expect(selector).to_have_value("scenario-b")
    expect(page.get_by_text("Synthetic disabled-new", exact=True)).to_be_visible()
    shots = ROOT / "output/playwright"
    shots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shots / "task-manager-filter-desktop.png"))
    page.set_viewport_size({"width": 768, "height": 800})
    expect(selector).to_be_visible()
    page.screenshot(path=str(shots / "task-manager-filter-narrow.png"))
    state["error"] = True
    page.goto(f"{base}/#tasks?manager=scenario-a")
    expect(page.get_by_text("Synthetic list unavailable", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="Manager", exact=True)).to_have_value("scenario-a")
    assert ("tasks/page", {"manager_id": "scenario-a"}) in state["calls"]
