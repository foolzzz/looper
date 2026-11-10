"""Dependency conditions in the built dashboard, with synthetic v1 data and no live service."""
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.installed
ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-01-01T00:00:00Z"


def _step(key, index, status="pending", dependencies=(), condition=None, state=None):
    return {
        "id": key, "step_key": key, "step_index": index, "task_id": "pipeline-example", "kind": "work",
        "instruction": key, "status": status, "depends_on": list(dependencies),
        "ready_when": condition, "ready_state": state, "acceptance_criteria": [], "target_files": [],
        "review": False, "review_roles": [], "role_key": None, "role_version": None,
        "review_stage": None, "review_position": None, "retries": 0, "worker_id": None,
        "reviewer_id": None, "result": None, "created_at": STAMP, "updated_at": STAMP,
        "started_at": None, "completed_at": None,
    }


def _task(scenario):
    if scenario == "nested-any":
        condition = {"any_of": [
            {"all_of": [{"step": "dev", "on": "completed"}, {"step": "review_a", "on": "passed"}]},
            {"any_of": [{"step": "review_b", "on": "passed"}, {"step": "review_c", "on": "passed"}]},
        ]}
        steps = [_step("dev", 0, "completed"), _step("review_a", 1, "verified"),
                 _step("review_b", 2), _step("review_c", 3, "failed")]
        state = "satisfied"
    else:
        condition = {"k_of": 2, "of": [
            {"step": key, "on": "passed"} for key in ("dev", "review_a", "review_b")
        ]}
        steps = [_step("dev", 0, "verified"), _step("review_a", 1, "failed"), _step("review_b", 2, "failed")]
        state = "unsatisfiable"
    steps.append(_step("verify_staging", len(steps), dependencies=[step["id"] for step in steps],
                       condition=condition, state=state))
    return {
        "task_id": "pipeline-example", "name": "依赖条件示例", "title": "依赖条件示例",
        "requirement": "检查依赖组合与状态", "goal_id": "", "status": "stopped", "phase": "stopped",
        "group": "attention", "planner_id": None, "correlation_id": "", "project_id": "example",
        "project_name": "示例", "work_dir": "", "cost": None, "schedule_id": None, "schedule_name": None,
        "continues_schedule_session": False, "config": {}, "origin_channel_id": "", "result_summary": None,
        "error": None, "fail_kind": None, "task_budget_usd": None, "own_budget_usd": None,
        "created_at": STAMP, "updated_at": STAMP, "completed_at": None, "attempt_ended_at": STAMP,
        "steps": steps, "journal": [], "progress": [], "roles": [], "gates": [], "waits": [],
        "agents": None, "builtin_role_stats": None, "profile_overrides": {}, "requirement_versions": [],
        "supplements": [], "manager_actions": [],
    }


@contextmanager
def _dashboard(staged_dashboard_dir, task, width, before_runs=None):
    from playwright.sync_api import sync_playwright

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
                    if asset.is_file():
                        route.fulfill(path=str(asset))
                    else:
                        route.fulfill(status=404, body="")
                    return
                result = {
                    "/api/v1/tasks/detail": task,
                    "/api/v1/runs/for_task": {"runs": []},
                    "/api/v1/tasks/activity": {"activity": []},
                    "/api/v1/project_admin/list": {"projects": []},
                    "/api/v1/system/status": {"checks": []},
                }.get(path)
                if path == "/api/v1/runs/for_task" and before_runs is not None:
                    result = before_runs(page)
                if result is None:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside dependency preview"}})
                else:
                    route.fulfill(json={"status": "ok", "result": result})

            page.route("**/*", respond)
            page.goto("http://pipeline.example/#tasks/pipeline-example?tab=pipeline", timeout=15000)
            yield page
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("scenario,width", [("nested-any", 1280), ("nested-any", 768),
                                           ("k-of", 1280), ("k-of", 768)])
def test_condition_junctions_labels_states_and_keyboard(staged_dashboard_dir, scenario, width):
    from playwright.sync_api import expect

    with _dashboard(staged_dashboard_dir, _task(scenario), width) as page:
        root = page.locator('.react-flow__node-condition[data-id="verify_staging:c0"]')
        expect(root).to_be_visible()
        if scenario == "nested-any":
            expect(page.locator(".react-flow__node-condition")).to_have_count(3)
            name = "verify_staging：任一，已满足"
            child = page.locator('.react-flow__node-condition[data-id="verify_staging:c0.0"]')
            expect(child).to_have_accessible_name("verify_staging：全部，已满足")
            expect(page.locator('[data-id="verify_staging:c0.1"]')).to_have_accessible_name(
                "verify_staging：任一，等待中")
            expect(page.locator(".react-flow__edge-text", has_text="完成")).to_have_count(1)
            assert child.bounding_box()["y"] < root.bounding_box()["y"]
        else:
            expect(page.locator(".react-flow__node-condition")).to_have_count(1)
            name = "verify_staging：2/3，无法满足"
            expect(root).to_contain_text("无法满足")
        expect(root).to_have_accessible_name(name)
        expect(page.locator(".react-flow__edge-text", has_text="通过")).not_to_have_count(0)
        assert root.bounding_box()["y"] < page.locator('[data-id="verify_staging"]').bounding_box()["y"]
        root.focus()
        page.keyboard.press("Shift+Tab")
        expect(root).not_to_be_focused()
        page.keyboard.press("Tab")
        expect(root).to_be_focused()
        assert name in page.locator(".react-flow").aria_snapshot()
        page.keyboard.press("Enter")
        page.locator('.react-flow__node[data-id="dev"]').hover()
        expect(page.get_by_role("dialog", name="dev: details", exact=True)).to_be_visible()
        expect(root).to_be_focused()
        page.keyboard.press("Escape")
        root.focus()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        output = ROOT / "output/playwright" / f"pipeline-conditions-{scenario}-{width}.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output), full_page=True)


def test_plain_all_of_preserves_rendered_pipeline(staged_dashboard_dir):
    from playwright.sync_api import expect

    task = _task("k-of")
    target = task["steps"][-1]
    target["ready_when"] = None
    target["ready_state"] = None
    with _dashboard(staged_dashboard_dir, task, 1280) as page:
        def snapshot():
            expect(page.locator(".react-flow__node")).to_have_count(6)
            expect(page.locator(".react-flow__edge-path")).to_have_count(7)
            expect(page.locator(".react-flow__node-condition")).to_have_count(0)
            return page.evaluate(r"""async () => {
                await document.fonts.ready;
                await new Promise(requestAnimationFrame);
                await new Promise(requestAnimationFrame);
                return {
                    nodes: [...document.querySelectorAll('.react-flow__node')].map(n => ({
                        id: n.dataset.id, text: n.textContent, style: n.getAttribute('style'),
                        label: n.getAttribute('aria-label'), details: n.getAttribute('aria-details'),
                    })),
                    edges: [...document.querySelectorAll('.react-flow__edge')].map(e => ({
                        id: e.dataset.id, text: e.textContent,
                        // Fit-view transforms introduce sub-pixel floating-point noise on reload.
                        path: e.querySelector('.react-flow__edge-path').getAttribute('d').replace(
                            /-?\d+(?:\.\d+)?(?:e[-+]?\d+)?/gi, n => String(Math.round(Number(n) * 1000) / 1000)),
                        style: e.querySelector('.react-flow__edge-path').getAttribute('style'),
                    })),
                };
            }""")

        legacy = snapshot()
        target["ready_when"] = {"all_of": ["dev", {"all_of": ["review_a", "review_b"]}]}
        target["ready_state"] = "unsatisfiable"
        page.reload()
        assert snapshot() == legacy


def test_pipeline_stays_visible_when_runs_arrive_after_node_measurement(staged_dashboard_dir):
    from playwright.sync_api import expect

    task = _task("k-of")
    task["planner_id"] = "planner-example"
    task["steps"][-1]["ready_when"] = None
    task["steps"][-1]["ready_state"] = None

    def before_runs(page):
        # Resolve the run read only after React Flow has measured the original graph.
        expect(page.locator(".react-flow__edge-path")).to_have_count(7)
        expect(page.locator('.react-flow__node[data-id="planner"]')).to_be_visible()
        return {"runs": [{
            "id": 1, "agent_id": "planner-example", "role": "planner", "status": "completed",
            "profile": {"name": "Example", "source": "default"},
            "runtime": {"name": "Example runtime", "type": "api"},
            "model": None, "effort": None, "created_at": STAMP,
        }]}

    with _dashboard(staged_dashboard_dir, task, 1280, before_runs=before_runs) as page:
        planner = page.locator('.react-flow__node[data-id="planner"]')
        planner.hover()
        card = page.get_by_role("dialog", name="Planner: details", exact=True)
        expect(card).to_contain_text("Example runtime")
        expect(planner).to_be_visible()
        expect(page.locator(".react-flow__edge-path")).to_have_count(7)
