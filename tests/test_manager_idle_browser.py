"""Scenario idle settings through the built dashboard, using synthetic API responses."""

from pathlib import Path

import pytest

from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401

pytestmark = pytest.mark.installed


def manager(manager_id, name, **changes):
    builtin = manager_id == "manager"
    return {"id": manager_id, "name": name, "description": "", "builtin": builtin, "enabled": True,
            "tools": "all" if builtin else "tasks", "task_scope": "all" if builtin else "own",
            "task_policy": "free", "idle_timeout_seconds": 0 if builtin else 1800,
            "system_prompt": None if builtin else "Review synthetic reports.", "profile_id": None,
            "profile_name": None, "templates": [], "revision": 0, "created_at": "", "updated_at": "",
            "state": {"running": True, "busy": False, "healthy": True, "sync": "ok"},
            "spend": {"today_usd": 0, "last_7d_usd": 0, "runs_today": 0}, **changes}


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
        state = {"calls": [], "refuse": False, "rows": [manager("manager", "Default"),
                                                          manager("manager-review", "Review")]}
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def respond(route):
            path = route.request.url.split("/api/v1/")[-1]
            body = route.request.post_data_json or {}
            state["calls"].append((path, body))
            if path == "managers/list":
                result = {"managers": state["rows"], "max_enabled": 8}
            elif path == "manager_installs/list":
                result = {"manager_id": body["manager_id"], "installs": []}
            elif path == "managers/update":
                if state["refuse"]:
                    route.fulfill(status=400, json={"status": "error", "error": {
                        "code": "invalid", "message": "Idle timeout could not be saved"}})
                    return
                row = next(row for row in state["rows"] if row["id"] == body["manager_id"])
                row.update(body["fields"])
                result = {"manager": row}
            else:
                result = {"manager_chat_bindings/list": {"bindings": []}, "project_admin/list": {"projects": []},
                          "system/overview": {}, "system/status": {}, "ops/gates/list": {"gates": [], "total": 0},
                          "chat_settings/get": {"stop_enabled": False},
                          "registry/view": {"profiles": [], "models": [], "runtimes": [], "providers": []}}.get(path)
                if result is None:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside synthetic idle settings fixture"}})
                    return
            route.fulfill(json={"status": "ok", "result": result})

        context.route("**/api/**", respond)
        try:
            yield page, dashboard_url, state
            assert not errors
        finally:
            context.close()
            browser.close()


def edit_review(page, base):
    page.goto(f"{base}/#settings/managers")
    page.get_by_role("button", name="Edit Manager Review", exact=True).click()
    return page.get_by_label("Idle timeout (seconds)", exact=True)


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_idle_timeout_save_reload_and_keyboard(dashboard, tmp_path, width):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    field = edit_review(page, base)
    expect(field).to_have_value("1800")
    expect(page.get_by_text("0 = off; otherwise 60–86400 seconds.", exact=False)).to_be_visible()
    field.focus()
    expect(field).to_be_focused()
    field.fill("60")
    page.keyboard.press("Enter")
    expect(page.get_by_role("button", name="Save", exact=True)).to_have_count(0)
    assert state["rows"][1]["idle_timeout_seconds"] == 60
    page.reload()
    page.get_by_role("button", name="Edit Manager Review", exact=True).click()
    expect(field).to_have_value("60")
    field.scroll_into_view_if_needed()
    expect(field).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    screenshot = tmp_path / f"manager-idle-{width}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(f"Screenshot: {screenshot}")
    field.fill("0")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("button", name="Save", exact=True)).to_have_count(0)
    assert state["rows"][1]["idle_timeout_seconds"] == 0
    assert [body["fields"] for path, body in state["calls"] if path == "managers/update"] == [
        {"idle_timeout_seconds": 60}, {"idle_timeout_seconds": 0}]


def test_idle_timeout_invalid_does_not_send(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    field = edit_review(page, base)
    field.fill("59")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("0 (off) or a whole number from 60 to 86400")
    assert not any(path == "managers/update" for path, _ in state["calls"])


def test_idle_timeout_server_error_keeps_form(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["refuse"] = True
    field = edit_review(page, base)
    field.fill("60")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Idle timeout could not be saved")
    expect(field).to_have_value("60")
    assert state["rows"][1]["idle_timeout_seconds"] == 1800


def test_default_manager_form_keeps_metadata_only(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["rows"] = state["rows"][:1]
    page.goto(f"{base}/#settings/managers")
    page.get_by_role("button", name="Edit Manager Default", exact=True).click()
    expect(page.get_by_label("Idle timeout (seconds)", exact=True)).to_have_count(0)
    expect(page.get_by_label("Responsibility prompt *", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Prompt and Agent Profile: Settings → Roles")).to_be_visible()
    page.get_by_label("Name *", exact=True).fill("Primary")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("button", name="Edit Manager Primary", exact=True)).to_be_visible()
    assert next(body for path, body in state["calls"] if path == "managers/update") == {
        "manager_id": "manager", "fields": {"name": "Primary", "description": ""}}
