"""Settings ownership readback across both pages, including an isolated real service."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.db_support import DSN
from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401
from tests.test_channel_chat_bindings_browser import bindings, channels_dashboard  # noqa: F401
from tests.test_manager_chat_bindings_browser import _chats, _manager

pytestmark = pytest.mark.installed
CHANNEL = "channel-telegram"
THREAD = "telegram:42#manager-review"


@pytest.fixture(scope="module", autouse=True)
def require_browser():
    playwright = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")
    with playwright.sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")


def _settings_roundtrip(page, base):
    from playwright.sync_api import expect

    page.goto(f"{base}/#settings/channels")
    section = bindings(page)
    section.get_by_role("switch", name="Only bound chats", exact=True).click()
    section.get_by_label("Bind Synthetic room to", exact=True).select_option("manager-review")
    expect(section.get_by_label("Bind Synthetic room to", exact=True)).to_have_value("manager-review")
    page.goto(f"{base}/#settings/managers")
    review = _chats(page, "Review")
    expect(review).to_contain_text("Synthetic room")
    review.get_by_label("Move Synthetic room to Manager", exact=True).select_option("manager")
    review.get_by_role("button", name="Move", exact=True).click()
    expect(_chats(page, "Manager")).to_contain_text("Synthetic room")
    page.goto(f"{base}/#settings/channels")
    expect(bindings(page).get_by_label("Bind Synthetic room to", exact=True)).to_have_value("manager")
    page.reload()
    expect(bindings(page).get_by_label("Bind Synthetic room to", exact=True)).to_have_value("manager")
    page.goto(f"{base}/#settings/managers")
    owner = _chats(page, "Manager")
    owner.get_by_role("button", name="Unbind", exact=True).click()
    expect(owner).to_contain_text("silent")
    owner.get_by_role("button", name="Confirm unbind", exact=True).click()
    expect(owner).to_contain_text("No explicit chat bindings")
    page.goto(f"{base}/#settings/channels")
    page.reload()
    expect(bindings(page).get_by_label("Bind Synthetic room to", exact=True)).to_have_value("")
    expect(bindings(page).get_by_role("switch", name="Only bound chats", exact=True)).to_have_attribute(
        "aria-checked", "true")
    bindings(page).get_by_label("Bind Synthetic room to", exact=True).select_option("manager")
    expect(bindings(page).get_by_label("Bind Synthetic room to", exact=True)).to_have_value("manager")


def test_both_settings_pages_share_binding_readback(channels_dashboard):  # noqa: F811 - shared fixture
    page, base, state = channels_dashboard
    state["managers"] = [_manager("manager", "Manager"), _manager("manager-review", "Review")]

    def listing(route):
        route.fulfill(json={"status": "ok", "result": {"bindings": list(state["bindings"].values())}})

    # The Channels fixture scopes its list to one bot; Managers intentionally requests the whole fleet.
    page.route("**/api/v1/manager_chat_bindings/list", listing)
    _settings_roundtrip(page, base)
    assert state["bindings"][(CHANNEL, "42")]["manager_id"] == "manager"
    assert state["policies"][CHANNEL] is True


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_real_settings_restart_and_encoded_history_keep_stored_owner(tmp_path, dashboard_url):  # noqa: F811 - shared fixture
    from playwright.sync_api import expect, sync_playwright

    from evals.prompt_regression.runner import fresh_database, running_server

    async def seed(database_url):
        import asyncpg

        from looper.agents.serve import seed_channel_configs
        from looper.db import chat, chat_bindings, connection

        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            await connection.ensure_schema(pool)
            await seed_channel_configs(pool)
            await pool.execute("""
                INSERT INTO managers (id, name, system_prompt, enabled)
                VALUES ('manager-review', 'Review', 'Review synthetic examples.', false);
                UPDATE channel_configs SET display_name = 'Telegram' WHERE channel_id = 'channel-telegram';
            """)
            await chat_bindings.record_seen(CHANNEL, "42", "Synthetic room", suppressed=False, pool=pool)
            await chat.save_channel_message(session_id=THREAD, message_id="example-message", role="user",
                content="Synthetic preserved history", manager_id="manager-review", channel="telegram", pool=pool)
            assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM channel_configs WHERE enabled)")
        finally:
            await pool.close()

    directories = {name: tmp_path / name for name in ("bin", "runtime", "home", "notes")}
    for directory in directories.values():
        directory.mkdir()
    server_args = dict(runtime_root=directories["runtime"], bin_dir=directories["bin"],
                       home=directories["home"], notes_root=directories["notes"],
                       log_path=tmp_path / "serve.log", startup_timeout=60)
    with fresh_database(str(DSN)) as (database_url, _):
        asyncio.run(seed(database_url))
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True, timeout=15000)
            try:
                with running_server(database_url=database_url, **server_args) as api:
                    created = api.post("/api/v1/managers/update", {"manager_id": "manager-review",
                        "fields": {"enabled": True}})
                    assert created["result"]["manager"]["id"] == "manager-review"
                    # Fresh contexts, no interception: both built pages exercise the real typed API.
                    context = browser.new_context(viewport={"width": 1280, "height": 900})
                    try:
                        page = context.new_page()
                        page.set_default_timeout(10000)
                        _settings_roundtrip(page, api.base_url)
                        result = api.post("/api/v1/channel_chats/list", {"channel_id": CHANNEL})["result"]
                        assert result["bound_chats_only"] is True
                        assert result["chats"][0]["binding"]["manager_id"] == "manager"
                    finally:
                        context.close()
                # The first process is reaped before reconstructing registry and browser state.
                with running_server(database_url=database_url, **server_args) as api:
                    context = browser.new_context(viewport={"width": 1280, "height": 900})
                    try:
                        page = context.new_page()
                        page.set_default_timeout(10000)
                        page.goto(f"{api.base_url}/#settings/channels")
                        expect(bindings(page).get_by_label("Bind Synthetic room to", exact=True)).to_have_value("manager")
                        expect(bindings(page).get_by_role("switch", name="Only bound chats", exact=True)).to_have_attribute(
                            "aria-checked", "true")
                        output = Path(".local/validation")
                        output.mkdir(parents=True, exist_ok=True)
                        for width in (1280, 768):
                            page.set_viewport_size({"width": width, "height": 900})
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                            bindings(page).get_by_label("Bind Synthetic room to", exact=True).scroll_into_view_if_needed()
                            page.screenshot(path=str(output / f"chat-binding-delivery-{width}.png"), full_page=True)
                        page.goto(f"{api.base_url}/#settings/managers")
                        expect(_chats(page, "Manager")).to_contain_text("Synthetic room")
                        expect(_chats(page, "Review")).to_contain_text("No explicit chat bindings")
                        for width in (1280, 768):
                            page.set_viewport_size({"width": width, "height": 900})
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                            page.screenshot(path=str(output / f"chat-binding-delivery-managers-{width}.png"), full_page=True)
                        # A literal '#' is data inside the session, not another fragment delimiter.
                        suffix = page.evaluate("""id => new URLSearchParams({target: 'manager-review',
                            conversation: id}).toString()""", THREAD)
                        assert "%23" in suffix
                        page.goto(f"{api.base_url}/#chat?{suffix}")
                        expect(page.get_by_text("Synthetic preserved history", exact=True)).to_be_visible()
                        expect(page.locator(f'[data-viewing="{THREAD}"]')).to_be_visible()
                        sessions = api.post("/api/v1/operator_chat/sessions", {"manager": "manager-review"})
                        assert THREAD in {row["session_id"] for row in sessions["result"]["sessions"]}
                        wrong = api.post("/api/v1/operator_chat/sessions", {"manager": "manager"})
                        assert THREAD not in {row["session_id"] for row in wrong["result"]["sessions"]}
                        page.evaluate("""id => { location.hash = '#chat?' + new URLSearchParams({
                            target: 'manager', conversation: id}); }""", THREAD)
                        expect(page.get_by_role("alert").filter(has_text="another Manager's conversation")).to_contain_text(
                            "belongs to Manager Review")
                        expect(page.get_by_text("Synthetic preserved history", exact=True)).to_have_count(0)
                        page.get_by_role("button", name="Open it there", exact=True).click()
                        expect(page).to_have_url(f"{api.base_url}/#chat?{suffix}")
                        expect(page.get_by_text("Synthetic preserved history", exact=True)).to_be_visible()
                    finally:
                        context.close()
            finally:
                browser.close()
