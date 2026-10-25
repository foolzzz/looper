"""Exercise the built dashboard with synthetic v1 data only; no Looper service or database."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from threading import Thread
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.installed


def gate_row(n):
    return {
        "task": {"task_id": f"t{n}", "name": f"Task {n}", "status": "waiting_user",
                 "group": "needs_you", "project_id": "p1", "project_name": "Example"},
        "gate": {"step": f"g{n}", "step_id": f"g{n}", "state": "open", "topic": f"Topic {n}",
                 "agenda": [{"id": "q", "question": "Continue?", "options": [], "status": "open", "answer": ""}],
                 "opened_at": "2026-10-20T10:00:00Z", "waiting_on": "user", "depends_on": []},
        "agenda_open": 1, "last_message": None,
    }


@pytest.fixture(scope="module")
def static_dashboard():
    subprocess.run(["npm", "run", "build"], cwd=ROOT / "web", check=True, timeout=180,
                   capture_output=True, text=True)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "web/dist")))
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def dashboard(static_dashboard):
    pw = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")
    with pw.sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(5000)
        page.clock.install()
        model = {"rows": [gate_row(n) for n in range(120)], "fault": None, "requests": []}
        unexpected, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def respond(route):
            path = urlsplit(route.request.url).path
            body = route.request.post_data_json
            if path == "/api/v1/ops/gates/list":
                model["requests"].append(body)
                rows = model["rows"]
                counts = {"open": len(rows), "waiting_user": max(0, len(rows) - 3),
                          "waiting_planner": min(3, len(rows)), "closed": 7, "all": len(rows) + 7}
                if body.get("waiting_on") == "user":
                    rows = rows[:counts["waiting_user"]]
                offset, limit = body.get("offset", 0), body["limit"]
                result = {"gates": rows[offset:offset + limit], "total": len(rows), "counts": counts,
                          "limit": limit, "offset": offset, "has_more": offset + limit < len(rows)}
                if offset and model["fault"]:
                    if model["fault"] == "duplicate":
                        result["gates"][0] = rows[0]
                    elif model["fault"] == "total":
                        result["total"] += 1
                    else:
                        result["gates"] = []
            elif path == "/api/v1/project_admin/list":
                result = {"projects": []}
            elif path in ("/api/v1/system/overview", "/api/v1/system/status"):
                result = {}
            elif path in ("/api/v1/operator_chat/targets", "/api/v1/operator_chat/sessions",
                          "/api/v1/operator_chat/messages", "/api/v1/roles/list",
                          "/api/v1/manager_session/get", "/api/v1/chat_settings/get"):
                # Chat stays mounted offscreen. Keep its unrelated reads unavailable.
                route.fulfill(status=503, json={"status": "error", "error": {
                    "code": "unavailable", "message": "Chat is unavailable in this gate fixture"}})
                return
            else:
                unexpected.append(path)
                route.abort()
                return
            route.fulfill(json={"status": "ok", "result": result})

        context.route("**/api/**", respond)
        try:
            yield page, model, pw.expect, static_dashboard
        finally:
            context.close()
            browser.close()
        assert not unexpected
        assert not errors


def open_gates(dashboard):
    page, model, expect, url = dashboard
    page.goto(f"{url}/#gates")
    expect(page.locator("main li")).to_have_count(50)
    expect(page.get_by_label(f"{len(model['rows']) - 3} gates waiting for you", exact=True)).to_be_visible()
    return page, model, expect


def draft_on(page, n):
    row = page.locator("main li").filter(has=page.locator(f'a[href="#tasks/t{n}"]'))
    row.get_by_role("button", name="Answer", exact=True).click()
    row.get_by_role("textbox").fill("Keep this later draft")


def refresh(page):
    page.clock.fast_forward(10000)


@pytest.mark.parametrize("insert", [False, True])
def test_closed_loaded_window_retains_unread_gates_and_later_draft(dashboard, insert):
    page, model, expect = open_gates(dashboard)
    model["rows"] = ([gate_row("new")] if insert else []) + model["rows"][50:]
    refresh(page)
    expect(page.locator("main li")).to_have_count(int(insert))
    if insert:
        expect(page.locator('main a[href="#tasks/tnew"]')).to_have_count(1)
        screenshot = ROOT / "output/playwright/gate-unread-boundary.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot))
    # A second refresh must retain even an empty loaded window's unread cutoff.
    refresh(page)
    expect(page.locator("main li")).to_have_count(int(insert))
    more = page.get_by_role("button", name="Show more", exact=True)
    more.click()
    expect(page.locator("main li")).to_have_count(50 + int(insert))
    expect(page.locator('main a[href="#tasks/t50"]')).to_have_count(1)
    draft_on(page, 90)
    model["rows"].insert(0, gate_row("newer"))
    refresh(page)
    expect(page.locator("main li")).to_have_count(51 + int(insert))
    expect(page.get_by_role("textbox")).to_have_value("Keep this later draft")
    more.click()
    expect(page.locator("main li")).to_have_count(71 + int(insert))
    expect(more).to_have_count(0)
    expect(page.get_by_role("textbox")).to_have_value("Keep this later draft")
    expect(page.get_by_role("alert")).to_have_count(0)


def test_badge_and_initial_page_read_only_the_requested_window(dashboard):
    dashboard[1]["rows"] = [gate_row(n) for n in range(700)]
    page, model, expect = open_gates(dashboard)
    badge = [r for r in model["requests"] if r.get("waiting_on") == "user"]
    initial = [r for r in model["requests"] if "waiting_on" not in r]
    assert badge == [{"state": "open", "waiting_on": "user", "limit": 2, "offset": 0}]
    assert initial == [{"state": "open", "limit": 51, "offset": 0}]
    expect(page.get_by_text("700 open · 697 waiting for you", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Show more", exact=True)).to_be_visible()


def test_empty_more_preserves_boundary_and_draft_then_refresh_restores_more(dashboard):
    page, model, expect = open_gates(dashboard)
    more = page.get_by_role("button", name="Show more", exact=True)
    more.click()
    expect(page.locator("main li")).to_have_count(100)
    draft_on(page, 90)
    # Close a loaded row and all unread rows, while a new gate opens ahead of the boundary.
    model["rows"] = [gate_row("new"), gate_row("newer"), *model["rows"][1:100]]
    more.click()
    expect(more).to_have_count(0)
    expect(page.locator("main li")).to_have_count(100)
    refresh(page)
    expect(page.locator('main a[href="#tasks/tnew"]')).to_have_count(1)
    expect(page.get_by_role("textbox")).to_have_value("Keep this later draft")
    expect(page.locator("main li")).to_have_count(101)
    expect(more).to_have_count(0)
    # The next first-page reconstruction discovers data beyond the saved boundary.
    model["rows"].append(gate_row("tail"))
    refresh(page)
    expect(more).to_be_visible()
    more.click()
    expect(page.locator("main li")).to_have_count(102)
    expect(more).to_have_count(0)
    expect(page.get_by_role("textbox")).to_have_value("Keep this later draft")
    page.set_viewport_size({"width": 768, "height": 900})
    page.get_by_role("textbox").scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.locator("main li").filter(has=page.locator('a[href="#tasks/t90"]')).scroll_into_view_if_needed()
    screenshot = ROOT / "output/playwright/gate-pagination.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot))


@pytest.mark.parametrize("change", ["insert", "close"])
def test_cross_page_refresh_keeps_surviving_loaded_draft(dashboard, change):
    dashboard[1]["rows"] = [gate_row(n) for n in range(250)]
    page, model, expect = open_gates(dashboard)
    for count in (100, 150, 200, 250):
        page.get_by_role("button", name="Show more", exact=True).click()
        expect(page.locator("main li")).to_have_count(count)
    draft_on(page, 240)
    if change == "insert":
        model["rows"].insert(0, gate_row("new"))
    else:
        model["rows"].pop(0)
    refresh(page)
    expect(page.locator("main li")).to_have_count(251 if change == "insert" else 249)
    expect(page.get_by_role("textbox")).to_have_value("Keep this later draft")


@pytest.mark.parametrize("fault", ["duplicate", "total", "empty"])
def test_inconsistent_snapshot_keeps_rows_and_draft_until_retry(dashboard, fault):
    dashboard[1]["rows"] = [gate_row(n) for n in range(250)]
    page, model, expect = open_gates(dashboard)
    page.get_by_role("button", name="Show more", exact=True).click()
    expect(page.locator("main li")).to_have_count(100)
    draft_on(page, 90)
    model["fault"] = fault
    refresh(page)
    expect(page.get_by_role("alert")).to_contain_text("Retry")
    expect(page.locator("main li")).to_have_count(100)
    expect(page.get_by_role("textbox")).to_have_value("Keep this later draft")
    model["fault"] = None
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(page.locator("main li")).to_have_count(100)
    expect(page.get_by_role("textbox")).to_have_value("Keep this later draft")
