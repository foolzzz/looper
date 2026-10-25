"""Built Channels controls against synthetic v1 responses, without transport accounts."""
from pathlib import Path

import pytest

from test_chat_navigation_browser import dashboard_url  # noqa: F401 - shared build/server fixture

pytestmark = pytest.mark.installed
STAMP = "2026-01-01T00:00:00Z"


def bot(channel_id, name, owner="manager", builtin=True):
    return {"channel_id": channel_id, "channel_type": "telegram", "display_name": name,
            "enabled": False, "has_bot_token": False, "builtin": builtin,
            "manager_id": owner, "manager_name": "Manager" if owner == "manager" else "Review",
            "created_at": STAMP, "updated_at": STAMP,
            "config": {"allowed_user_ids": ["42"]}}


@pytest.fixture
def channels_dashboard(dashboard_url):  # noqa: F811 - pytest injects the shared fixture
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_default_timeout(5000)
        state = {"calls": [], "held": [], "bindings": {}, "policies": {}, "failure": None,
                 "hold": None, "catalog_error": False, "empty": False, "paged": False,
                 "managers": [{"id": "manager", "name": "Manager", "enabled": True},
                              {"id": "manager-review", "name": "Review", "enabled": True},
                              {"id": "manager-off", "name": "Paused", "enabled": False}]}
        configs = [bot("channel-telegram", "Telegram"),
                   bot("channel-telegram-2", "Review bot", "manager-review", False)]
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def catalog(channel_id, after=None):
            owner = next(c["manager_id"] for c in configs if c["channel_id"] == channel_id)

            def chat(chat_id, name, count=3, last_at=STAMP):
                binding = state["bindings"].get((channel_id, chat_id))
                manager_id = binding["manager_id"] if binding else owner
                manager = next((m for m in state["managers"] if m["id"] == manager_id),
                               {"name": "Unavailable", "enabled": False})
                return {"chat_id": chat_id, "name": name, "count": count, "last_at": last_at,
                        "binding": binding, "effective_manager_id": manager_id,
                        "effective_manager_name": manager["name"],
                        "effective_manager_enabled": manager["enabled"], "effective_manager_deleted": False}

            chats = [] if state["empty"] else [chat("42", "Synthetic room")]
            suppressed = []
            if state["paged"]:
                chats = ([chat("43", None, None, None)] if after else chats)
                suppressed = [{"chat_id": "42", "name": "Synthetic room", "count": 2, "last_at": STAMP},
                              {"chat_id": "44", "name": "Suppressed room", "count": 4, "last_at": STAMP}]
            return {"channel_id": channel_id, "bot_manager_id": owner,
                    "bound_chats_only": state["policies"].get(channel_id, False), "chats": chats,
                    "unbound_chats": suppressed, "has_more": bool(state["paged"] and not after),
                    "next_cursor": "42" if state["paged"] and not after else None}

        def api(route):
            path = route.request.url.split("/api/v1/")[-1]
            body = route.request.post_data_json or {}
            state["calls"].append((path, body))
            if path.startswith(("channel_chats/", "manager_chat_bindings/")):
                assert route.request.method == "POST"
                assert body["channel_id"] in {c["channel_id"] for c in configs}
            if path == state["hold"]:
                state["held"].append(route)
                return
            failure = state["failure"]
            if failure and failure[0] == path:
                state["failure"] = None
                route.fulfill(status=failure[1], json={"status": "error", "error": {
                    "code": failure[2], "message": failure[3]}})
                return
            if path == "channel_configs/list":
                result = {"configs": configs}
            elif path == "managers/list":
                result = {"managers": state["managers"], "max_enabled": 8}
            elif path == "channel_chats/list":
                if state["catalog_error"]:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Synthetic catalog unavailable"}})
                    return
                result = catalog(body["channel_id"], body.get("after"))
            elif path == "manager_chat_bindings/list":
                result = {"bindings": [binding for (cid, _), binding in state["bindings"].items()
                                       if cid == body["channel_id"]]}
            elif path == "manager_chat_bindings/put":
                manager = next(m for m in state["managers"] if m["id"] == body["manager_id"])
                binding = {**body, "created_at": STAMP, "channel_type": "telegram",
                           "bot_name": "Synthetic bot", "chat_name": "Synthetic room",
                           "manager_name": manager["name"], "manager_enabled": manager["enabled"],
                           "manager_deleted": False}
                state["bindings"][(body["channel_id"], body["chat_id"])] = binding
                result = {"binding": binding}
            elif path == "manager_chat_bindings/delete":
                state["bindings"].pop((body["channel_id"], body["chat_id"]), None)
                result = {**body, "deleted": True}
            elif path == "channel_chats/set_policy":
                state["policies"][body["channel_id"]] = body["bound_chats_only"]
                result = body
            else:
                result = {"gate_settings/get": {"targets": [], "dashboard_target": None},
                          "project_admin/list": {"projects": []}, "system/overview": {},
                          "system/status": {}, "ops/gates/list": {"gates": [], "total": 0},
                          "chat_settings/get": {"stop_enabled": False}}.get(path)
                if result is None:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside synthetic Channels fixture"}})
                    return
            route.fulfill(json={"status": "ok", "result": result})

        context.route("**/api/**", api)
        state["catalog"] = catalog
        try:
            yield page, dashboard_url, state
            assert not errors
        finally:
            try:
                for route in state["held"]:
                    try:
                        route.abort()
                    except Error:
                        pass  # Navigation may have already cancelled it.
            finally:
                try:
                    context.close()
                finally:
                    browser.close()


def card(page, name="Telegram"):
    return page.get_by_role("heading", name=name, exact=True).locator(
        "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' rounded-lg ')][1]")


def bindings(page, name="Telegram"):
    return card(page, name).get_by_role("region", name="Chat bindings", exact=True)


def open_channels(dashboard):
    page, base, state = dashboard
    page.goto(f"{base}/#settings/channels")
    return page, state


def writes(state, suffix):
    return [body for path, body in state["calls"] if path == suffix]


def test_immediate_binding_readback_is_scoped_to_bot(channels_dashboard):
    from playwright.sync_api import expect

    page, state = open_channels(channels_dashboard)
    target = bindings(page).get_by_label("Bind Synthetic room to", exact=True)
    other = bindings(page, "Review bot").get_by_label("Bind Synthetic room to", exact=True)
    target.select_option("manager-review")
    expect(target).to_have_value("manager-review")
    expect(other).to_have_value("")
    assert writes(state, "manager_chat_bindings/put") == [{
        "channel_id": "channel-telegram", "chat_id": "42", "manager_id": "manager-review"}]
    assert sum(body["channel_id"] == "channel-telegram" for body in writes(state, "channel_chats/list")) >= 2
    page.reload()
    expect(bindings(page).get_by_label("Bind Synthetic room to", exact=True)).to_have_value("manager-review")
    expect(bindings(page, "Review bot").get_by_label("Bind Synthetic room to", exact=True)).to_have_value("")


def test_owner_binding_and_unbound_default_are_distinct(channels_dashboard):
    from playwright.sync_api import expect

    channels_dashboard[2]["policies"]["channel-telegram"] = True
    page, state = open_channels(channels_dashboard)
    section = bindings(page)
    target = section.get_by_label("Bind Synthetic room to", exact=True)
    target.select_option("manager")
    expect(target).to_have_value("manager")
    assert writes(state, "manager_chat_bindings/put")[-1]["manager_id"] == "manager"
    target.select_option("")
    expect(target).to_have_value("")
    assert writes(state, "manager_chat_bindings/delete") == [{"channel_id": "channel-telegram", "chat_id": "42"}]
    expect(section.get_by_role("switch", name="Only bound chats", exact=True)).to_have_attribute("aria-checked", "true")
    expect(section).to_contain_text("silence")


def test_policy_saves_true_and_false_without_saving_credentials(channels_dashboard):
    from playwright.sync_api import expect

    page, state = open_channels(channels_dashboard)
    draft = card(page).get_by_label("Bot token", exact=True)
    draft.fill("synthetic-unsaved-token")
    save = card(page).get_by_role("button", name="Save", exact=True)
    expect(save).to_be_enabled()
    switch = bindings(page).get_by_role("switch", name="Only bound chats", exact=True)
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "true")
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "false")
    assert writes(state, "channel_chats/set_policy") == [
        {"channel_id": "channel-telegram", "bound_chats_only": True},
        {"channel_id": "channel-telegram", "bound_chats_only": False}]
    assert not writes(state, "channel_configs/update")
    expect(draft).to_have_value("synthetic-unsaved-token")
    expect(save).to_be_enabled()


def test_typed_binding_failure_keeps_confirmed_value_and_retries(channels_dashboard):
    from playwright.sync_api import expect

    page, state = open_channels(channels_dashboard)
    section = bindings(page)
    target = section.get_by_label("Bind Synthetic room to", exact=True)
    expect(target.locator('option[value="manager-review"]')).to_have_count(1)
    state["failure"] = ("manager_chat_bindings/put", 404, "not_found", "Synthetic Manager was deleted")
    before = len(writes(state, "managers/list"))
    state["managers"] = [manager for manager in state["managers"] if manager["id"] != "manager-review"]
    target.select_option("manager-review")
    expect(section.get_by_role("alert")).to_contain_text("not_found")
    expect(section.get_by_role("alert")).to_contain_text("Synthetic Manager was deleted")
    expect(target).to_have_value("")
    expect(target).to_be_enabled()
    assert len(writes(state, "managers/list")) > before
    expect(target.locator('option[value="manager-review"]')).to_have_count(0)
    target.select_option("manager")
    expect(target).to_have_value("manager")
    expect(section.get_by_role("alert")).to_have_count(0)


def test_pending_binding_disables_only_its_row(channels_dashboard):
    from playwright.sync_api import expect

    page, state = open_channels(channels_dashboard)
    section = bindings(page)
    target = section.get_by_label("Bind Synthetic room to", exact=True)
    state["hold"] = "manager_chat_bindings/put"
    target.select_option("manager-review")
    expect(target).to_be_disabled()
    expect(section.get_by_role("switch", name="Only bound chats", exact=True)).to_be_enabled()
    expect(bindings(page, "Review bot").get_by_label("Bind Synthetic room to", exact=True)).to_be_enabled()
    pending = state["held"].pop()
    state["hold"] = None
    pending.fulfill(status=503, json={"status": "error", "error": {
        "code": "unavailable", "message": "Synthetic retry required"}})
    expect(target).to_be_enabled()
    expect(target).to_have_value("")


def test_paginated_catalog_unions_suppressed_chats_without_duplicate_selectors(channels_dashboard):
    from playwright.sync_api import expect

    channels_dashboard[2]["paged"] = True
    page, state = open_channels(channels_dashboard)
    section = bindings(page)
    expect(section.get_by_label("Bind Synthetic room to", exact=True)).to_have_count(1)
    expect(section.get_by_label("Bind Suppressed room to", exact=True)).to_be_visible()
    section.get_by_role("button", name="Load more chats", exact=True).click()
    expect(section.get_by_label("Bind 43 to", exact=True)).to_be_visible()
    expect(section).to_contain_text("Unknown")
    expect(section.get_by_label("Bind Synthetic room to", exact=True)).to_have_count(1)
    assert any(body.get("after") == "42" for body in writes(state, "channel_chats/list"))


@pytest.mark.parametrize("width", [1280, 768], ids=["desktop", "narrow"])
def test_keyboard_controls_and_layout(channels_dashboard, width):
    from playwright.sync_api import expect

    page = channels_dashboard[0]
    page.set_viewport_size({"width": width, "height": 900})
    page, state = open_channels(channels_dashboard)
    section = bindings(page)
    target = section.get_by_label("Bind Synthetic room to", exact=True)
    target.focus()
    expect(target).to_be_focused()
    target.press("m")
    target.press("Tab")
    expect(target).to_have_value("manager")
    switch = section.get_by_role("switch", name="Only bound chats", exact=True)
    switch.focus()
    switch.press("Space")
    expect(switch).to_have_attribute("aria-checked", "true")
    switch.press("Space")
    expect(switch).to_have_attribute("aria-checked", "false")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width == 1280:
        output = Path("output/playwright/channel-chat-bindings.png")
        output.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output), full_page=True)
    assert writes(state, "manager_chat_bindings/put")[-1]["manager_id"] == "manager"


def test_empty_catalog_is_visible_and_refreshable(channels_dashboard):
    from playwright.sync_api import expect

    channels_dashboard[2]["empty"] = True
    page, state = open_channels(channels_dashboard)
    section = bindings(page)
    expect(section).to_contain_text("No chats")
    expect(section.get_by_role("alert")).to_have_count(0)
    state["empty"] = False
    section.get_by_role("button", name="Refresh chats", exact=True).click()
    expect(section.get_by_label("Bind Synthetic room to", exact=True)).to_be_visible()


def test_catalog_error_is_visible_and_retry_refreshes(channels_dashboard):
    from playwright.sync_api import expect

    channels_dashboard[2]["catalog_error"] = True
    page, state = open_channels(channels_dashboard)
    section = bindings(page)
    expect(section.get_by_role("alert")).to_contain_text("Synthetic catalog unavailable")
    expect(section.get_by_label("Bind Synthetic room to", exact=True)).to_have_count(0)
    state["catalog_error"] = False
    section.get_by_role("button", name="Refresh chats", exact=True).click()
    expect(section.get_by_label("Bind Synthetic room to", exact=True)).to_be_visible()
    expect(section.get_by_role("alert")).to_have_count(0)


def test_loading_catalog_does_not_claim_empty(channels_dashboard):
    from playwright.sync_api import expect

    channels_dashboard[2]["hold"] = "channel_chats/list"
    page, state = open_channels(channels_dashboard)
    section = bindings(page)
    expect(section).to_contain_text("Loading chats")
    expect(section.get_by_text("No chats", exact=False)).to_have_count(0)
    state["hold"] = None
    for pending in state["held"][:]:
        pending.fulfill(json={"status": "ok", "result": state["catalog"](pending.request.post_data_json["channel_id"])})
        state["held"].remove(pending)
    expect(section.get_by_label("Bind Synthetic room to", exact=True)).to_be_visible()
