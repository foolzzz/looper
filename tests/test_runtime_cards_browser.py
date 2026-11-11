"""Agent Runtimes readback with version-only CLI stand-ins and no model or login calls."""

import asyncio
from urllib.parse import urlsplit

import pytest

from fake_claude import fake_claude
from looper.agents import profile_registry as registry, runtimes
from looper.agents.runtimes import ttadk as ttadk_runtime


@pytest.mark.installed
@pytest.mark.parametrize("ttadk_detected", [True, False], ids=["ttadk-detected", "ttadk-not-detected"])
def test_agent_runtimes_launcher_and_operator_configuration(tmp_path, monkeypatch, staged_dashboard_dir, ttadk_detected):
    from playwright.sync_api import expect, sync_playwright

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, version in [("ttadk", "1.0.37"), ("codex", "codex-cli 0.162.0"),
                          ("cursor-agent", "2026.10.22-27dcf5f"), ("coco", "traecli 0.214.1"),
                          ("traex", "traecli 0.214.1")]:
        if name == "ttadk" and not ttadk_detected:
            continue
        fake_claude(bin_dir / name, "import sys\n"
                    "assert sys.argv[1:] == ['--version'], 'Only version probes are allowed'\n"
                    f"print({version!r})\n")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(ttadk_runtime, "tool_command", lambda tool: {
        "codex": "codex", "cursor-cli": "agent", "trae-cli": "traex"}[tool])

    async def snapshot():
        rows = []
        for kind, launcher in [("codex", runtimes.Launcher.NATIVE), ("cursor-agent", runtimes.Launcher.NATIVE),
                               ("codex", runtimes.Launcher.TTADK), ("coco", runtimes.Launcher.NATIVE),
                               ("coco", runtimes.Launcher.TTADK)]:
            if launcher is runtimes.Launcher.TTADK and not ttadk_detected:
                continue
            adapter = runtimes.adapter(kind)
            probe = await registry._probe(kind, "", "claude", launcher)
            assert probe["probe_error"] is None, probe
            row = registry.runtime_view({
                "id": registry.builtin_runtime_id(adapter, launcher), "type": kind, "launcher": launcher.value,
                "name": adapter.ttadk_runtime_name if launcher is runtimes.Launcher.TTADK else kind,
                "executable": "", "builtin": True, "profile_count": 0, "probed_at": None,
                "created_at": "", "updated_at": "", **probe,
            })
            # Public screenshot paths describe the stand-ins without exposing the checkout or temporary directory.
            row["path"] = f"/usr/local/bin/{row['default_executable']}"
            rows.append(row)
        return {"runtime_types": [registry.type_view(runtimes.adapter(kind)) for kind in ("codex", "cursor-agent", "coco")],
                "runtimes": rows, "profiles": [], "providers": [], "models": [], "vendor_types": [],
                "prices": {"as_of": "", "sources": [], "models": {}}, "limits": registry._limits()}

    data = asyncio.run(asyncio.wait_for(snapshot(), 15))
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.set_default_timeout(5000)

            def route(request):
                path = urlsplit(request.request.url).path
                if not path.startswith("/api/"):
                    request.fulfill(path=str(staged_dashboard_dir / (path.lstrip("/") or "index.html")))
                    return
                result = {"/api/v1/registry/view": data, "/api/v1/system/status": {"checks": []},
                          "/api/v1/project_admin/list": {"projects": []}}.get(path)
                if result is None:
                    request.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside runtime verification"}})
                else:
                    request.fulfill(json={"status": "ok", "result": result})

            page.route("**/*", route)
            page.goto("http://runtimes.example/#settings/runtimes", wait_until="networkidle", timeout=15000)
            ttadk = page.get_by_role("listitem").filter(has=page.get_by_role("heading", name="ttadk-codex", exact=True))
            expect(ttadk.get_by_text("TTADK", exact=True)).to_be_visible()
            if ttadk_detected:
                expect(ttadk).to_contain_text("ttadk 1.0.37 / codex-cli 0.162.0")
                coco = page.get_by_role("listitem").filter(has=page.get_by_role("heading", name="ttadk-coco", exact=True))
                expect(coco).to_contain_text("ttadk 1.0.37 / traecli 0.214.1")
                expect(coco.get_by_role("button", name="Re-detect ttadk-coco", exact=True)).to_be_enabled()
            else:
                for name in ("ttadk-codex", "ttadk-cursor-agent", "ttadk-coco"):
                    card = page.get_by_role("listitem").filter(has=page.get_by_role("heading", name=name, exact=True))
                    expect(card).to_contain_text("Not detected")
                    expect(card).to_contain_text("Install TTADK and sign in (ttadk auth login sso), then Re-detect.")
                    expect(card.get_by_role("button", name=f"Re-detect {name}", exact=True)).to_be_enabled()
            cursor = page.get_by_role("listitem").filter(has=page.get_by_role("heading", name="Cursor CLI", exact=True))
            expect(cursor).to_contain_text("your personal Cursor and Claude Code configuration")
            expect(cursor).to_contain_text("cannot be turned off")
            codex = page.get_by_role("listitem").filter(has=page.get_by_role("heading", name="Codex CLI", exact=True))
            expect(codex).not_to_contain_text("personal Cursor")
            page.screenshot(path=str(tmp_path / "agent-runtimes.png"), full_page=True)
            page.set_viewport_size({"width": 768, "height": 1000})
            expect(cursor.get_by_text("Runs with your own Cursor login and HOME:", exact=False)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("button", name="Re-detect Cursor CLI", exact=True).focus()
            expect(page.get_by_role("button", name="Re-detect Cursor CLI", exact=True)).to_be_focused()
        finally:
            browser.close()
