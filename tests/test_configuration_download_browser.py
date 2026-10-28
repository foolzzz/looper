"""Reproduction probes, not evidence of a fixed defect: built UI and saved bytes, no live API."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
from threading import Thread
from time import monotonic

import pytest

pytestmark = pytest.mark.installed


@pytest.fixture(scope="module")
def dashboard_url(staged_dashboard_dir):
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(staged_dashboard_dir)))
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
def dashboard(dashboard_url):
    pw = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")
    with pw.sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")
        browser = driver.chromium.launch(headless=True, timeout=15000)
        print(f"Chromium {browser.version}")
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(5000)
        pending, downloads, errors, cancelled = [], [], [], []
        page.on("download", lambda download: downloads.append(download))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("requestfailed", lambda request: cancelled.append(request.url))

        def respond(route):
            path = route.request.url.split("/api/v1/")[-1]
            if path == "config/export":
                assert route.request.method == "POST"
                assert route.request.post_data_json == {"include_projects": True, "include_secrets": False}
                pending.append(route)
                return
            result = {"project_admin/list": {"projects": []}, "system/overview": {},
                      "system/status": {}, "ops/gates/list": {"gates": [], "total": 0},
                      "chat_settings/get": {"stop_enabled": False}}.get(path)
            if result is None:
                route.fulfill(status=503, json={"status": "error", "error": {
                    "code": "unavailable", "message": "Outside the synthetic export fixture"}})
            else:
                route.fulfill(json={"status": "ok", "result": result})

        context.route("**/api/**", respond)
        try:
            page.goto(f"{dashboard_url}/#settings/transfer")
            yield page, pending, downloads, cancelled, pw.expect
            assert not errors
        finally:
            # Context shutdown aborts held API requests and closes any dashboard streams.
            for route in pending:
                route.abort()
            context.close()
            browser.close()


def begin_export(page, expect):
    projects = page.get_by_role("switch", name="Include projects", exact=True)
    if projects.get_attribute("aria-checked") == "false":
        projects.click()
    expect(page.get_by_role("switch", name="Include secrets", exact=True)).to_have_attribute("aria-checked", "false")
    button = page.get_by_role("button", name="Export configuration", exact=True)
    button.click()
    expect(button).to_be_disabled()
    return button


def read_download(page, route, document, tmp_path):
    with page.expect_download(timeout=15000) as event:
        route.fulfill(json={"status": "ok", "result": {"document": document}})
    download = event.value
    assert download.failure() is None, "Browser started but failed/cancelled the download"
    assert download.suggested_filename.startswith("looper-config-")
    assert download.suggested_filename.endswith(".json")
    saved = tmp_path / download.suggested_filename
    download.save_as(saved)
    data = saved.read_bytes()
    assert data == json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert json.loads(data) == document
    print(f"Saved and parsed all {len(data)} bytes")


@pytest.mark.parametrize("delay_ms", [0, 6500], ids=["immediate", "activation-expired"])
@pytest.mark.parametrize("items", [1, 5000], ids=["small", "large-catalog"])
def test_configuration_download_readback(dashboard, tmp_path, delay_ms, items):
    page, pending, downloads, _, expect = dashboard
    content = "# Synthetic skill\nUnicode: 配置 🚀\n" + "portable content\n" * 24
    document = {"format": "looper-config/v4", "secrets": "omitted", "settings": {},
                "projects": [{"name": "Synthetic project", "knowledge": [], "items": []}],
                "catalog": [{"kind": "skill", "name": f"synthetic-{n}", "origin": "upload",
                             "files": [{"path": "SKILL.md", "content": content, "executable": False}]}
                            for n in range(items)]}
    button = begin_export(page, expect)
    started = monotonic()
    if delay_ms:
        # Real elapsed time: a virtual clock would not establish transient activation expiry.
        page.wait_for_timeout(delay_ms)
    # Playwright's page.evaluate grants userGesture; sample passively and do no UI reads before fulfilling.
    cdp = page.context.new_cdp_session(page)
    active = cdp.send("Runtime.evaluate", {"expression": "navigator.userActivation.isActive",
                                         "userGesture": False, "returnByValue": True})["result"]["value"]
    cdp.detach()
    assert not delay_ms or not active
    assert not downloads
    print(f"Response held {monotonic() - started:.3f}s; activation={active}")
    read_download(page, pending.pop(), document, tmp_path)
    expect(button).to_be_enabled()
    expect(page.get_by_role("alert")).to_have_count(0)
    assert len(downloads) == 1


@pytest.mark.parametrize("failure", ["cancel", "network", "json"])
def test_unsuccessful_export_has_no_download_and_retry_works(dashboard, tmp_path, failure):
    page, pending, downloads, cancelled, expect = dashboard
    button = begin_export(page, expect)
    route = pending.pop()
    if failure == "cancel":
        with page.expect_event("requestfailed", predicate=lambda request: request.url.endswith("/config/export")):
            page.evaluate("location.hash = '#settings/chat'")
        assert route.request.url in cancelled
        # Try the late result too: a cancelled export must never produce a stale file.
        route.fulfill(json={"status": "ok", "result": {"document": {"cancelled": True}}})
        page.evaluate("location.hash = '#settings/transfer'")
    else:
        if failure == "network":
            route.abort("failed")
        else:
            route.fulfill(content_type="application/json", body="invalid JSON")
        expect(page.get_by_role("alert")).to_be_visible()
    expect(button).to_be_enabled()
    assert not downloads
    button = begin_export(page, expect)
    read_download(page, pending.pop(), {"format": "looper-config/v4", "catalog": []}, tmp_path)
    expect(button).to_be_enabled()
    expect(page.get_by_role("alert")).to_have_count(0)
    assert len(downloads) == 1
