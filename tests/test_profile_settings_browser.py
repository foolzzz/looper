"""Knowledge-vault profile controls against a fresh service, without model calls."""

from pathlib import Path

import pytest

from fake_claude import fake_claude
from evals.prompt_regression.runner import running_server
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = [pytest.mark.installed, pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")]


def test_profile_source_and_switch_roundtrip_in_settings(tmp_path, staged_dashboard_dir):
    from playwright.sync_api import expect, sync_playwright

    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "关于我.md").write_text("## Name\nAda\n", encoding="utf-8")
    (vault / "notes/profile.md").write_text("## Name\nBee\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude(bin_dir / "claude", "print('2.1.0')\n")
    screenshots = Path(".local/validation/manager-profile")
    screenshots.mkdir(parents=True, exist_ok=True)
    with fresh_database_without_recording(DSN) as (database_url, _), sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        try:
            with running_server(database_url=database_url, runtime_root=tmp_path / "runtime", bin_dir=bin_dir,
                                log_path=tmp_path / "serve.log", home=tmp_path / "home") as api:
                page = browser.new_page(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
                page.set_default_timeout(10000)
                page.goto(f"{api.base_url}/#settings/vault")
                # The dashboard keeps an SSE connection open; wait for the actual form below.
                source = page.get_by_label("画像文件", exact=True)
                root = page.get_by_label("Vault folder", exact=True)
                form = page.locator("form").filter(has=source)
                switch = page.get_by_role("switch", name="管家常驻画像", exact=True)
                expect(source).to_have_value("关于我.md")
                expect(switch).to_have_attribute("aria-checked", "false")
                form.locator("..").screenshot(path=str(screenshots / "before.png"))
                root.fill(str(vault))
                source.fill("notes/profile.md")
                form.get_by_role("button", name="Save", exact=True).click()
                expect(form.get_by_text("Saved.", exact=True)).to_be_visible()
                page.reload()
                expect(source).to_have_value("notes/profile.md")
                assert api.post("/api/v1/knowledge_vault/get", {})["result"]["profile_source"] == "notes/profile.md"

                page.once("dialog", lambda dialog: dialog.dismiss())
                switch.click()
                expect(switch).to_have_attribute("aria-checked", "false")
                for enabled in (True, False):
                    page.once("dialog", lambda dialog: dialog.accept())
                    switch.click()
                    expect(switch).to_have_attribute("aria-checked", str(enabled).lower())
                    assert api.post("/api/v1/profile_settings/get", {})["result"]["enabled"] is enabled
                    page.reload()
                    expect(switch).to_have_attribute("aria-checked", str(enabled).lower())

                for invalid in ("../outside.md", "missing.md"):
                    source.fill(invalid)
                    with page.expect_response(lambda response: response.url.endswith("/api/v1/knowledge_vault/update")) as failure:
                        form.get_by_role("button", name="Save", exact=True).click()
                    assert failure.value.status == 400
                    expect(form.get_by_role("alert")).to_be_visible()
                    assert api.post("/api/v1/knowledge_vault/get", {})["result"]["profile_source"] == "notes/profile.md"
                page.screenshot(path=str(screenshots / "error.png"), full_page=True, mask=[root], mask_color="#303030")
                source.fill("")
                form.get_by_role("button", name="Save", exact=True).click()
                expect(source).to_have_value("关于我.md")
                page.reload()
                expect(source).to_have_value("关于我.md")
                assert api.post("/api/v1/knowledge_vault/get", {})["result"]["profile_source"] == "关于我.md"
                source.focus()
                page.keyboard.press("Tab")
                expect(form.get_by_role("button", name="Save", exact=True)).to_be_focused()
                for width, height in ((1280, 900), (768, 900), (390, 844), (844, 390)):
                    page.set_viewport_size({"width": width, "height": height})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    form.locator("..").screenshot(path=str(screenshots / f"configured-{width}.png"),
                                                 mask=[root], mask_color="#303030")
                page.route("**/api/v1/knowledge_vault/get", lambda route: route.fulfill(
                    status=503, json={"status": "error", "error": {"code": "unavailable", "message": "Synthetic unavailable"}}))
                page.reload()
                expect(page.get_by_role("alert").filter(has_text="Synthetic unavailable")).to_be_visible()
                page.screenshot(path=str(screenshots / "unavailable.png"), full_page=True)
                page.unroute("**/api/v1/knowledge_vault/get")
                page.get_by_role("button", name="Retry", exact=True).click()
                expect(source).to_have_value("关于我.md")
                form.get_by_role("button", name="Turn off", exact=True).click()
                expect(root).to_have_value("")
                expect(source).to_have_value("关于我.md")
                page.screenshot(path=str(screenshots / "empty.png"), full_page=True)
        finally:
            browser.close()
