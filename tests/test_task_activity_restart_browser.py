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
@pytest.mark.parametrize("outcome", ["running", "stopped", "completed", "failed", "refused"])
def test_sessions_resume_after_async_restart(page, outcome):
    page, base = page
    state = {"starts": 0, "reads": 0, "streams": 0, "new": False}
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
            if state["starts"] and outcome != "refused":
                state["reads"] += 1
                state["new"] = state["reads"] > 1  # First read is the unchanged old terminal task.
            result = dict(task)
            if state["new"]:
                result.update(status=outcome, cost={**cost, "runs": 1 if outcome == "failed" else 2})
                if outcome == "failed":
                    result.update(error="Workspace could not start", completed_at="2026-10-24T00:02:00Z")
        elif path == "ops/task_control/restart":
            state["starts"] += 1
            if outcome == "refused":
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
    page.get_by_role("button", name="Start", exact=True).click()
    if outcome == "refused":
        expect(page.get_by_role("alert")).to_have_text("Start refused")
        page.wait_for_timeout(2300)
        assert state["reads"] == 0 and state["streams"] == initial_streams
    else:
        expect(page.get_by_text("Resumed synthetic activity", exact=True)).to_be_visible()
        assert state["reads"] >= 2 and state["streams"] == initial_streams + 1
        if outcome == "failed":
            expect(page.get_by_role("alert")).to_have_text("Workspace could not start")
        if outcome != "running":
            reads = state["reads"]
            page.wait_for_timeout(2300)
            assert state["reads"] == reads
        if outcome == "stopped" and (screenshot := os.environ.get("LOOPER_RESTART_SCREENSHOT")):
            page.screenshot(path=screenshot)
