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
        page.add_init_script("""if (localStorage.getItem('looper-chat-session-id') === null) {
            localStorage.setItem('looper-chat-session-id', 'session-b');
        }""")
        calls, held, cancelled = [], [], []
        page.on("requestfailed", lambda request: cancelled.append(request.url))
        rows = [session("session-a"), session("session-b"), session("lark:known", "lark"),
                session("telegram:known", "telegram"),
                session("planner-known")]
        state = {"hold": True, "calls": calls, "held": held, "cancelled": cancelled, "rows": rows}

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
                result = {"turn": state.get("turn") if body["session_id"] == "session-b" else None}
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
                if state.get("hold_reply"):
                    held.append(route)
                    return
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


@pytest.mark.parametrize("stage", ["send", "reply"])
@pytest.mark.parametrize("via_tasks", [False, True])
def test_returning_to_same_thread_keeps_send_and_queue(dashboard, stage, via_tasks):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["hold_reply"] = True
    page.goto(f"{base}/#chat?conversation=session-b")
    page.get_by_role("textbox").fill("Synthetic original turn")
    with page.expect_request(lambda r: r.url.endswith("/operator_chat/send")):
        page.get_by_role("textbox").press("Enter")
    expect(page.get_by_text("Synthetic original turn", exact=True)).to_be_visible()
    pending = next(r for r in state["held"] if r.request.url.endswith("/send"))
    if stage == "reply":
        with page.expect_request(lambda r: r.url.endswith("/operator_chat/reply_events")):
            pending.fulfill(json={"status": "ok", "result": {"correlation_id": "reply-a"}})
    navigation = page.get_by_role("complementary", name="Navigation")
    if via_tasks:
        navigation.get_by_role("button", name="Tasks", exact=True).click()
        expect(page).to_have_url(f"{base}/#tasks")
    navigation.get_by_role("button", name="Chat", exact=True).click()
    expect(page).to_have_url(f"{base}/#chat?conversation=session-b")
    page.get_by_role("textbox").fill("Synthetic queued turn")
    expect(page.get_by_role("button", name="Queue message", exact=True)).to_be_visible()
    page.get_by_role("textbox").press("Enter")
    expect(page.get_by_text("Queued · sent when the reply ends", exact=True)).to_be_visible()
    assert sum(path == "operator_chat/send" for path, _ in state["calls"]) == 1
    assert not any(url.endswith(("/operator_chat/send", "/operator_chat/reply_events"))
                   for url in state["cancelled"])
    state["hold"] = state["hold_reply"] = False
    if stage == "send":
        pending.fulfill(json={"status": "ok", "result": {"correlation_id": "reply-a"}})
    else:
        pending = next(r for r in state["held"] if r.request.url.endswith("/reply_events"))
        pending.fulfill(content_type="text/event-stream", body=
                        'event: answer.final\ndata: {"text":"Late A reply","outcome":"answered"}\n\n')
    expect(page.get_by_text("Late A reply", exact=True)).to_be_visible()
    expect(page.get_by_text("Current B reply", exact=True)).to_be_visible()
    sends = [body for path, body in state["calls"] if path == "operator_chat/send"]
    assert [body["session_id"] for body in sends] == ["session-b", "session-b"]


def open_chat_link(page, conversation):
    # Follow an ordinary link, so Back restores the original history entry.
    page.evaluate("""id => {
        const link = document.createElement('a');
        link.href = '#chat?conversation=' + encodeURIComponent(id);
        link.textContent = 'Open linked conversation';
        document.body.append(link);
    }""", conversation)
    page.get_by_role("link", name="Open linked conversation", exact=True).click()
    page.get_by_role("link", name="Open linked conversation", exact=True).evaluate("e => e.remove()")


@pytest.mark.parametrize("destination", ["lark:known", "unknown-web-id"])
@pytest.mark.parametrize("stage", ["send", "accepted", "rejected"])
def test_pending_submission_survives_transcript_navigation(dashboard, destination, stage):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["hold_reply"] = True
    page.goto(f"{base}/#chat?conversation=session-b")
    text = "Synthetic pending submission"
    page.get_by_role("textbox").fill(text)
    with page.expect_request(lambda r: r.url.endswith("/operator_chat/send")):
        page.get_by_role("textbox").press("Enter")
    pending = next(r for r in state["held"] if r.request.url.endswith("/send"))
    if stage == "accepted":
        with page.expect_request(lambda r: r.url.endswith("/operator_chat/reply_events")):
            pending.fulfill(json={"status": "ok", "result": {"correlation_id": "reply-a"}})
    if stage == "rejected":
        page.get_by_role("textbox").fill("Synthetic queued followup")
        page.get_by_role("textbox").press("Enter")
        page.get_by_role("textbox").fill("Newer draft")
    open_chat_link(page, destination)
    if destination == "lark:known":
        expect(page.locator('[data-viewing="lark:known"]')).to_be_visible()
    else:
        expect(page.get_by_role("alert")).to_contain_text("not found")
    expect(page.get_by_role("textbox")).to_have_count(0)
    assert page.evaluate("localStorage.getItem('looper-chat-session-id')") == "session-b"
    assert not any(url.endswith(("/operator_chat/send", "/operator_chat/reply_events"))
                   for url in state["cancelled"])
    if stage == "rejected":
        pending.fulfill(status=409, json={"status": "error", "error": {
            "code": "conflict", "message": "Synthetic request was not accepted"}})
    elif stage == "accepted":
        reply = next(r for r in state["held"] if r.request.url.endswith("/reply_events"))
        reply.fulfill(content_type="text/event-stream", body=
                      'event: answer.final\ndata: {"text":"Synthetic answer","outcome":"answered"}\n\n')
    page.go_back()
    expect(page).to_have_url(f"{base}/#chat?conversation=session-b")
    expect(page.get_by_role("textbox")).to_be_visible()
    if stage == "send":
        state["hold_reply"] = False
        pending.fulfill(json={"status": "ok", "result": {"correlation_id": "reply-a"}})
    if stage == "rejected":
        expect(page.get_by_role("textbox")).to_have_value(
            f"{text}\n\nSynthetic queued followup\n\nNewer draft")
        expect(page.get_by_text(text, exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Remove queued message")).to_have_count(0)
        cached = page.evaluate("JSON.parse(localStorage.getItem('looper-chat-messages-session-b'))")
        assert [m["role"] for m in cached] == ["user"]  # Only the original history, no empty assistant.
    else:
        expect(page.get_by_text("Late A reply" if stage == "send" else "Synthetic answer", exact=True)).to_have_count(1)
        expect(page.get_by_text(text, exact=True)).to_have_count(1)
        expect(page.get_by_role("textbox")).to_have_value("")
    # Repeated navigation must neither resend nor restore the text a second time.
    page.go_forward()
    expect(page.get_by_role("textbox")).to_have_count(0)
    page.go_back()
    expect(page.get_by_role("textbox")).to_be_visible()
    if stage == "rejected":
        expect(page.get_by_role("textbox")).to_have_value(f"{text}\n\nSynthetic queued followup\n\nNewer draft")
        page.locator('aside button[title="session-a"]').click()
        expect(page.get_by_role("textbox")).to_have_value("")
    assert sum(path == "operator_chat/send" for path, _ in state["calls"]) == 1
    if destination == "lark:known" and stage == "send":
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 800})
            expect(page.get_by_role("textbox")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        shot = ROOT / "output/playwright/chat-pending-navigation.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot))


@pytest.mark.parametrize("destination", ["lark:known", "telegram:known", "unknown-web-id"])
def test_answered_while_hidden_restores_queue_to_original_draft(dashboard, destination):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["hold"] = False
    state["hold_reply"] = True
    page.goto(f"{base}/#chat?conversation=session-b")
    page.get_by_role("textbox").fill("Synthetic original turn")
    with page.expect_request(lambda r: r.url.endswith("/operator_chat/reply_events")):
        page.get_by_role("textbox").press("Enter")
    for text in ("Synthetic queued first", "Synthetic queued second"):
        page.get_by_role("textbox").fill(text)
        page.get_by_role("textbox").press("Enter")
    expect(page.get_by_role("button", name="Remove queued message")).to_have_count(2)
    page.get_by_role("textbox").fill("Newer draft")
    open_chat_link(page, destination)
    if destination == "unknown-web-id":
        expect(page.get_by_role("alert")).to_contain_text("not found")
    else:
        expect(page.locator(f'[data-viewing="{destination}"]')).to_be_visible()
    expect(page.get_by_role("textbox")).to_have_count(0)
    reply = next(r for r in state["held"] if r.request.url.endswith("/reply_events"))
    reply.fulfill(content_type="text/event-stream", body=
                  'event: answer.final\ndata: {"text":"Synthetic completed answer","outcome":"answered"}\n\n')
    # Wait for the final answer to be applied while the original composer is still hidden.
    page.wait_for_function("""() => JSON.parse(localStorage.getItem('looper-chat-messages-session-b'))
        .some(m => m.content === 'Synthetic completed answer')""")
    assert sum(path == "operator_chat/send" for path, _ in state["calls"]) == 1
    restored = "Synthetic queued first\n\nSynthetic queued second\n\nNewer draft"
    page.go_back()
    expect(page).to_have_url(f"{base}/#chat?conversation=session-b")
    expect(page.get_by_role("textbox")).to_have_value(restored)
    expect(page.get_by_role("button", name="Remove queued message")).to_have_count(0)
    assert page.evaluate("sessionStorage.getItem('looper-chat-staged-session-b')") is None
    expect(page.get_by_text("Synthetic completed answer", exact=True)).to_be_visible()
    page.go_forward()
    expect(page.get_by_role("textbox")).to_have_count(0)
    page.go_back()
    expect(page.get_by_role("textbox")).to_have_value(restored)
    state["hold_reply"] = False
    page.get_by_role("textbox").press("Enter")
    expect(page.get_by_text("Current B reply", exact=True)).to_be_visible()
    sends = [body for path, body in state["calls"] if path == "operator_chat/send"]
    assert [body["session_id"] for body in sends] == ["session-b", "session-b"]
    assert sends[1]["text"] == restored


def test_only_validated_writable_adoption_cancels_pending_send(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    checks = []

    def validate(route):
        if route.request.post_data_json.get("session_id") == "session-a":
            checks.append(route)
        else:
            route.fallback()

    page.route("**/api/v1/operator_chat/sessions", validate)
    page.goto(f"{base}/#chat?conversation=session-b")
    page.get_by_role("textbox").fill("Synthetic old owner submission")
    with page.expect_request(lambda r: r.url.endswith("/operator_chat/send")):
        page.get_by_role("textbox").press("Enter")
    pending = next(r for r in state["held"] if r.request.url.endswith("/send"))
    open_chat_link(page, "unknown-web-id")
    expect(page.get_by_role("alert")).to_contain_text("not found")
    with page.expect_request(lambda r: r.url.endswith("/operator_chat/sessions") and
                             r.post_data_json.get("session_id") == "session-a"):
        open_chat_link(page, "session-a")
    expect(page.get_by_role("textbox")).to_have_count(0)
    assert page.evaluate("localStorage.getItem('looper-chat-session-id')") == "session-b"
    assert not any(url.endswith("/operator_chat/send") for url in state["cancelled"])
    checks[0].fulfill(json={"status": "ok", "result": {"sessions": [session("session-a")]}})
    expect(page.get_by_role("textbox")).to_be_visible()
    page.get_by_role("textbox").fill("New owner's draft")
    pending.fulfill(json={"status": "ok", "result": {"correlation_id": "reply-a"}})
    page.clock.run_for(3100)
    assert any(url.endswith("/operator_chat/send") for url in state["cancelled"])
    expect(page.get_by_role("textbox")).to_have_value("New owner's draft")
    expect(page.get_by_text("Synthetic old owner submission", exact=True)).to_have_count(0)
    assert not any(path == "operator_chat/reply_events" for path, _ in state["calls"])


@pytest.mark.parametrize("finished_while_away", [False, True])
def test_return_from_readonly_resyncs_turn_and_recovers_queue(dashboard, finished_while_away):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["hold_reply"] = True
    page.goto(f"{base}/#chat?conversation=session-b")
    page.get_by_role("textbox").fill("Synthetic original turn")
    page.get_by_role("textbox").press("Enter")
    expect(page.get_by_text("Synthetic original turn", exact=True)).to_be_visible()
    pending = next(r for r in state["held"] if r.request.url.endswith("/send"))
    with page.expect_request(lambda r: r.url.endswith("/operator_chat/reply_events")):
        pending.fulfill(json={"status": "ok", "result": {"correlation_id": "reply-a"}})
    original = next(body for path, body in state["calls"] if path == "operator_chat/send")
    state["turn"] = {"message_id": "envelope-a", "client_message_id": original["message_id"],
                     "channel": "web", "started_at": "2026-01-01T00:00:00Z", "state": "running",
                     "method": None, "stopping": False, "can_interrupt": True}
    page.get_by_role("textbox").fill("Synthetic queued followup")
    page.get_by_role("textbox").press("Enter")
    expect(page.get_by_text("Queued · sent when the reply ends", exact=True)).to_be_visible()
    page.locator('aside button[title="lark:known"]').click()
    expect(page.locator('[data-viewing="lark:known"]')).to_be_visible()
    expect(page.get_by_role("textbox")).to_have_count(0)
    assert page.evaluate("localStorage.getItem('looper-chat-session-id')") == "session-b"
    # A stream that actually ends without a final answer still needs the existing turn/queue recovery.
    reply = next(r for r in state["held"] if r.request.url.endswith("/reply_events"))
    reply.fulfill(content_type="text/event-stream", body="")
    if finished_while_away:
        state["turn"] = None
    page.locator('aside button[title="session-b"]').click()
    expect(page.get_by_role("textbox")).to_be_visible()
    if not finished_while_away:
        expect(page.get_by_role("button", name="Queue message", exact=True)).to_be_visible()
        state["turn"] = None
        page.clock.run_for(3100)
    # An ended stream has no final outcome: the existing recovery contract returns the queue to the draft.
    expect(page.get_by_role("textbox")).to_have_value("Synthetic queued followup")
    expect(page.get_by_role("button", name="Remove queued message")).to_have_count(0)
    assert page.evaluate("sessionStorage.getItem('looper-chat-staged-session-b')") is None
    assert sum(path == "operator_chat/send" for path, _ in state["calls"]) == 1
    assert not any(url.endswith("/operator_chat/reply_events") for url in state["cancelled"])
    state["hold"] = state["hold_reply"] = False
    page.get_by_role("textbox").press("Enter")
    expect(page.get_by_text("Current B reply", exact=True)).to_be_visible()
    sends = [body for path, body in state["calls"] if path == "operator_chat/send"]
    assert [body["session_id"] for body in sends] == ["session-b", "session-b"]
    shot = ROOT / "output/playwright/chat-readonly-queue-recovery.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot))


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


@pytest.mark.parametrize("source", ["generated", "stored", "new"])
def test_empty_thread_survives_back_and_reload_and_can_send(dashboard, source):
    from playwright.sync_api import expect

    page, base, state = dashboard
    if source == "generated":
        page.add_init_script("localStorage.setItem('looper-chat-session-id', '')")
    elif source == "stored":
        state["rows"][:] = [row for row in state["rows"] if row["session_id"] != "session-b"]
    with page.expect_response(lambda r: r.url.endswith("/operator_chat/sessions") and
                              bool(r.request.post_data_json.get("session_id"))) as initial:
        page.goto(f"{base}/#chat")
    expect(page.get_by_role("textbox")).to_be_visible()
    confirmed = initial.value
    if source == "new":
        with page.expect_response(lambda r: r.url.endswith("/operator_chat/sessions") and
                                  r.request.post_data_json.get("session_id", "session-b") != "session-b") as fresh:
            page.get_by_title("New conversation:", exact=False).click()
        confirmed = fresh.value
    empty = page.evaluate("localStorage.getItem('looper-chat-session-id')")
    expect(page).to_have_url(f"{base}/#chat?conversation={empty}")
    assert confirmed.json()["result"]["sessions"] == []
    for reload in (False, True):
        page.locator('aside button[title="session-a"]').click()
        expect(page.locator('aside button[title="session-a"]')).to_have_attribute("aria-current", "true")
        if reload:
            page.reload()
            expect(page.get_by_role("textbox")).to_be_visible()
        page.go_back()
        expect(page).to_have_url(f"{base}/#chat?conversation={empty}")
        expect(page.get_by_role("textbox")).to_be_visible()
        assert page.evaluate("localStorage.getItem('looper-chat-session-id')") == empty
    state["hold"] = False
    page.get_by_role("textbox").fill("Synthetic restored empty thread")
    page.get_by_role("textbox").press("Enter")
    expect(page.get_by_text("Current B reply", exact=True)).to_be_visible()
    assert [body["session_id"] for path, body in state["calls"] if path == "operator_chat/send"] == [empty]


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
