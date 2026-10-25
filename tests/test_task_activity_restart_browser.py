"""Real built dashboard with deterministic asynchronous restart responses."""
import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def page(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    assert (dist / "index.html").exists(), "Build web before running this browser regression"
    with playwright.sync_playwright() as driver:
        executable = driver.chromium.executable_path
    monkeypatch.setenv("HOME", str(tmp_path))
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, executable_path=executable)
        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=str(dist)))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with browser.new_context(viewport={"width": 1280, "height": 800}) as context:
                page = context.new_page()
                page.set_default_timeout(8000)
                yield page, f"http://127.0.0.1:{server.server_port}"
        finally:
            browser.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.installed
@pytest.mark.parametrize("outcome", ["running", "stopped", "completed", "failed", "refused",
                                     "mixed", "before_request", "start_over", "refused_over"])
def test_sessions_resume_after_async_restart(page, outcome):
    page, base = page
    state = {"starts": 0, "reads": 0, "streams": 0, "new": False, "details": 0}
    refused = outcome in ("refused", "refused_over")
    start_over = outcome in ("start_over", "refused_over")
    final_status = outcome if outcome in ("running", "stopped", "completed", "failed") else "running"
    cost = {"cost_usd": 0, "runs": 1, "running": 0, "unpriced_runs": 0, "by_model": [], "manager_by_model": [],
            "by_role": {role: {"cost_usd": 0, "runs": 0} for role in ("planner", "worker", "reviewer")}}
    task = {"task_id": "restart-demo", "name": "Restart activity demo", "requirement": "Resume task activity",
            "status": "stopped", "group": "attention", "phase": "stopped", "planner_id": "planner-restart-demo",
            "created_at": "2026-10-24T00:00:00Z", "updated_at": "2026-10-24T00:01:00Z", "completed_at": None,
            "project_id": "demo", "project_name": "Demo", "cost": cost, "steps": [], "gates": [], "roles": [],
            "error": None, "agents": None, "config": {}, "schedule_id": None}

    def api(route):
        path = route.request.url.split("/api/v1/")[-1]
        result = {}
        if path == "tasks/detail":
            state["details"] += 1
            # A queued completion after Stop has already changed the row before Start.
            if outcome == "before_request" and state["details"] == 2:
                task.update(completed_at="2026-10-24T00:01:30Z", cost={**cost, "runs": 2})
            if state["starts"] and not refused:
                state["reads"] += 1
                state["new"] = state["reads"] > (2 if outcome in ("before_request", "mixed") else 1)
                if outcome == "completed":
                    state["new"] = True  # The requested run finished before its first read.
            result = dict(task)
            if outcome == "mixed" and state["starts"] and not state["new"]:
                result["cost"] = {**cost, "runs": 2, "running": 1}  # New run, old terminal status.
            if state["new"]:
                result.update(status=final_status, cost={**cost, "runs": 1 if outcome == "failed" else 3})
                result["group"] = "in_progress" if final_status == "running" else "attention"
                if outcome == "failed":
                    result.update(error="Workspace could not start", completed_at="2026-10-24T00:02:00Z")
        elif path in ("ops/task_control/restart", "ops/task_control/start_over"):
            assert path.endswith("start_over" if start_over else "restart")
            state["starts"] += 1
            if refused:
                route.fulfill(status=409, json={"status": "error", "error": {"code": "conflict", "message": "Start refused"}})
                return
            result = {"task_id": task["task_id"], "restarted": True, "note": "Accepted"}
        elif path == "tasks/activity":
            result = {"activity": [], "has_more": False}
        elif path == "tasks/activity_stream":
            state["streams"] += 1
            event = {"seq": 1, "task_id": task["task_id"], "worker": "planner", "kind": "text",
                     "summary": "Resumed synthetic activity", "created_at": "2026-10-24T00:02:00Z"}
            body = f"event: activity\ndata: {json.dumps(event)}\n\n" if state["new"] else ""
            route.fulfill(content_type="text/event-stream", body=body + "event: done\ndata: {}\n\n")
            return
        elif path == "runs/for_task":
            result = {"runs": []}
        elif path == "project_admin/list":
            result = {"projects": []}
        elif path == "ops/gates/list":
            result = {"gates": [], "total": 0}
        elif path == "tasks/page":
            result = {"tasks": [], "counts": {}, "next_before": None}
        else:
            route.fulfill(status=503, json={"status": "error", "error": {"code": "unavailable", "message": "Not in fixture"}})
            return
        route.fulfill(json={"status": "ok", "result": result})

    page.route("**/api/**", api)
    page.goto(f"{base}/#tasks/restart-demo?tab=sessions")
    expect = pytest.importorskip("playwright.sync_api").expect
    expect(page.get_by_text("No activity yet", exact=True)).to_be_visible()
    page.wait_for_function("!document.body.innerText.includes('Live')")
    initial_streams = state["streams"]
    assert initial_streams >= 1  # Positive control: the initial terminal stream delivered done.
    if start_over:
        page.get_by_role("button", name="Start over", exact=True).click()
        expect(page.get_by_text("Delete its steps?", exact=True)).to_be_visible()
    page.get_by_role("button", name="Start over" if start_over else "Start", exact=True).click()
    if refused:
        expect(page.get_by_role("alert")).to_have_text("Start refused")
        expect(page.get_by_role("button", name="Start", exact=True)).to_be_enabled()
        expect(page.get_by_role("button", name="Start over", exact=True)).to_be_enabled()
        expect(page.get_by_text("Delete its steps?", exact=True)).to_have_count(0)
        page.wait_for_timeout(2300)
        assert state["reads"] == 0 and state["streams"] == initial_streams
    else:
        if start_over:
            expect(page.get_by_text("Delete its steps?", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Start", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Start over", exact=True)).to_be_disabled()
        page.wait_for_timeout(300)
        assert state["reads"] == 1 and state["streams"] == initial_streams
        if outcome == "mixed" and (screenshot := os.environ.get("LOOPER_RESTART_PENDING_SCREENSHOT")):
            page.set_viewport_size({"width": 768, "height": 800})
            page.screenshot(path=screenshot)
        expect(page.get_by_text("Resumed synthetic activity", exact=True)).to_be_visible()
        assert state["reads"] >= 2 and state["streams"] == initial_streams + 1
        if outcome == "failed":
            expect(page.get_by_role("alert")).to_have_text("Workspace could not start")
        if final_status != "running":
            reads = state["reads"]
            page.wait_for_timeout(2300)
            assert state["reads"] == reads
        if outcome == "stopped" and (screenshot := os.environ.get("LOOPER_RESTART_SCREENSHOT")):
            page.set_viewport_size({"width": 768, "height": 800})
            page.screenshot(path=screenshot)
    assert state["starts"] == 1
