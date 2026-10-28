"""Explicit Manager installs in full catalog readback and existing dashboard controls."""
import base64
import json
from types import SimpleNamespace
from urllib.parse import urlsplit
import pytest
from looper.agents import api_v1, catalog, catalog_sources
from looper.agents.http_server import AgentHTTPServer
from looper.db import api_clients, manager_installs
from tests.db_support import call_v1, run_db
@pytest.mark.parametrize("mixed", [False, True])
def test_full_catalog_manager_usage_readback(monkeypatch, tmp_path, mixed):
    async def check(pool):
        item = (await catalog_sources.create_skill({"name": "example-capability", "description": "Example",
                                                    "content": "Reply carefully."}))["item"]
        key = item["item_id"]
        await manager_installs.put_install("manager", key, enabled=False)
        if mixed:
            await catalog.install({"item_id": key, "scope": "global", "roles": ["worker"]})
        server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                 task_router=SimpleNamespace(runtime_root=tmp_path))
        for path, args in [("list", {}), ("get", {"item_id": key}),
                           ("update", {"item_id": key, "fields": {"description": "Changed"}})]:
            status, result = await call_v1(server, f"/api/v1/catalog_items/{path}", args)
            assert status == 200, result
            viewed = result["items"][0] if path == "list" else result["item"]
            assert viewed["manager_install_count"] == 1
            assert len(viewed["installs"]) == int(mixed)
            if path == "list":
                assert result["installed_count"] == 1
        text = "---\nname: example-capability\ndescription: Example\n---\nReply carefully.\n"
        uploaded = await catalog_sources.upload({"filename": "SKILL.md", "name": "uploaded-example", "content_base64":
                                                base64.b64encode(text.encode()).decode()})
        assert uploaded["item"]["manager_install_count"] == 0
        source = "https://example.invalid/capabilities"
        await pool.execute("UPDATE library_items SET origin='git',source=$2 WHERE id=$1", key, source + "@old")
        parsed = catalog_sources._from_skill_md(text.encode(), "SKILL.md", None)
        imported = await catalog_sources._store([parsed], catalog_sources.Origin.GIT, source + "@new", repository=source)
        assert imported["items"][0]["manager_install_count"] == 1
        client, _ = await api_clients.create("Catalog confirmation control")
        path = "/api/v1/catalog_items/delete"
        status, asked = await server._answer_v1("POST", path, api_v1.find("POST", path), {},
            json.dumps({"item_id": key}).encode(), api_v1.Principal("client", client["id"]))
        assert status.startswith("202") and asked["status"] == "needs_confirmation"
        assert f"all {1 + int(mixed)} installations (1 Manager installations)" in asked["impact"]
        assert (await manager_installs.item_use_counts())[key] == 1
    run_db(check, monkeypatch, timeout=25)
@pytest.mark.installed
@pytest.mark.parametrize("width", [1280, 390])
def test_catalog_manager_usage_delete_cancel_and_recalculation(tmp_path, width, staged_dashboard_dir):
    from playwright.sync_api import expect, sync_playwright
    item = {"item_id": "example", "kind": "skill", "name": "example-capability",
            "qualified_name": "personal:example-capability", "origin": "ui", "description": "Example",
            "size_bytes": 32, "mcp": None,
            "installs": [], "manager_install_count": 1}
    if width == 1280:
        item["installs"] = [{"install_id": "global", "scope": "global", "roles": ["worker"], "enabled": True},
                            {"install_id": "project", "scope": "project", "project_name": "Example project",
                             "roles": ["worker"], "enabled": False}]
    snapshot = {"items": [item], "installed_count": 1, "total_bytes": 32, "last_import": None,
                "last_mcp_import": None, "roles": ["worker"], "task_roles": ["worker"],
                "caps": {"file_max_bytes": 100000, "upload_max_bytes": 100000}, "mcp_caps": {"mask": "***"}}
    calls = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            page.set_default_timeout(5000)
            def route(request):
                path = urlsplit(request.request.url).path
                if not path.startswith("/api/"):
                    file = staged_dashboard_dir / (path.lstrip("/") or "index.html")
                    request.fulfill(path=str(file))
                    return
                calls.append(path)
                result = {"/api/v1/catalog_items/list": snapshot,
                          "/api/v1/catalog_items/get": {"item": item, "content": "Reply.", "files": []},
                          "/api/v1/catalog_items/update": {"item": item},
                          "/api/v1/project_admin/list": {"projects": []}}.get(path)
                if result is None:
                    request.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside catalog verification"}})
                    return
                request.fulfill(json={"status": "ok", "result": result})
            page.route("**/*", route)
            opened = page.goto("http://catalog.example/#settings/catalog")
            staged = (staged_dashboard_dir / "index.html").read_text()
            assert opened is not None and opened.text() == staged
            expect(page).to_have_title(staged.split("<title>", 1)[1].split("</title>", 1)[0])
            row = page.get_by_role("listitem").filter(has_text="personal:example-capability")
            expect(row).to_contain_text("Managers: 1")
            if width == 1280:
                expect(row).to_contain_text("Global: worker · Example project: worker (off)")
            row.get_by_role("button", name="Delete personal:example-capability", exact=True).click()
            expect(row).to_contain_text("1 Manager installation")
            page.screenshot(path=str(tmp_path / f"catalog-manager-delete-{width}.png"), full_page=True)
            row.get_by_role("button", name="Cancel", exact=True).click()
            assert "/api/v1/catalog_items/delete" not in calls
            row.get_by_role("button", name="Edit personal:example-capability", exact=True).click()
            row.get_by_label("Description", exact=False).fill("Changed example")
            row.get_by_role("button", name="Save", exact=True).click()
            expect(page.get_by_text("1 items · 1 installed", exact=False)).to_be_visible()
            expect(row).to_contain_text("Managers: 1")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / f"catalog-manager-usage-{width}.png"), full_page=True)
        finally:
            browser.close()
