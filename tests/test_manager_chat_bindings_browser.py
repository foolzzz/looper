"""Managers' explicit chat bindings through Chromium and an isolated real server."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.db_support import DSN
from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401

pytestmark = pytest.mark.installed
ROOT = Path(__file__).resolve().parents[1]


def _manager(key, name, *, enabled=True):
    return {"id": key, "name": name, "description": "", "builtin": key == "manager",
            "enabled": enabled, "tools": "all" if key == "manager" else "tasks", "task_scope": "own",
            "task_policy": "free", "system_prompt": None, "profile_id": None, "profile_name": None,
            "templates": [], "revision": 0, "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "state": {"running": False, "busy": False, "healthy": True, "sync": "ok"},
            "spend": {"today_usd": 0, "last_7d_usd": 0, "runs_today": 0}}


def _binding(channel, manager, name, bot):
    return {"channel_id": channel, "chat_id": "42", "manager_id": manager,
            "created_at": "2026-01-01T00:00:00Z", "channel_type": "telegram", "bot_name": bot,
            "chat_name": name, "manager_name": "Manager" if manager == "manager" else "Review",
            "manager_enabled": manager != "manager-review", "manager_deleted": False}


@pytest.fixture
def managers_dashboard(dashboard_url):
    from playwright.sync_api import Error, sync_playwright

    managers = [_manager("manager", "Manager"), _manager("manager-review", "Review", enabled=False),
                _manager("manager-archive", "Archive")]
    state = {"managers": managers, "bindings": [
        _binding("channel-telegram-201", "manager", "Example room", "Example bot"),
        _binding("channel-telegram-202", "manager-review", "Review room", "Review bot")], "calls": []}
    held = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_default_timeout(5000)

        def api(route):
            path = route.request.url.split("/api/v1/")[-1]
            body = route.request.post_data_json or {}
            state["calls"].append((path, body))
            if path == "managers/list":
                result = {"managers": managers, "max_enabled": 8}
            elif path == "manager_installs/list":
                result = {"manager_id": body["manager_id"], "installs": []}
            elif path == "manager_chat_bindings/list":
                if state.pop("fail_list", False):
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Synthetic read failure"}})
                    return
                result = {"bindings": state["bindings"]}
            elif path == "manager_chat_bindings/put":
                if state.pop("fail_put", False):
                    route.fulfill(status=404, json={"status": "error", "error": {
                        "code": "not_found", "message": "Manager not found"}})
                    return
                row = next(b for b in state["bindings"] if b["channel_id"] == body["channel_id"])
                target = next(m for m in managers if m["id"] == body["manager_id"])
                row.update(manager_id=target["id"], manager_name=target["name"], manager_enabled=target["enabled"])
                result = {"binding": row}
            elif path == "channel_chats/list":
                result = {"channel_id": body["channel_id"], "bot_manager_id": "manager",
                          "bound_chats_only": body["channel_id"] == "channel-telegram-202", "chats": [],
                          "unbound_chats": [], "next_cursor": None, "has_more": False}
            elif path == "manager_chat_bindings/delete":
                state["bindings"] = [b for b in state["bindings"] if b["channel_id"] != body["channel_id"]]
                result = {**body, "deleted": True}
            elif path == "registry/view":
                result = {"profiles": [], "providers": [], "runtimes": []}
            elif path == "project_admin/list":
                result = {"projects": []}
            elif path == "system/overview":
                result = {"tasks": {}, "agents": [], "channels": []}
            elif path == "operator_chat/targets":
                result = {"targets": []}
            elif path.endswith("thread_events"):
                held.append(route)
                return
            else:
                route.fulfill(status=503, json={"status": "error", "error": {
                    "code": "unavailable", "message": "Outside Managers fixture"}})
                return
            route.fulfill(json={"status": "ok", "result": result})

        page.route("**/api/**", api)
        try:
            yield page, dashboard_url, state
        finally:
            for route in held:
                try:
                    route.abort()
                except Error:
                    pass
            context.close()
            browser.close()


def _chats(page, manager):
    return page.get_by_role("region", name=f"Chats for {manager}", exact=True)


def test_default_and_disabled_managers_show_only_their_explicit_bot_chat_bindings(managers_dashboard):
    from playwright.sync_api import expect

    page, base, state = managers_dashboard
    page.goto(f"{base}/#settings/managers")
    expect(_chats(page, "Manager").get_by_role("listitem")).to_have_count(1)
    expect(_chats(page, "Manager")).to_contain_text("Example bot")
    expect(_chats(page, "Manager")).to_contain_text("Example room")
    expect(_chats(page, "Review")).to_contain_text("Review bot")
    expect(_chats(page, "Review")).not_to_contain_text("Example bot")
    expect(_chats(page, "Archive")).to_contain_text("No explicit chat bindings")
    assert [body for path, body in state["calls"] if path == "manager_chat_bindings/list"] == [{}]
    assert not any(path == "channel_chats/list" for path, _ in state["calls"])


def test_move_is_read_back_under_the_destination_and_survives_reload(managers_dashboard):
    from playwright.sync_api import expect

    page, base, state = managers_dashboard
    page.goto(f"{base}/#settings/managers")
    source = _chats(page, "Manager")
    source.get_by_label("Move Example room to Manager", exact=True).select_option("manager-review")
    source.get_by_role("button", name="Move", exact=True).click()
    expect(source.get_by_role("listitem")).to_have_count(0)
    expect(_chats(page, "Review").get_by_role("listitem")).to_have_count(2)
    paths = [path for path, _ in state["calls"]]
    assert paths[paths.index("manager_chat_bindings/put") + 1] == "manager_chat_bindings/list"
    page.reload()
    expect(_chats(page, "Review")).to_contain_text("Example room")
    expect(_chats(page, "Manager").get_by_role("listitem")).to_have_count(0)


@pytest.mark.parametrize("manager,chat,consequence", [
    ("Manager", "Example room", "default"), ("Review", "Review room", "silent")])
def test_unbind_shows_server_policy_then_readback_removes_the_explicit_row(managers_dashboard, manager, chat, consequence):
    from playwright.sync_api import expect

    page, base, state = managers_dashboard
    page.goto(f"{base}/#settings/managers")
    chats = _chats(page, manager)
    chats.get_by_role("button", name="Unbind", exact=True).click()
    expect(chats).to_contain_text(consequence, ignore_case=True)
    chats.get_by_role("button", name="Confirm unbind", exact=True).click()
    expect(chats).to_contain_text("No explicit chat bindings")
    paths = [path for path, _ in state["calls"]]
    assert paths[paths.index("manager_chat_bindings/delete") + 1] == "manager_chat_bindings/list"
    page.reload()
    expect(_chats(page, manager)).not_to_contain_text(chat)


def test_list_error_is_visible_and_retry_recovers_the_saved_bindings(managers_dashboard):
    from playwright.sync_api import expect

    page, base, state = managers_dashboard
    state["fail_list"] = True
    page.goto(f"{base}/#settings/managers")
    expect(page.get_by_role("alert")).to_contain_text("unavailable")
    page.get_by_role("button", name="Retry chats", exact=True).click()
    expect(_chats(page, "Manager")).to_contain_text("Example room")
    expect(page.get_by_role("alert")).to_have_count(0)


def test_failed_move_keeps_confirmed_ownership_and_can_be_retried(managers_dashboard):
    from playwright.sync_api import expect

    page, base, state = managers_dashboard
    state["fail_put"] = True
    page.goto(f"{base}/#settings/managers")
    source = _chats(page, "Manager")
    source.get_by_label("Move Example room to Manager", exact=True).select_option("manager-archive")
    source.get_by_role("button", name="Move", exact=True).click()
    expect(source.get_by_role("alert")).to_contain_text("not_found")
    expect(source).to_contain_text("Example room")
    expect(_chats(page, "Archive").get_by_role("listitem")).to_have_count(0)
    source.get_by_role("button", name="Move", exact=True).click()
    expect(_chats(page, "Archive")).to_contain_text("Example room")
    expect(source.get_by_role("listitem")).to_have_count(0)


@pytest.mark.parametrize("width", [1280, 768])
def test_manager_binding_controls_have_keyboard_focus_without_horizontal_overflow(managers_dashboard, width):
    from playwright.sync_api import expect

    page, base, _ = managers_dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{base}/#settings/managers")
    move = _chats(page, "Manager").get_by_label("Move Example room to Manager", exact=True)
    unbind = _chats(page, "Manager").get_by_role("button", name="Unbind", exact=True)
    unbind.focus()
    expect(unbind).to_be_focused()
    page.keyboard.press("Tab")
    expect(move).to_be_focused()
    move.select_option("manager-review")
    page.keyboard.press("Tab")
    expect(_chats(page, "Manager").get_by_role("button", name="Move", exact=True)).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width == 1280:
        directory = ROOT / ".local" / "validation"
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / "manager-chat-bindings.png"), full_page=True)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_real_serve_managers_readback_matches_typed_binding_writes(tmp_path, dashboard_url):
    from playwright.sync_api import expect, sync_playwright

    from evals.prompt_regression.runner import fresh_database, running_server
    from tests.fake_claude import fake_claude

    async def seed(database_url):
        import asyncpg

        from looper.agents.serve import seed_channel_configs
        from looper.db import chat_bindings, connection

        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            await connection.ensure_schema(pool)
            await seed_channel_configs(pool)
            await pool.execute("""
                INSERT INTO managers (id, name, system_prompt, enabled)
                VALUES ('manager-review', 'Review', 'Review examples.', false);
                INSERT INTO channel_configs (channel_id, channel_type, display_name, manager_id, enabled)
                VALUES ('channel-telegram-201', 'telegram', 'Example bot', 'manager', false);
            """)
            await chat_bindings.record_seen("channel-telegram-201", "42", "Example room", suppressed=False, pool=pool)
            await chat_bindings.record_seen("channel-telegram-201", "43", "Implicit room", suppressed=False, pool=pool)
            assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM channel_configs WHERE enabled)")
        finally:
            await pool.close()

    bin_dir, runtime, home, notes = [tmp_path / name for name in ("bin", "runtime", "home", "notes")]
    for directory in (bin_dir, runtime, home, notes):
        directory.mkdir()
    fake_claude(bin_dir / "claude", "import sys\nsys.exit(0)\n")
    with fresh_database(str(DSN)) as (database_url, _):
        asyncio.run(seed(database_url))
        with running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                            notes_root=notes, log_path=tmp_path / "serve.log", startup_timeout=60) as api:
            body = {"channel_id": "channel-telegram-201", "chat_id": "42", "manager_id": "manager"}
            assert api.post("/api/v1/manager_chat_bindings/put", body)["result"]["binding"]["manager_id"] == "manager"
            with sync_playwright() as driver:
                browser = driver.chromium.launch(headless=True, timeout=15000)
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                try:
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.goto(f"{api.base_url}/#settings/managers")
                    expect(_chats(page, "Manager")).to_contain_text("Example room")
                    expect(page.get_by_text("Implicit room", exact=True)).to_have_count(0)
                    _chats(page, "Manager").get_by_label("Move Example room to Manager", exact=True).select_option("manager-review")
                    _chats(page, "Manager").get_by_role("button", name="Move", exact=True).click()
                    expect(_chats(page, "Review")).to_contain_text("Example room")
                    assert api.post("/api/v1/manager_chat_bindings/list", {})["result"]["bindings"][0]["manager_id"] == "manager-review"
                    page.reload()
                    _chats(page, "Review").get_by_role("button", name="Unbind", exact=True).click()
                    expect(_chats(page, "Review")).to_contain_text("bot default: Manager")
                    _chats(page, "Review").get_by_role("button", name="Confirm unbind", exact=True).click()
                    expect(_chats(page, "Review")).to_contain_text("No explicit chat bindings")
                    assert api.post("/api/v1/manager_chat_bindings/list", {})["result"]["bindings"] == []
                    page.reload()
                    expect(_chats(page, "Review")).to_contain_text("No explicit chat bindings")
                    chats = api.post("/api/v1/channel_chats/list", {"channel_id": "channel-telegram-201"})["result"]["chats"]
                    assert {row["chat_id"] for row in chats} == {"42", "43"}
                    assert all(row["binding"] is None and row["effective_manager_id"] == "manager" for row in chats)
                finally:
                    context.close()
                    browser.close()
