"""Portable Manager graph through the built dashboard, with synthetic API responses."""
from pathlib import Path
import json

import pytest

from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401
from tests.test_configuration_download_browser import read_download

pytestmark = pytest.mark.installed


def graph(bots=False):
    document = {"format": "looper-config/v4", "secrets": "omitted", "managers": [
        {"id": "manager", "name": "Default", "description": "Default metadata"},
        {"id": "manager-manager", "name": "Release coordinator", "system_prompt": "Coordinate releases.",
         "tools": "tasks", "task_scope": "own", "task_policy": "templates_only", "enabled": False,
         "templates": [{"key": "release", "name": "Prepare release", "requirement": "Check {{version}}.",
                        "repoint": True, "inputs": [{"key": "version", "label": "Version", "kind": "text",
                                                     "required": True}]}]},
    ]}
    if bots:
        document["channels"] = [
            {"channel_id": "channel-telegram-41", "channel_type": "telegram", "manager_id": "manager",
             "display_name": "Default alerts", "enabled": True, "config": {}},
            {"channel_id": "channel-telegram-42", "channel_type": "telegram", "manager_id": "manager-manager",
             "display_name": "Release alerts", "enabled": True, "config": {}},
        ]
    return document


def plan(applied=False, bots=False):
    result = {"applied": applied, "counts": {section: {"added": 1, "changed": 0, "unchanged": 0, "skipped": 0}
                                         for section in ("managers", "manager.templates")},
            "changes": [{"section": "managers", "key": "manager-manager", "action": "added"},
                        {"section": "manager.templates", "key": "manager-manager/release", "action": "added"}],
            "need_secret": [], "notes": ["Template manager-manager/release requires a target working directory."]}
    if bots:
        result["counts"]["channels"] = {"added": 2, "changed": 0, "unchanged": 0, "skipped": 0}
        for entry in graph(bots=True)["channels"]:
            channel_id = entry["channel_id"]
            result["changes"].append({"section": "channels", "key": channel_id, "action": "added"})
            result["need_secret"].append({"section": "channels", "key": channel_id, "names": ["bot_token"],
                                           "target": {"kind": "channel", "id": channel_id}})
        result["notes"].append("New channel bots are disabled; configure local policy and chat bindings before activation.")
    return result


@pytest.fixture
def dashboard(request):
    pw = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")
    with pw.sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.set_default_timeout(5000)
        state = {"calls": [], "export": [], "invalid": False, "document": graph()}
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def respond(route):
            path = route.request.url.split("/api/v1/")[-1]
            state["calls"].append((path, route.request.post_data_json))
            if path == "config/export":
                state["export"].append(route)
                return
            if path == "config/import_upload":
                assert route.request.post_data_json == state["document"]
                if state["invalid"]:
                    route.fulfill(status=400, json={"status": "error", "error": {
                        "code": "invalid", "message": "Manager template release: project Missing project does not exist"}})
                    return
                result = {"import_id": "manager-import", "bytes": 700, "file": {"format": "looper-config/v4"},
                          "expires_at": "2030-01-01T12:00:00Z", "plan": plan(bots="channels" in state["document"])}
            elif path == "config/import_apply":
                assert route.request.post_data_json == {"import_id": "manager-import"}
                result = {"import_id": "manager-import", "plan": plan(applied=True, bots="channels" in state["document"])}
            elif path == "config/import_cancel":
                result = {"deleted": "manager-import"}
            else:
                result = {"project_admin/list": {"projects": []}, "system/overview": {}, "system/status": {},
                          "ops/gates/list": {"gates": [], "total": 0},
                          "chat_settings/get": {"stop_enabled": False}}.get(path)
                if result is None:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside the synthetic Manager graph fixture"}})
                    return
            route.fulfill(json={"status": "ok", "result": result})

        context.route("**/api/**", respond)
        try:
            page.goto(f"{request.getfixturevalue('dashboard_url')}/#settings/transfer")
            yield page, state, pw.expect
            assert not errors
        finally:
            for route in state["export"]:
                route.abort()
            context.close()
            browser.close()


def upload(page):
    page.locator('input[type="file"]').set_input_files({
        "name": "manager-config.json", "mimeType": "application/json", "buffer": json.dumps(graph()).encode()})


def test_manager_graph_cancel_before_apply(dashboard):
    page, state, expect = dashboard
    upload(page)
    expect(page.get_by_role("rowheader", name="Managers", exact=True)).to_be_visible()
    expect(page.get_by_role("rowheader", name="Templates", exact=True)).to_be_visible()
    expect(page.get_by_text("Note: Template manager-manager/release requires a target working directory.")).to_be_visible()
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("table")).to_have_count(0)
    assert any(path == "config/import_cancel" for path, _ in state["calls"])
    assert not any(path == "config/import_apply" for path, _ in state["calls"])


def test_manager_graph_apply_and_saved_export(dashboard, tmp_path):
    page, state, expect = dashboard
    upload(page)
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Apply 2 changes", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("2 entries written")
    page.get_by_role("button", name="Export configuration", exact=True).click()
    read_download(page, state["export"].pop(), graph(), tmp_path)
    expect(page.get_by_role("alert")).to_have_count(0)


def test_manager_graph_error_keeps_import_empty(dashboard):
    page, state, expect = dashboard
    state["invalid"] = True
    upload(page)
    expect(page.get_by_role("alert")).to_contain_text("project Missing project does not exist")
    expect(page.get_by_role("table")).to_have_count(0)
    expect(page.get_by_role("button", name="Apply 2 changes", exact=True)).to_have_count(0)


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_bot_graph_download_confirm_and_activation_guidance(dashboard, tmp_path, width):
    page, state, expect = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    state["document"] = graph(bots=True)
    page.get_by_role("button", name="Export configuration", exact=True).click()
    read_download(page, state["export"].pop(), state["document"], tmp_path)
    saved = next(tmp_path.glob("looper-config-*.json"))
    assert [row["channel_id"] for row in json.loads(saved.read_bytes())["channels"]] == ["channel-telegram-41", "channel-telegram-42"]
    page.locator('input[type="file"]').set_input_files(saved)
    expect(page.get_by_role("rowheader", name="Channel bots", exact=True)).to_be_visible()
    expect(page.get_by_text("New channel bots are disabled", exact=False)).to_be_visible()
    page.locator("summary").filter(has_text="Added").click()
    for channel_id in ("channel-telegram-41", "channel-telegram-42"):
        expect(page.locator("details li").filter(has_text=channel_id)).to_be_visible()
        link = page.locator(f'a[href="#settings/channels/{channel_id}"]')
        expect(link).to_be_visible()
        link.focus()
        expect(link).to_be_focused()
    apply = page.get_by_role("button", name="Apply 4 changes", exact=True)
    apply.focus()
    page.once("dialog", lambda dialog: dialog.accept())
    page.keyboard.press("Enter")
    expect(page.get_by_role("status")).to_contain_text("4 entries written")
    expect(page.get_by_text("Restart the server for channel credentials", exact=False)).to_be_visible()
    for channel_id in ("channel-telegram-41", "channel-telegram-42"):
        expect(page.locator(f'a[href="#settings/channels/{channel_id}"]')).to_be_visible()
    screenshot = tmp_path / f"bot-transfer-{width}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(f"Screenshot: {screenshot}")
    expect(page.get_by_role("alert")).to_have_count(0)
