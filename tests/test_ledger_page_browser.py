"""The To-do page in the built dashboard, on synthetic v1 responses: its states at desktop, tablet and phone widths.

Set ``LEDGER_SHOTS`` to a directory to also save a screenshot of every state and width.
"""
import os
from pathlib import Path

import pytest

from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401

pytestmark = pytest.mark.installed
NOW = "2026-11-19T12:00:00Z"
WIDTHS = {"desktop": (1280, 800), "tablet": (768, 1024), "phone": (390, 844)}


def item(n, **extra):
    return {"id": f"L-{n:04d}", "number": n, "kind": "pending", "status": "open", "title": f"Item {n}", "detail": "",
            "assurance": "user", "source": None, "owner": "user", "project_id": None, "due_at": None, "overdue": False,
            "refs": [], "superseded_by": None, "added_via": "dashboard", "created_at": NOW, "updated_at": NOW,
            "closed_at": None, **extra}


NEEDS = [item(1, title="Reply to the vendor about the contract", overdue=True, due_at="2026-11-10T00:00:00Z"),
         item(2, kind="decision", assurance="relayed", title="Store raw messages in PostgreSQL"),
         item(3, title="Review the migration plan", source={"kind": "gate", "ref": "g1", "state": "open"},
              assurance="system")]
WORK = [item(4, kind="work_item", title="Add the ledger page", owner="task:task-1", assurance="system",
             source={"kind": "task", "ref": "task-1", "state": "running"})]
DECISIONS = [item(5, kind="decision", title="Use the second plan", updated_at="2099-01-01T00:00:00Z")]
EVENTS = [{"id": 1, "action": "add", "actor_kind": "relayed", "actor_id": None, "relayed_by": "lark:ou_example",
           "via": "lark", "session_label": None, "quote": "let's store them in PostgreSQL", "note": None,
           "changes": None, "created_at": NOW}]
COUNTS = {"open": 4, "done": 0, "cancelled": 0, "expired": 0, "superseded": 0}


@pytest.fixture
def open_page(dashboard_url):  # noqa: F811
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        pages = []

        def open_at(width, state, hash_="#ledger"):
            w, h = WIDTHS[width]
            context = browser.new_context(viewport={"width": w, "height": h}, has_touch=width == "phone",
                                          is_mobile=width == "phone")
            page = context.new_page()
            page.set_default_timeout(5000)

            def api(route):
                path = route.request.url.split("/api/v1/")[-1]
                body = route.request.post_data_json or {}
                if state == "error" and path.startswith("ledger/"):
                    return route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "the database is unavailable"}})
                result = {}
                if path == "ledger/status":
                    result = {"enabled": state != "off", "needs_you": 0 if state == "empty" else 3, "counts": COUNTS,
                              "operator_unset": state == "unset"}
                elif path == "ledger/list":
                    rows = ([] if state == "empty" else NEEDS if body.get("needs_you") else
                            WORK if body.get("kind") == "work_item" else DECISIONS if body.get("kind") == "decision"
                            else [])
                    result = {"items": rows, "more": 0, "counts": COUNTS}
                elif path == "ledger/get":
                    result = {"item": next(i for i in NEEDS + WORK + DECISIONS if i["id"] == body["item"]),
                              "events": EVENTS}
                elif path == "gates/list":
                    result = {"gates": [], "total": 0, "limit": 1, "offset": 0, "next_cursor": None, "has_more": False,
                              "counts": {"open": 0, "waiting_user": 0, "waiting_planner": 0, "closed": 0, "all": 0}}
                elif path == "system/overview":
                    result = {"version": "test"}
                elif path == "project_admin/list":
                    result = {"projects": []}
                else:  # nothing else is part of this page
                    return route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside synthetic fixture"}})
                route.fulfill(json={"status": "ok", "result": result})

            page.route("**/api/v1/**", api)
            page.goto(dashboard_url + "/" + hash_)
            pages.append(context)
            return page

        yield open_at
        for context in pages:
            context.close()
        browser.close()


def shot(page, name):
    directory = os.environ.get("LEDGER_SHOTS")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(directory) / f"{name}.png"), full_page=True)


def no_sideways_scroll(page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


@pytest.mark.parametrize("width", list(WIDTHS))
def test_data_state_orders_what_needs_the_user_first_and_stays_inside_the_screen(open_page, width):
    page = open_page(width, "data")
    page.get_by_role("heading", name="Needs you").wait_for()
    assert [h.inner_text().split(" (")[0] for h in page.locator("main h2").all()] == [
        "Needs you", "In progress", "Recent decisions"]
    first = page.locator("main section").first.locator("li").first.inner_text()
    assert "Reply to the vendor" in first and "Overdue" in first
    assert "Relayed, not confirmed" in page.locator("main").inner_text()
    no_sideways_scroll(page)
    shot(page, f"data-{width}")


@pytest.mark.parametrize("width", list(WIDTHS))
def test_detail_shows_who_said_it_and_the_actions_reach_touch_size_on_a_phone(open_page, width):
    page = open_page(width, "data", "#ledger?item=L-0002")
    region = page.get_by_role("region", name="Details of L-0002")
    region.wait_for()
    text = region.inner_text()
    assert "Someone else said this" in text and "Relayed · lark:ou_example" in text
    assert "let's store them in PostgreSQL" in text
    confirm = region.get_by_role("button", name="Confirm")
    confirm.wait_for()
    if width == "phone":
        assert confirm.bounding_box()["height"] >= 43
    no_sideways_scroll(page)
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement !== document.body")
    shot(page, f"detail-{width}")


@pytest.mark.parametrize("state,expected", [
    ("empty", "Nothing is waiting for you."), ("off", "The ledger is off."),
    ("error", "Could not read the ledger"), ("unset", "has no My ID set")])
@pytest.mark.parametrize("width", list(WIDTHS))
def test_empty_off_error_and_unset_states_say_what_they_are(open_page, width, state, expected):
    page = open_page(width, state)
    page.get_by_text(expected).first.wait_for()
    if state == "error":
        assert "Nothing is waiting" not in page.locator("main").inner_text()
        page.get_by_role("button", name="Retry").wait_for()
    no_sideways_scroll(page)
    shot(page, f"{state}-{width}")
