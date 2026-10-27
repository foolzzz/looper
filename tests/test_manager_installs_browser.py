"""Manager-owned installs through Chromium, with a disposable real-server readback."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from tests.db_support import DSN
from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401
from tests.test_manager_chat_bindings_browser import _manager

pytestmark = pytest.mark.installed
ROOT = Path(__file__).resolve().parents[1]


def _item(key, name, *, kind="skill", origin="ui"):
    return {"item_id": key, "kind": kind, "name": name, "qualified_name": name,
            "origin": origin, "owner_project_id": "project-example" if origin == "project" else None,
            "owner_project_name": None, "description": "Harmless example capability.", "source": "",
            "version": "", "sha256": "synthetic", "size_bytes": 32, "file_count": 1, "modified": False,
            "imported_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "mcp": None, "installs": []}


def _install(key="greeting", name="example-greeting", *, inherited=False, enabled=True):
    return {"item_id": key, "kind": "skill", "name": name, "enabled": enabled,
            "inherited": inherited, "updated_at": None if inherited else "2026-01-01T00:00:00Z"}


@pytest.fixture
def installs_dashboard(dashboard_url):
    from playwright.sync_api import Error, sync_playwright

    state = {"managers": [_manager("manager", "Manager"), _manager("manager-alpha", "Alpha", enabled=False),
                          _manager("manager-beta", "Beta")],
             "own": {}, "inherited": [], "calls": [], "held": [],
             "items": [_item("greeting", "example-greeting"), _item("plugin", "example-plugin", kind="plugin"),
                       _item("project", "project-only", origin="project"), _item("mcp", "example-mcp", kind="mcp")]}

    def snapshot(owner):
        return deepcopy({"manager_id": owner, "installs": state["inherited"] + state["own"].get(owner, [])})

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": 1280, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(5000)

        def api(route):
            path = route.request.url.split("/api/v1/")[-1]
            body = route.request.post_data_json or {}
            state["calls"].append((path, body))
            if path == "managers/list":
                result = {"managers": state["managers"], "max_enabled": 8}
            elif path.startswith("manager_installs/"):
                owner = body["manager_id"]
                action = path.rsplit("/", 1)[-1]
                if state.get("hold") == (owner, action):
                    state["held"].append((route, snapshot(owner)))
                    return
                if state.get("fail") == (owner, action):
                    route.fulfill(status=404, json={"status": "error", "error": {
                        "code": "not_found", "message": "Manager no longer exists"}})
                    return
                if action == "put":
                    item = next(item for item in state["items"] if item["item_id"] == body["item_id"])
                    rows = state["own"].setdefault(owner, [])
                    rows[:] = [row for row in rows if row["item_id"] != body["item_id"]]
                    rows.append({**_install(item["item_id"], item["name"], enabled=body.get("enabled", True)),
                                 "kind": item["kind"]})
                elif action == "delete":
                    state["own"][owner] = [row for row in state["own"].get(owner, [])
                                            if row["item_id"] != body["item_id"]]
                result = snapshot(owner)
            elif path == "catalog_items/list":
                result = {"items": state["items"], "installed_count": 0, "total_bytes": 128,
                          "last_import": None, "last_mcp_import": None, "roles": ["manager", "worker", "reviewer"],
                          "task_roles": ["worker", "reviewer"], "caps": {"file_max_bytes": 100000,
                          "item_max_bytes": 100000, "item_max_files": 100, "library_max_bytes": 100000,
                          "upload_max_bytes": 100000}, "mcp_caps": {"servers_max": 10, "variables_max": 10,
                          "value_max": 1000, "args_max": 100, "mask": "***"}}
            elif path == "manager_chat_bindings/list":
                result = {"bindings": []}
            elif path == "registry/view":
                result = {"profiles": [], "providers": [], "runtimes": []}
            elif path == "agent_settings/get":
                result = {"roles": {"manager": {"effective": {"profile": {
                    "value": {"profile_id": "agent-claude-code-opus"}}}}}}
            elif path == "project_admin/list":
                result = {"projects": []}
            elif path == "system/overview":
                result = {"tasks": {}, "agents": [], "channels": []}
            elif path == "operator_chat/targets":
                result = {"targets": []}
            elif path.endswith("thread_events"):
                state["held"].append((route, None))
                return
            else:
                route.fulfill(status=503, json={"status": "error", "error": {
                    "code": "unavailable", "message": "Outside Manager installs fixture"}})
                return
            route.fulfill(json={"status": "ok", "result": result})

        page.route("**/api/**", api)
        try:
            yield page, dashboard_url, state
        finally:
            for route, _ in state["held"]:
                try:
                    route.abort()
                except Error:
                    pass
            context.close()
            browser.close()


def _section(page, name):
    return page.get_by_role("region", name=f"Plugins and skills for {name}", exact=True)


def _open(page, name):
    section = _section(page, name)
    button = section.get_by_role("button", name="Plugins and skills", exact=True)
    if button.get_attribute("aria-expanded") != "true":
        button.click()
    return section


def _add(page, name, item="greeting"):
    section = _open(page, name)
    section.get_by_label(f"Add plugin or skill for {name}", exact=True).select_option(item)
    section.get_by_role("button", name="Add install", exact=True).click()
    return section


def test_install_for_disabled_manager_survives_reload_without_changing_other_managers(installs_dashboard):
    from playwright.sync_api import expect

    page, base, state = installs_dashboard
    page.goto(f"{base}/#settings/managers")
    alpha = _add(page, "Alpha")
    expect(alpha.get_by_role("listitem").filter(has_text="Added here")).to_contain_text("example-greeting")
    expect(alpha).to_contain_text("next turn")
    page.reload()
    expect(_open(page, "Alpha").get_by_role("switch", name="Enable example-greeting for Alpha")).to_be_checked()
    expect(_open(page, "Beta").get_by_role("listitem")).to_have_count(0)
    assert state["own"]["manager-alpha"][0]["enabled"] is True
    assert state["own"].get("manager-beta", []) == []


def test_disable_and_remove_only_change_own_row_while_global_inheritance_remains(installs_dashboard):
    from playwright.sync_api import expect

    page, base, state = installs_dashboard
    state["inherited"] = [_install(inherited=True)]
    state["own"]["manager-alpha"] = [_install()]
    page.goto(f"{base}/#settings/managers")
    alpha = _open(page, "Alpha")
    inherited = alpha.get_by_role("listitem").filter(has_text="Inherited globally")
    expect(inherited.get_by_role("switch")).to_have_count(0)
    expect(inherited.get_by_role("button", name="Remove", exact=False)).to_have_count(0)
    expect(alpha.get_by_role("link", name="Settings → Skills & MCP", exact=True)).to_have_attribute("href", "#settings/catalog")
    alpha.get_by_role("switch", name="Enable example-greeting for Alpha").click()
    expect(alpha.get_by_role("switch", name="Enable example-greeting for Alpha")).not_to_be_checked()
    expect(inherited).to_contain_text("example-greeting")
    alpha.get_by_role("button", name="Remove example-greeting from Alpha", exact=True).click()
    expect(alpha.get_by_role("listitem")).to_have_count(1)
    expect(inherited).to_contain_text("example-greeting")
    expect(alpha.get_by_label("Add plugin or skill for Alpha", exact=True)).to_be_focused()
    page.reload()
    expect(_open(page, "Alpha").get_by_role("listitem")).to_have_count(1)
    assert state["own"]["manager-alpha"] == []


def test_inherited_item_can_be_added_explicitly_for_same_manager(installs_dashboard):
    from playwright.sync_api import expect

    page, base, state = installs_dashboard
    state["inherited"] = [_install(inherited=True)]
    page.goto(f"{base}/#settings/managers")
    alpha = _open(page, "Alpha")
    expect(alpha.get_by_role("listitem")).to_have_count(1)
    expect(alpha.get_by_role("listitem")).to_contain_text("Inherited globally")
    _add(page, "Alpha")
    expect(alpha.get_by_role("listitem")).to_have_count(2)
    expect(alpha.get_by_role("listitem").filter(has_text="Added here")).to_contain_text("example-greeting")
    expect(alpha.get_by_role("listitem").filter(has_text="Inherited globally")).to_contain_text("example-greeting")


@pytest.mark.parametrize("action", ["list", "put"])
def test_late_alpha_response_cannot_change_beta_rows(installs_dashboard, action):
    from playwright.sync_api import expect

    page, base, state = installs_dashboard
    if action == "list":
        state["own"]["manager-alpha"] = [_install()]
    state["hold"] = ("manager-alpha", action)
    page.goto(f"{base}/#settings/managers")
    if action == "put":
        _add(page, "Alpha")
    else:
        _open(page, "Alpha")
    beta = _open(page, "Beta")
    _add(page, "Beta", "plugin")
    expect(beta).to_contain_text("example-plugin")
    assert state["held"]
    route, result = next((route, result) for route, result in state["held"]
                         if route.request.url.endswith(f"/manager_installs/{action}"))
    state["hold"] = None
    route.fulfill(json={"status": "ok", "result": result})
    expect(beta.get_by_role("listitem")).to_have_count(1)
    expect(beta.get_by_role("listitem")).not_to_contain_text("example-greeting")


def test_missing_target_failure_stays_visible_and_keeps_selected_draft(installs_dashboard):
    from playwright.sync_api import expect

    page, base, state = installs_dashboard
    state["fail"] = ("manager-alpha", "put")
    page.goto(f"{base}/#settings/managers")
    alpha = _add(page, "Alpha")
    expect(alpha.get_by_role("alert")).to_contain_text("Manager no longer exists")
    expect(alpha.get_by_label("Add plugin or skill for Alpha", exact=True)).to_have_value("greeting")
    expect(alpha.get_by_role("listitem")).to_have_count(0)
    expect(_open(page, "Beta").get_by_role("listitem")).to_have_count(0)
    assert state["own"] == {}


def test_picker_excludes_project_skills_and_mcp(installs_dashboard):
    from playwright.sync_api import expect

    page, base, _ = installs_dashboard
    page.goto(f"{base}/#settings/managers")
    picker = _open(page, "Alpha").get_by_label("Add plugin or skill for Alpha", exact=True)
    expect(picker.locator("option[value='greeting']")).to_have_count(1)
    expect(picker.locator("option[value='plugin']")).to_have_count(1)
    expect(picker.locator("option[value='project'], option[value='mcp']")).to_have_count(0)


@pytest.mark.parametrize("width", [1280, 390])
def test_add_controls_support_keyboard_focus_and_fit_narrow_layout(installs_dashboard, width):
    from playwright.sync_api import expect

    page, base, _ = installs_dashboard
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{base}/#settings/managers")
    alpha = _section(page, "Alpha")
    disclosure = alpha.get_by_role("button", name="Plugins and skills", exact=True)
    disclosure.focus()
    page.keyboard.press("Enter")
    picker = alpha.get_by_label("Add plugin or skill for Alpha", exact=True)
    expect(picker).to_be_enabled()
    picker.focus()
    expect(picker).to_be_focused()
    picker.select_option("greeting")
    page.keyboard.press("Tab")
    expect(alpha.get_by_role("button", name="Add install", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(alpha.get_by_role("listitem")).to_contain_text("Added here")
    expect(picker).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width == 1280:
        directory = ROOT / ".local" / "validation"
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / "manager-installs.png"), full_page=True)


def test_default_only_baseline_hides_section_and_keeps_global_catalog_entry(installs_dashboard):
    from playwright.sync_api import expect

    page, base, state = installs_dashboard
    state["managers"] = [_manager("manager", "Manager")]
    with page.expect_response(lambda response: response.url.endswith("/manager_installs/list")
                              and response.request.post_data_json == {"manager_id": "manager"}) as listed:
        page.goto(f"{base}/#settings/managers")
    assert listed.value.json()["result"] == {"manager_id": "manager", "installs": []}
    listed.value.finished()
    expect(page.get_by_role("button", name="Edit Manager Manager", exact=True)).to_be_visible()
    expect(_section(page, "Manager")).to_have_count(0)
    page.get_by_role("button", name="Skills & MCP", exact=True).click()
    expect(page).to_have_url(f"{base}/#settings/catalog")
    expect(page.get_by_text("example-greeting", exact=True)).to_be_visible()


def test_default_only_manager_with_cli_created_extra_can_manage_it(installs_dashboard):
    from playwright.sync_api import expect

    page, base, state = installs_dashboard
    state["managers"] = [_manager("manager", "Manager")]
    state["own"]["manager"] = [_install()]
    page.goto(f"{base}/#settings/managers")
    expect(_open(page, "Manager").get_by_role("listitem")).to_contain_text("example-greeting")


def test_removing_last_default_only_extra_hides_section_and_restores_row_focus(installs_dashboard):
    from playwright.sync_api import expect

    page, base, state = installs_dashboard
    state["managers"] = [_manager("manager", "Manager")]
    state["own"]["manager"] = [_install()]
    page.goto(f"{base}/#settings/managers")
    section = _open(page, "Manager")
    expect(section.get_by_role("listitem")).to_contain_text("example-greeting")
    section.get_by_role("button", name="Remove example-greeting from Manager", exact=True).click()
    expect(section).to_have_count(0)
    expect(page.get_by_role("button", name="Edit Manager Manager", exact=True)).to_be_focused()
    assert state["own"]["manager"] == []


def test_list_error_is_visible_and_retry_recovers_empty_server_state(installs_dashboard):
    from playwright.sync_api import expect

    page, base, state = installs_dashboard
    state["fail"] = ("manager-alpha", "list")
    page.goto(f"{base}/#settings/managers")
    alpha = _open(page, "Alpha")
    expect(alpha.get_by_role("alert")).to_contain_text("Manager no longer exists")
    state["fail"] = None
    alpha.get_by_role("button", name="Retry installs", exact=True).click()
    expect(alpha.get_by_role("alert")).to_have_count(0)
    expect(alpha.get_by_role("listitem")).to_have_count(0)
    expect(alpha.get_by_label("Add plugin or skill for Alpha", exact=True)).to_be_enabled()


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_real_serve_browser_install_is_read_back_on_next_supported_runtime_turn(tmp_path, dashboard_url):
    from playwright.sync_api import expect, sync_playwright

    from evals.prompt_regression.dialogs import _ask
    from evals.prompt_regression.runner import fresh_database, running_server
    from tests.fake_claude import fake_claude
    from tests.test_manager_reply import _resident

    async def seed(database_url):
        import asyncpg
        from looper.db import connection

        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            await connection.ensure_schema(pool)
            assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM channel_configs WHERE enabled)")
        finally:
            await pool.close()

    directories = [tmp_path / name for name in ("bin", "runtime", "home", "notes")]
    for directory in directories:
        directory.mkdir()
    bin_dir, runtime, home, notes = directories
    fake_claude(bin_dir / "claude", "import json, os, sys, time\n"
                "if '--version' in sys.argv:\n    print('2.1.100 (Claude Code)')\n    sys.exit(0)\n"
                + _resident())
    with fresh_database(str(DSN)) as (database_url, _):
        asyncio.run(seed(database_url))
        with running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                            notes_root=notes, log_path=tmp_path / "serve.log", startup_timeout=60) as api:
            for key, name in (("alpha", "Alpha"), ("beta", "Beta")):
                api.post("/api/v1/managers/create", {"key": key, "name": name, "system_prompt": "Help with examples."})
            item = api.post("/api/v1/catalog_sources/create_skill", {"name": "example-greeting",
                            "description": "A harmless example.", "content": "Return EXAMPLE-GREETING."})["result"]["item"]
            with sync_playwright() as driver:
                browser = driver.chromium.launch(headless=True, timeout=15000)
                context = browser.new_context(viewport={"width": 1280, "height": 1000})
                try:
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.goto(f"{api.base_url}/#settings/managers")
                    alpha = _add(page, "Alpha", item["item_id"])
                    expect(alpha.get_by_role("listitem")).to_contain_text("example-greeting")
                    page.reload()
                    expect(_open(page, "Alpha").get_by_role("listitem")).to_contain_text("example-greeting")
                    shared_contents = {}
                    for key in ("alpha", "beta"):
                        rows = api.post("/api/v1/manager_installs/list", {"manager_id": f"manager-{key}"})["result"]["installs"]
                        assert len(rows) == int(key == "alpha")
                        assert "Synthetic next turn" in _ask(api, f"install-{key}", "Synthetic next turn",
                                                            timeout=30, manager=key)
                        records = api.post("/api/v1/runs/list", {"agent_id": f"manager-{key}", "limit": 1})["result"]["records"]
                        installed = (records[0]["metadata"].get("library") or {}).get("items", [])
                        assert [(row["name"], row["scope"]) for row in installed if row["scope"] == "manager"] == (
                            [("example-greeting", "manager")] if key == "alpha" else [])
                        if key == "beta":
                            assert not any(row["name"] == "example-greeting" for row in installed)
                        # The built-in MCP hash includes each Manager's own tool binding.
                        shared_contents[key] = [(row["kind"], row["name"], row["scope"])
                                                for row in installed if row["scope"] != "manager"]
                    assert shared_contents["alpha"] == shared_contents["beta"]
                    _open(page, "Alpha").get_by_role("button", name="Remove example-greeting from Alpha", exact=True).click()
                    expect(_section(page, "Alpha").get_by_role("listitem")).to_have_count(0)
                    assert api.post("/api/v1/manager_installs/list", {"manager_id": "manager-alpha"})["result"]["installs"] == []
                finally:
                    context.close()
                    browser.close()
