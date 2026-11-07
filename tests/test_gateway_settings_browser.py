"""Local gateway settings on the built dashboard, with synthetic operator API replies."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from threading import Thread

import pytest

from looper.gateway_env import gateway_environment

pytestmark = pytest.mark.installed


@pytest.fixture(scope="module")
def gateway_dashboard(staged_dashboard_dir):
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


@pytest.fixture(scope="module")
def gateway_browser():
    pw = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        try:
            yield browser, pw.expect
        finally:
            browser.close()


def _settings():
    usage = {"requests": 12, "input_tokens": 20, "cache_read_input_tokens": 80,
             "cache_creation_input_tokens": 0, "output_tokens": 10}
    return {"enabled": True, "port": 32991, "url": "http://127.0.0.1:32991/gw", "key_exists": True,
            "aliases": [{"alias": "local-chat", "model_id": "chat", "model_name": "Chat model",
                         "provider_id": "example", "provider_name": "Example", "enabled": True}],
            "proxy": {"state": "running", "port": 32991}, "translate": usage,
            "client_environment": {"local-chat": gateway_environment("http://127.0.0.1:32991/gw", "local-chat")},
            "auth_failures": 0, "alias_usage": {"local-chat": usage}}


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("state", ["populated", "empty", "error", "unavailable"])
def test_local_gateway_settings_layout(gateway_dashboard, gateway_browser, width, state):
    browser, expect = gateway_browser
    context = browser.new_context(viewport={"width": width, "height": 1000}, reduced_motion="reduce")
    page = context.new_page()
    page.set_default_timeout(5000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    settings = _settings()
    if state == "empty":
        settings.update(enabled=False, url=None, aliases=[], alias_usage={}, client_environment={})
    if state == "unavailable":
        settings["proxy"]["state"] = "unavailable"
    registry = {"providers": [{"provider_id": "example", "enabled": True, "api_key": "***", "auth": "api_key"}],
                "models": [{"model_id": "chat", "provider_id": "example", "provider_name": "Example",
                            "name": "Chat model", "protocol": "openai-chat"}]}

    def respond(route):
        operation = route.request.url.split("/api/v1/")[-1]
        if operation == "gateway/get" and state == "error":
            route.fulfill(status=503, json={"status": "error", "error": {
                "code": "unavailable", "message": "Gateway configuration unavailable"}})
            return
        result = {"gateway/get": settings, "registry/view": registry,
                  "project_admin/list": {"projects": []}, "system/overview": {}, "system/status": {},
                  "ops/gates/list": {"gates": [], "total": 0}, "chat_settings/get": {"stop_enabled": False}}.get(operation)
        if result is None:
            route.fulfill(status=503, json={"status": "error", "error": {
                "code": "unavailable", "message": "Outside the synthetic gateway fixture"}})
        else:
            route.fulfill(json={"status": "ok", "result": result})

    context.route("**/api/**", respond)
    try:
        page.goto(f"{gateway_dashboard}/#settings/gateway")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("heading", name="Local gateway", exact=True).first).to_be_visible()
        if state == "error":
            expect(page.get_by_role("alert")).to_contain_text("Gateway configuration unavailable")
            expect(page.get_by_role("button", name="Retry", exact=True)).to_be_visible()
        else:
            expect(page.get_by_role("switch", name="Enable local gateway")).to_have_attribute(
                "aria-checked", "false" if state == "empty" else "true")
            if state == "empty":
                expect(page.get_by_text("No aliases yet.", exact=False)).to_be_visible()
                expect(page.get_by_text("Port 32991", exact=True)).to_be_visible()
            else:
                expect(page.get_by_label("Model for local-chat")).to_be_visible()
                expect(page.get_by_text("12 requests · Cache hit 80%", exact=False)).to_be_visible()
                expect(page.locator("pre code")).to_have_text(settings["client_environment"]["local-chat"])
            expect(page.get_by_text("All translated requests through this proxy, including Looper agents and local clients.")).to_be_visible()
            if state == "unavailable":
                expect(page.get_by_text("Proxy unavailable", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "page overflows horizontally"
        assert not errors
        if state == "populated" and width == 1440 and (screenshot := os.environ.get("LOOPER_GATEWAY_SETTINGS_SCREENSHOT")):
            page.set_viewport_size({"width": width, "height": 1700})
            page.screenshot(path=screenshot, full_page=True)
    finally:
        context.close()
