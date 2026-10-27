"""Retention cleanup through the built dashboard with synthetic v1 transports."""
from pathlib import Path

import pytest

from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401
from tests.test_manager_idle_browser import manager

pytestmark = pytest.mark.installed
ROOT = Path(__file__).resolve().parents[1]


def cleanup_result(target="manager-retired", *, purged=False, eligible=True):
    return {"manager_id": target, "deleted_at": "2026-01-01T00:00:00Z", "retention_days": 30,
            "eligible": eligible, "blockers": [] if eligible else ["Manager 仍有运行记录"],
            "counts": {"threads": 2, "chat_messages": 4, "llm_records": 3,
                       "llm_record_events": 6, "manager_actions": 1}, "purged": purged}


@pytest.fixture
def dashboard(dashboard_url):  # noqa: F811 — imported shared fixture
    playwright = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")
    with playwright.sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": 1280, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(5000)
        state = {"calls": [], "purged": False, "held": [], "eligible": True}
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def respond(route):
            path = route.request.url.split("/api/v1/")[-1]
            body = route.request.post_data_json or {}
            state["calls"].append((path, body))
            if path in {"manager_cleanup/preview", "manager_cleanup/purge"}:
                if state.pop("fail", False):
                    route.fulfill(status=409, json={"status": "error", "error": {
                        "code": "conflict", "message": "Manager eligibility changed"}})
                    return
                result = cleanup_result(state.get("answer_target", body["manager_id"]),
                                        eligible=state["eligible"])
                result["retention_days"] = body["retention_days"]
                if path.endswith("purge"):
                    state["purged"] = True
                    result["purged"] = True
                elif state["purged"]:
                    result["counts"] = {key: 2 if key == "threads" else 0 for key in result["counts"]}
                if state.get("hold") == path:
                    state["held"].append((route, result))
                    return
            else:
                result = {"managers/list": {"managers": [manager("manager", "Default")], "max_enabled": 8},
                          "manager_chat_bindings/list": {"bindings": []},
                          "project_admin/list": {"projects": []}, "operator_chat/targets": {"targets": []},
                          "system/overview": {}, "system/status": {}, "ops/gates/list": {"gates": [], "total": 0},
                          "chat_settings/get": {"stop_enabled": False},
                          "registry/view": {"profiles": [], "models": [], "runtimes": [], "providers": []}}.get(path)
                if result is None:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside synthetic cleanup fixture"}})
                    return
            route.fulfill(json={"status": "ok", "result": result})

        context.route("**/api/**", respond)
        try:
            yield page, dashboard_url, state
            assert not errors
        finally:
            for route, _result in state["held"]:
                try:
                    route.abort()
                except playwright.Error:
                    pass
            context.close()
            browser.close()


def open_cleanup(page, base):
    page.goto(f"{base}/#settings/managers")
    page.get_by_role("button", name="清理已删除 Manager 的历史（仅操作员）", exact=True).click()
    page.get_by_label("已删除 Manager ID", exact=True).fill("manager-retired")


def preview(page):
    page.get_by_role("button", name="预览清理", exact=True).click()
    from playwright.sync_api import expect
    expect(page.get_by_role("status")).to_contain_text("保留期")


def purges(state):
    return [body for path, body in state["calls"] if path == "manager_cleanup/purge"]


def test_cleanup_is_collapsed_until_operator_opens_it(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.goto(f"{base}/#settings/managers")
    expect(page.get_by_label("已删除 Manager ID", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Add Manager", exact=True)).to_be_visible()
    assert not any(path.startswith("manager_cleanup/") for path, _ in state["calls"])


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_preview_explains_retention_and_retained_thread_ownership(dashboard, width):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    open_cleanup(page, base)
    field = page.get_by_label("已删除 Manager ID", exact=True)
    field.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("status")).to_contain_text("符合清理条件")
    expect(page.get_by_role("status")).to_contain_text("保留期 30 天")
    expect(page.get_by_text("保留会话归属记录：2 条（不删除）", exact=True)).to_be_visible()
    counts = page.get_by_role("definition")
    expect(counts).to_have_text(["4", "3", "6", "1"])
    expect(page.get_by_text("聊天消息正文、模型运行记录及其事件", exact=False).first).to_be_visible()
    assert not state["purged"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    screenshots = ROOT / ".local/manager-cleanup-screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.get_by_role("button", name="永久清理…", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(screenshots / f"preview-{width}.png"), full_page=True)


def test_cancel_dialog_sends_no_purge(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    open_cleanup(page, base)
    preview(page)
    button = page.get_by_role("button", name="永久清理…", exact=True)
    button.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="取消", exact=True)).to_be_focused()
    dialog.get_by_role("button", name="取消", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(button).to_be_focused()
    assert not purges(state)


def test_confirmed_purge_has_actual_counts_and_preview_readback(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    open_cleanup(page, base)
    preview(page)
    page.get_by_role("button", name="永久清理…", exact=True).click()
    page.get_by_role("button", name="确认永久清理", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("已完成永久清理")
    expect(page.get_by_text("已删除聊天消息", exact=True)).to_be_visible()
    assert purges(state) == [{"manager_id": "manager-retired", "retention_days": 30}]
    preview(page)
    expect(page.get_by_role("definition")).to_have_text(["0", "0", "0", "0"])
    expect(page.get_by_text("保留会话归属记录：2 条（不删除）", exact=True)).to_be_visible()


def test_target_change_invalidates_previous_preview(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    open_cleanup(page, base)
    preview(page)
    page.get_by_label("已删除 Manager ID", exact=True).fill("manager-another")
    expect(page.get_by_role("button", name="永久清理…", exact=True)).to_have_count(0)
    assert not purges(state)


def test_late_preview_cannot_replace_current_target(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["hold"] = "manager_cleanup/preview"
    open_cleanup(page, base)
    page.get_by_role("button", name="预览清理", exact=True).click()
    expect(page.get_by_role("button", name="正在预览…", exact=True)).to_be_disabled()
    # Wait for the fake server to hold the old request before changing ownership.
    assert state["held"]
    page.get_by_label("已删除 Manager ID", exact=True).fill("manager-another")
    state["hold"] = None
    preview(page)
    expect(page.get_by_role("status")).to_contain_text("manager-another")
    for route, result in state["held"]:
        route.fulfill(json={"status": "ok", "result": result})
    expect(page.get_by_role("status")).to_contain_text("manager-another")
    assert not purges(state)


def test_mismatched_response_cannot_enable_purge(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["answer_target"] = "manager-another"
    open_cleanup(page, base)
    page.get_by_role("button", name="预览清理", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("返回结果与当前目标不一致")
    expect(page.get_by_role("button", name="永久清理…", exact=True)).to_have_count(0)
    assert not purges(state)


def test_retention_change_requires_new_preview(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    open_cleanup(page, base)
    preview(page)
    page.get_by_label("删除后保留天数", exact=True).fill("60")
    expect(page.get_by_role("button", name="永久清理…", exact=True)).to_have_count(0)
    preview(page)
    expect(page.get_by_role("status")).to_contain_text("保留期 60 天")
    assert not purges(state)


def test_purge_in_flight_disables_repeated_submission_and_target_changes(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    open_cleanup(page, base)
    preview(page)
    state["hold"] = "manager_cleanup/purge"
    page.get_by_role("button", name="永久清理…", exact=True).click()
    page.get_by_role("button", name="确认永久清理", exact=True).click()
    expect(page.get_by_label("已删除 Manager ID", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="永久清理…", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="预览清理", exact=True)).to_be_disabled()
    assert len(purges(state)) == 1
    route, result = state["held"].pop()
    route.fulfill(json={"status": "ok", "result": result})
    expect(page.get_by_role("status")).to_contain_text("已完成永久清理")


def test_failed_purge_keeps_target_and_requires_fresh_preview(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    open_cleanup(page, base)
    preview(page)
    state["fail"] = True
    page.get_by_role("button", name="永久清理…", exact=True).click()
    page.get_by_role("button", name="确认永久清理", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Manager eligibility changed")
    expect(page.get_by_label("已删除 Manager ID", exact=True)).to_have_value("manager-retired")
    expect(page.get_by_role("button", name="永久清理…", exact=True)).to_be_disabled()
    assert not state["purged"]
    preview(page)
    expect(page.get_by_role("button", name="永久清理…", exact=True)).to_be_enabled()


def test_ineligible_preview_blocks_confirmation(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["eligible"] = False
    open_cleanup(page, base)
    preview(page)
    expect(page.get_by_role("status")).to_contain_text("暂不符合清理条件")
    expect(page.get_by_text("Manager 仍有运行记录", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="永久清理…", exact=True)).to_be_disabled()
    assert not purges(state)


def test_dialog_keyboard_escape_and_focus_are_contained_on_narrow_screen(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.set_viewport_size({"width": 390, "height": 800})
    open_cleanup(page, base)
    preview(page)
    button = page.get_by_role("button", name="永久清理…", exact=True)
    button.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="确认永久清理", exact=True)).to_be_focused()
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="取消", exact=True)).to_be_focused()
    screenshots = ROOT / ".local/manager-cleanup-screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / "confirm-390.png"))
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(button).to_be_focused()
    assert not purges(state)
