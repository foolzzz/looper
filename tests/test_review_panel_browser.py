"""Review panels in the built dashboard, without a service or model processes."""
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.installed
STAMP = "2026-01-01T00:00:00Z"
FINDING = {"member": "review_a", "severity": "suggestion", "where": "src/cache.py:18", "text": "复用缓存 / Reuse the cache."}


def _task():
    steps = []
    for index, key in enumerate(("dev", "review_a", "review_b", "review_c", "verify_staging")):
        review = key.startswith("review_")
        result = None
        if not index:
            result = {"status": "completed", "summary": "Done.", "verify_passed": True, "verify_findings": [FINDING]}
        elif review and key != "review_c":
            result = {"status": "completed", "passed": True, "findings": [FINDING]}
        steps.append({
            "id": key, "step_key": key, "step_index": index, "task_id": "panel-example",
            "kind": "review" if review else "work", "reviews": "dev" if review else None,
            "instruction": key, "status": "pending" if key in ("review_c", "verify_staging") else "verified",
            "depends_on": ["dev"] if index else [], "acceptance_criteria": [], "target_files": [],
            "ready_when": {"step": "dev", "on": "completed"} if review else None,
            "accept_when": {"all_of": [{"step": member, "on": "passed"} for member in ("review_a", "review_b")]} if not index else None,
            "accept_state": "satisfied" if not index else None,
            "review": False, "review_roles": [], "role_key": key if review else None,
            "role_version": None, "review_stage": None, "review_position": None, "retries": 0,
            "worker_id": None, "reviewer_id": None, "result": result,
            "created_at": STAMP, "updated_at": STAMP, "started_at": None, "completed_at": None,
        })
    return {
        "task_id": "panel-example", "name": "评审小组示例", "title": "评审小组示例",
        "requirement": "查看必选与建议评审", "goal_id": "", "status": "stopped", "phase": "stopped",
        "group": "attention", "planner_id": None, "project_id": "example", "project_name": "示例",
        "work_dir": "", "cost": None, "config": {}, "created_at": STAMP, "updated_at": STAMP,
        "steps": steps, "journal": [], "progress": [], "roles": [], "gates": [], "waits": [],
        "agents": None, "builtin_role_stats": None, "profile_overrides": {}, "requirement_versions": [],
        "supplements": [], "manager_actions": [],
    }


@pytest.mark.parametrize("width", [1280, 768])
def test_review_panel_junction_advisory_and_downstream(staged_dashboard_dir, width):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, timeout=15000)
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def respond(route):
                path = urlsplit(route.request.url).path
                if not path.startswith("/api/"):
                    asset = staged_dashboard_dir / (path.lstrip("/") or "index.html")
                    route.fulfill(path=str(asset)) if asset.is_file() else route.fulfill(status=404, body="")
                    return
                result = {
                    "/api/v1/tasks/detail": _task(), "/api/v1/runs/for_task": {"runs": []},
                    "/api/v1/tasks/activity": {"activity": []}, "/api/v1/project_admin/list": {"projects": []},
                    "/api/v1/system/status": {"checks": []},
                }.get(path)
                route.fulfill(json={"status": "ok", "result": result}) if result is not None else route.fulfill(status=503, body="")

            page.route("**/*", respond)
            page.goto("http://panel.example/#tasks/panel-example?tab=pipeline", timeout=15000)
            junction = page.locator('.react-flow__node-condition[data-id="dev:a0"]')
            expect(junction).to_have_accessible_name("dev：必选全部通过，已满足")
            expect(page.locator(".react-flow__node-condition")).to_have_count(1)
            advisory = page.locator('.react-flow__node[data-id="review_c"]')
            expect(advisory).to_contain_text("建议")
            assert "dashed" in advisory.get_attribute("style")
            review_y = {page.locator(f'.react-flow__node[data-id="{key}"]').bounding_box()["y"]
                        for key in ("review_a", "review_b", "review_c")}
            assert len(review_y) == 1
            assert page.locator('[data-id="dev"]').bounding_box()["y"] < next(iter(review_y)) < junction.bounding_box()["y"]
            expect(page.locator('[data-id="dep:dev:a0->verify_staging"]')).to_have_count(1)
            expect(page.locator('[data-id="dep:review_c->dev:a0"]')).to_have_count(1)
            junction.focus()
            expect(junction).to_be_focused()
            for key in ("review_a", "dev"):
                page.locator(f'.react-flow__node[data-id="{key}"]').hover()
                card = page.get_by_role("dialog", name=f"{key}: details", exact=True)
                expect(card).to_be_visible()
                expect(card).to_contain_text("review_a · suggestion · src/cache.py:18: 复用缓存 / Reuse the cache.")
                expect(card).not_to_contain_text("[object Object]")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            output = Path(__file__).resolve().parents[1] / "output/playwright" / f"pipeline-review-panel-{width}.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output), full_page=True)
            assert not errors
        finally:
            browser.close()
