"""Project-scoped Manager settings through the built dashboard with synthetic data."""

import pytest

from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401
from tests.test_manager_idle_browser import dashboard, manager  # noqa: F401

pytestmark = pytest.mark.installed


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_project_scope_form_and_emptied_badge(dashboard, tmp_path, width):  # noqa: F811
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    state["rows"][1] = manager("manager-review", "Review", task_scope="projects", scope_project_ids=["proj-review"])
    page.route("**/api/v1/project_admin/list", lambda route: route.fulfill(json={"status": "ok", "result": {
        "projects": [{"project_id": "proj-review", "name": "Reviews", "description": "",
                      "task_count": 0, "active_count": 0}], "projects_root": ""}}))
    page.goto(f"{base}/#settings/managers")
    expect(page.get_by_text("projects: Reviews", exact=True)).to_be_visible()
    page.get_by_role("button", name="Edit Manager Review", exact=True).click()
    expect(page.get_by_label("Task visibility", exact=True)).to_have_value("projects")
    expect(page.get_by_label("Tools", exact=True)).to_have_value("tasks")
    expect(page.get_by_label("Tools", exact=True)).to_be_disabled()
    project = page.get_by_role("checkbox", name="Reviews", exact=True)
    expect(project).to_be_checked()
    project.focus()
    expect(project).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    screenshot = tmp_path / f"manager-project-scope-{width}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(f"Screenshot: {screenshot}")
    project.uncheck()
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Choose at least one project.")
    assert not any(path == "managers/update" for path, _ in state["calls"])
    project.check()
    page.get_by_role("button", name="Cancel", exact=True).click()
    state["rows"][1]["scope_project_ids"] = []
    page.reload()
    warning = page.get_by_text("no projects", exact=True)
    expect(warning).to_be_visible()
    expect(warning).to_have_attribute("title", "Its projects were deleted: it sees and creates no task until you choose them.")
    screenshot = tmp_path / f"manager-no-projects-{width}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(f"Screenshot: {screenshot}")
    page.get_by_role("button", name="Edit Manager Review", exact=True).click()
    expect(page.get_by_text("Its projects were deleted: it sees and creates no task until you choose them.", exact=True)).to_be_visible()
    page.get_by_label("Name *", exact=True).fill("Renamed")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("button", name="Edit Manager Renamed", exact=True)).to_be_visible()
    assert next(body["fields"] for path, body in state["calls"] if path == "managers/update") == {"name": "Renamed"}
