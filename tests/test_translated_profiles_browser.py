"""Translated profile and model settings readback in Chromium, using synthetic registry data."""
from urllib.parse import urlsplit

import pytest

from looper.agents import profile_registry as registry, runtimes


@pytest.mark.installed
def test_translated_profile_and_dialect_settings(tmp_path, staged_dashboard_dir):
    playwright = pytest.importorskip("playwright.sync_api", reason="UNTESTED: optional Playwright package missing")
    expect, sync_playwright = playwright.expect, playwright.sync_playwright

    runtime_type = registry.type_view(runtimes.adapter("claude-code"))
    provider = {
        "provider_id": "example", "name": "Example account", "vendor_type": "deepseek", "auth": "api_key",
        "oauth_runtime_type": None, "endpoints": [{"protocol": "openai-chat", "base_url": "https://example.invalid/v1",
                                                   "use_runtime_default": False}],
        "env": [], "login": None, "api_key": "***", "builtin": False, "enabled": True,
        "max_concurrency": None, "endpoint_count": 1, "model_count": 1, "profile_count": 1,
    }
    model = {
        "model_id": "example-model", "name": "example-chat", "protocol": "openai-chat", "provider_id": "example",
        "provider_name": "Example account", "provider_enabled": True, "context_window": None,
        "chat_dialect": None, "effective_chat_dialect": "deepseek", "efforts": None, "background_model": None,
        "builtin": False, "profile_count": 1, "warnings": [],
        "prices": {name: {"effective": None, "source": "unknown"} for name in registry.PRICE_FIELDS},
    }
    profile = {
        "profile_id": "example-profile", "name": "Example worker", "runtime_id": "rt-claude-code",
        "runtime_name": "Claude Code", "runtime_type": "claude-code", "model_id": "example-model",
        "model_path": "translated", "model": model, "provider": provider, "agent_teams": True, "effort": None,
        "custom_prompt": "", "builtin": False, "roles": ["worker"], "warnings": [],
        "used_by": {"layers": [], "task_count": 0, "schedule_count": 0, "allowed_in": []},
        "cost": {"source": "provider_price", "equivalent": False, "unpriced": []},
    }
    data = {
        "runtime_types": [runtime_type], "runtimes": [{**runtime_type, "name": "Claude Code", "builtin": True,
             "type_label": "Claude Code", "launcher": "native"}],
        "providers": [provider], "models": [model], "profiles": [profile], "vendor_types": [],
        "limits": registry._limits(), "prices": {"models": {}, "sources": [], "as_of": ""},
    }
    with sync_playwright() as driver:
        from pathlib import Path

        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")
        browser = driver.chromium.launch(headless=True, timeout=15000)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.set_default_timeout(5000)

            def route(request):
                path = urlsplit(request.request.url).path
                if not path.startswith("/api/"):
                    request.fulfill(path=str(staged_dashboard_dir / (path.lstrip("/") or "index.html")))
                elif path == "/api/v1/registry/view":
                    request.fulfill(json={"status": "ok", "result": data})
                elif path == "/api/v1/system/status":
                    request.fulfill(json={"status": "ok", "result": {"checks": []}})
                elif path == "/api/v1/project_admin/list":
                    request.fulfill(json={"status": "ok", "result": {"projects": []}})
                else:
                    request.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside settings verification"}})

            page.route("**/*", route)
            page.goto("http://settings.example/#settings/profiles", wait_until="networkidle", timeout=15000)
            expect(page.get_by_text("via local gateway", exact=True)).to_be_visible()
            page.get_by_role("button", name="Edit Agent Profile Example worker", exact=True).click()
            runtime = page.get_by_label("Runtime", exact=True)
            expect(runtime).to_have_value("rt-claude-code")
            expect(runtime.locator("option:checked")).to_contain_text("via local gateway")
            expect(page.get_by_text("Runs are refused when the gateway is unavailable.", exact=False)).to_be_visible()
            page.screenshot(path=str(tmp_path / "translated-profile-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 768, "height": 1000})
            runtime.focus()
            expect(runtime).to_be_focused()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / "translated-profile-narrow.png"), full_page=True)
            page.goto("http://settings.example/#settings/providers", wait_until="networkidle", timeout=15000)
            page.get_by_role("button", name="Models for provider Example account", exact=True).click()
            page.get_by_role("button", name="Edit model example-chat (openai-chat)", exact=True).click()
            dialect = page.get_by_label("Chat dialect", exact=True)
            expect(dialect).to_be_visible()
            expect(dialect.locator("option:checked")).to_have_text("Auto (from provider: deepseek)")
            dialect.select_option("openai")
            expect(dialect).to_have_value("openai")
            page.screenshot(path=str(tmp_path / "chat-dialect-narrow.png"), full_page=True)
        finally:
            browser.close()
