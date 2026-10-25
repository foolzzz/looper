"""Conversation ownership through the built dashboard, using synthetic v1 requests only."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import subprocess
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.installed


def session(sid, channel="web"):
    return {"session_id": sid, "channel": channel, "message_count": 1,
            "first_message_at": "2026-01-01T00:00:00Z", "last_message_at": "2026-01-01T00:00:00Z",
            "last_role": "user", "last_content": f"History of {sid}"}


@pytest.fixture(scope="module")
def dashboard_url():
    subprocess.run(["npm", "run", "build"], cwd=ROOT / "web", check=True, timeout=180, capture_output=True)

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
def dashboard(dashboard_url):
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.set_default_timeout(5000)
        page.clock.install()
        page.add_init_script("localStorage.setItem('looper-chat-session-id', 'session-b')")
        calls, held, cancelled = [], [], []
        page.on("requestfailed", lambda request: cancelled.append(request.url))
        rows = [session("session-a"), session("session-b"), session("lark:known", "lark"),
                session("planner-known")]
        state = {"hold": True, "calls": calls, "held": held, "cancelled": cancelled}

        def api(route):
            path = route.request.url.split("/api/v1/")[-1]
            body = route.request.post_data_json or {}
            calls.append((path, body))
            if path == "operator_chat/targets":
                result = {"targets": [{"kind": "manager", "id": "manager", "can_send": True}]}
            elif path == "operator_chat/sessions":
                result = {"sessions": [r for r in rows if not r["session_id"].startswith("planner-") and
                          (not body.get("session_id") or r["session_id"] == body["session_id"])]}
            elif path == "operator_chat/messages":
                sid = body["session_id"]
                result = {"messages": [{"message_id": f"{sid}-old", "role": "user",
                          "content": f"History of {sid}", "created_at": "2026-01-01T00:00:00Z"}] if sid in {r["session_id"] for r in rows} else []}
            elif path == "operator_chat/turn":
                result = {"turn": None}
            elif path == "roles/list":
                result = {"configs": []}
            elif path == "project_admin/list":
                result = {"projects": []}
            elif path == "chat_settings/get":
                result = {"stop_enabled": False}
            elif path == "operator_chat/send":
                if state["hold"]:
                    held.append(route)
                    return
                result = {"correlation_id": "reply-b"}
            elif path == "operator_chat/reply_events":
                text = "Late A reply" if body["correlation_id"] == "reply-a" else "Current B reply"
                events = [("agent.phase", {"label": text}), ("answer.delta", {"text": text}),
                          ("turn.completed", {}), ("answer.final", {"text": text, "outcome": "answered"})]
                route.fulfill(content_type="text/event-stream", body="".join(
                    f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in events))
                return
            elif path == "operator_chat/thread_events":
                held.append(route)
                return
            elif path == "manager_session/get":
                result = {"session": None, "selected": None, "effective": {"profile": None, "source": "system"}, "choices": []}
            else:
                route.fulfill(status=503, json={"status": "error", "error": {"code": "unavailable", "message": "Outside fixture"}})
                return
            route.fulfill(json={"status": "ok", "result": result})

        page.route("**/api/**", api)
        try:
            yield page, dashboard_url, state
        finally:
            # Abort every deferred request and open thread stream before closing Chromium.
            for route in held:
                try:
                    route.abort()
                except Error:
                    pass  # Already fulfilled or cancelled by navigation.
            context.close()
            browser.close()


def test_back_during_deferred_send_keeps_the_restored_conversation(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.goto(f"{base}/#chat?conversation=session-b")
    expect(page.get_by_role("textbox")).to_be_visible()
    page.locator('aside button[title="session-a"]').click()
    expect(page.locator('aside button[title="session-a"]')).to_have_attribute("aria-current", "true")
    page.get_by_role("textbox").fill("Synthetic send A")
    page.get_by_role("textbox").press("Enter")
    page.wait_for_function("document.body.innerText.includes('Synthetic send A')")
    pending = next(r for r in state["held"] if r.request.url.endswith("/send"))
    with page.expect_response(lambda r: r.url.endswith("/operator_chat/turn") and
                              r.request.post_data_json.get("session_id") == "session-b"):
        page.go_back()
    expect(page.locator('aside button[title="session-b"]')).to_have_attribute("aria-current", "true")
    page.get_by_role("textbox").fill("B draft")
    turns = sum(path == "operator_chat/turn" for path, _ in state["calls"])
    pending.fulfill(json={"status": "ok", "result": {"correlation_id": "reply-a"}})
    page.clock.run_for(3100)
    assert any(url.endswith("/operator_chat/send") for url in state["cancelled"])
    expect(page.get_by_role("textbox")).to_have_value("B draft")
    expect(page.get_by_text("Late A reply", exact=True)).to_have_count(0)
    assert not any(path == "operator_chat/reply_events" for path, _ in state["calls"])
    assert not any(path == "operator_chat/save_message" for path, _ in state["calls"])
    assert sum(path == "operator_chat/turn" for path, _ in state["calls"]) == turns
    state["hold"] = False
    page.get_by_role("textbox").press("Enter")
    expect(page.get_by_text("Current B reply", exact=True)).to_be_visible()
    sends = [body for path, body in state["calls"] if path == "operator_chat/send"]
    assert [body["session_id"] for body in sends] == ["session-a", "session-b"]


def test_missing_link_keeps_owner_and_valid_local_and_readonly_threads_work(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.goto(f"{base}/#chat?conversation=unknown-web-id")
    expect(page.get_by_role("alert")).to_contain_text("not found")
    assert page.evaluate("localStorage.getItem('looper-chat-session-id')") == "session-b"
    expect(page.get_by_role("textbox")).to_have_count(0)
    assert not any(body.get("session_id") == "unknown-web-id" and path in {
        "operator_chat/messages", "operator_chat/send"} for path, body in state["calls"])
    shot = ROOT / "output/playwright/chat-navigation.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))
    page.evaluate("location.hash = '#chat?conversation=lark%3Aknown'")
    expect(page.locator('[data-viewing="lark:known"]')).to_be_visible()
    expect(page.get_by_role("textbox")).to_have_count(0)
    page.evaluate("location.hash = '#chat?conversation=planner-known'")
    expect(page.get_by_role("alert")).to_contain_text("not found")
    expect(page.get_by_role("textbox")).to_have_count(0)
    assert page.evaluate("localStorage.getItem('looper-chat-session-id')") == "session-b"
    page.evaluate("location.hash = '#chat?conversation=session-a'")
    expect(page.get_by_role("textbox")).to_be_visible()
    assert page.evaluate("localStorage.getItem('looper-chat-session-id')") == "session-a"
    page.get_by_title("New conversation:", exact=False).click()
    fresh = page.evaluate("localStorage.getItem('looper-chat-session-id')")
    assert fresh not in {"session-a", "session-b", "unknown-web-id"}
    expect(page.get_by_role("textbox")).to_be_visible()
    state["hold"] = False
    page.get_by_role("textbox").fill("Synthetic local thread")
    page.get_by_role("textbox").press("Enter")
    expect(page.get_by_text("Current B reply", exact=True)).to_be_visible()
    assert [body["session_id"] for path, body in state["calls"] if path == "operator_chat/send"] == [fresh]
