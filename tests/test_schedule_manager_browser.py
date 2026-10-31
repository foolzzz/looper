"""Schedule ownership through the built dashboard and synthetic v1 responses."""

import base64
from pathlib import Path

import pytest

from tests.test_chat_navigation_browser import dashboard_url  # noqa: F401

pytestmark = pytest.mark.installed


@pytest.fixture
def dashboard(dashboard_url):  # noqa: F811
    playwright = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")

    with playwright.sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_default_timeout(5000)
        project = {"project_id": "proj-example", "name": "Example", "work_dir": "/srv/example",
                   "description": "", "background": "", "task_count": 0, "active_count": 0, "schedule_count": 0}
        state = {"calls": [], "schedules": [], "scenarios": True, "refuse": False, "owner_error": False,
                 "runs": [], "logs": {}, "overview": {}}
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        limits = {"name_max_chars": 80, "requirement_max_chars": 10000, "default_timezone": "UTC",
                  "min_interval_seconds": 300, "min_interval_gated_seconds": 60, "max_parallel_runs_limit": 10,
                  "concurrency_max": 100, "gate_command_max_chars": 2000, "gate_timeout_max_seconds": 300,
                  "gate_timeout_default_seconds": 30, "run_timeout_max_seconds": 86400,
                  "roles": ["planner", "worker", "reviewer"], "notify_targets": []}

        def respond(route):
            path = route.request.url.split("/api/v1/")[-1]
            body = route.request.post_data_json or {}
            state["calls"].append((path, body))
            if path == "managers/list":
                if state["owner_error"]:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Owner list unavailable"}})
                    return
                managers = [{"id": "manager", "name": "Default", "enabled": True}]
                if state["scenarios"]:
                    managers += [{"id": "manager-review", "name": "Review", "enabled": True},
                                 {"id": "manager-paused", "name": "Paused", "enabled": False}]
                result = {"managers": managers, "max_enabled": 8}
            elif path == "schedule_admin/create":
                if state["refuse"]:
                    route.fulfill(status=409, json={"status": "error", "error": {
                        "code": "conflict", "message": "Owner is no longer available"}})
                    return
                owner = body["fields"].get("manager_id", "manager")
                schedule = {**body["fields"], "schedule_id": "sched-example", "project_id": project["project_id"],
                    "project_name": project["name"], "manager_id": owner,
                    "manager_name": {"manager": "Default", "manager-review": "Review", "manager-paused": "Paused"}[owner],
                    "description": "Every hour (UTC)", "enabled": False, "next_fire_at": None, "last_run": None,
                    "work_dir": "", "max_parallel_runs": 3, "run_budget_usd": None, "daily_budget_usd": None,
                    "profile_overrides": {}, "task_limits": {"worker": None, "reviewer": None},
                    "gate_command": None, "gate_timeout_seconds": None, "notify_target": None,
                    "spent_today_usd": 0, "consecutive_failures": 0,
                    "planner_session": {"active": False}, "alerts": {"failures_since": None, "stale_since": None}}
                state["schedules"].append(schedule)
                result = {"schedule": schedule}
            elif path == "schedule_admin/update":
                assert "manager_id" not in body["fields"]
                state["schedules"][0].update(body["fields"])
                result = {"schedule": state["schedules"][0]}
            elif path == "schedule_admin/runs":
                rows = state["runs"]
                if body.get("status"):
                    rows = [run for run in rows if run["status"] == body["status"]]
                elif body.get("skips") == "hide":
                    rows = [run for run in rows if run["status"] not in {"skipped_gate", "skipped_overlap"}]
                cursor = body.get("before")
                if cursor:
                    rows = rows[next(i + 1 for i, run in enumerate(rows) if run["run_id"] == cursor):]
                limit = min(body.get("limit", 20), state.get("run_page_size", 20))
                zero = {"runs": 0, "succeeded": 0, "failed": 0, "success_rate": None,
                        "avg_seconds": None, "max_seconds": None}
                result = {"runs": rows[:limit], "next_before": rows[limit - 1]["run_id"] if len(rows) > limit else None,
                          "routine_skips": {status: sum(run["status"] == status for run in state["runs"])
                                            for status in ("skipped_gate", "skipped_overlap")},
                          "stats": state.get("stats", {"day": zero, "week": zero})}
            elif path == "schedule_admin/run_log":
                data = state["logs"][body["run_id"]]
                offset = body.get("offset", 0)
                end = min(len(data), offset + body.get("limit_bytes", len(data)))
                result = {"size": len(data), "offset": offset,
                          "next_offset": end if end < len(data) else None,
                          "data_base64": base64.b64encode(data[offset:end]).decode("ascii"),
                          "complete": True, "truncated": False}
            else:
                result = {"project_admin/list": {"projects": [project]}, "system/status": {},
                    "system/overview": state["overview"], "ops/gates/list": {"gates": [], "total": 0},
                    "chat_settings/get": {"stop_enabled": False},
                    "registry/view": {"profiles": [], "models": [], "runtimes": [], "providers": []},
                    "schedule_admin/project_list": {"schedules": state["schedules"], "limits": limits},
                    "schedule_admin/list": {"schedules": state["schedules"]},
                    "schedule_admin/get": {"schedule": state["schedules"][0] if state["schedules"] else None},
                    "schedule_admin/notifications": {"notifications": []},
                    "schedule_admin/preview": {"valid": True, "description": "Every hour (UTC)", "next": []},
                }.get(path)
                if result is None:
                    route.fulfill(status=503, json={"status": "error", "error": {
                        "code": "unavailable", "message": "Outside schedule ownership fixture"}})
                    return
            route.fulfill(json={"status": "ok", "result": result})

        context.route("**/api/**", respond)
        try:
            yield page, dashboard_url, state
            assert not errors
        finally:
            context.close()
            browser.close()


def new_schedule(page, base):
    page.goto(f"{base}/#projects/proj-example/schedules")
    page.get_by_role("button", name="New schedule", exact=True).click()
    page.get_by_label("Name *", exact=True).fill("Hourly review")
    page.get_by_label("Rule (cron) *", exact=True).fill("0 * * * *")
    page.get_by_label("Requirement *", exact=True).fill("Review synthetic reports.")


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_scenario_owner_create_reload_and_immutable_edit(dashboard, tmp_path, width):
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    new_schedule(page, base)
    owner = page.get_by_label("Owner", exact=True)
    expect(owner).to_have_value("manager")
    expect(owner.locator("option")).to_have_text(["Default", "Review", "Paused (disabled)"])
    owner.select_option("manager-review")
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_text("Owner: Review. Ownership cannot be changed.", exact=True)).to_be_visible()
    assert state["schedules"][0]["manager_id"] == "manager-review"
    page.reload()
    expect(page.get_by_text("Owner: Review. Ownership cannot be changed.", exact=True)).to_be_visible()
    expect(page.get_by_label("Owner", exact=True)).to_have_count(0)
    page.get_by_label("Name *", exact=True).fill("Updated review")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    assert state["schedules"][0]["manager_id"] == "manager-review"
    page.goto(f"{base}/#schedules")
    expect(page.get_by_text("Owner: Review", exact=True)).to_be_visible()
    page.get_by_role("link", name="Updated review", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Updated review")).to_be_visible()
    expect(page.get_by_text("Owner: Review", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot = tmp_path / f"schedule-owner-{width}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(f"Screenshot: {screenshot}")


def test_failed_create_keeps_owner_and_draft(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["refuse"] = True
    new_schedule(page, base)
    page.get_by_label("Owner", exact=True).select_option("manager-paused")
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("Owner is no longer available")
    expect(page.get_by_label("Name *", exact=True)).to_have_value("Hourly review")
    expect(page.get_by_label("Owner", exact=True)).to_have_value("manager-paused")
    assert not state["schedules"]


def test_default_only_creation_omits_owner_control_and_field(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["scenarios"] = False
    new_schedule(page, base)
    expect(page.get_by_role("button", name="Create", exact=True)).to_be_enabled()
    expect(page.get_by_label("Owner", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_text("Owner: Default. Ownership cannot be changed.", exact=True)).to_be_visible()
    create = next(body for path, body in state["calls"] if path == "schedule_admin/create")
    assert "manager_id" not in create["fields"]


def test_owner_load_failure_is_visible_and_can_retry_without_losing_draft(dashboard):
    from playwright.sync_api import expect

    page, base, state = dashboard
    state["owner_error"] = True
    new_schedule(page, base)
    expect(page.get_by_role("alert")).to_contain_text("Owner list unavailable")
    expect(page.get_by_role("button", name="Create", exact=True)).to_be_disabled()
    state["owner_error"] = False
    page.get_by_role("button", name="Retry owners", exact=True).click()
    expect(page.get_by_label("Owner", exact=True)).to_be_visible()
    expect(page.get_by_label("Name *", exact=True)).to_have_value("Hourly review")


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_command_environment_multiline_round_trip(dashboard, tmp_path, width):
    import json

    from playwright.sync_api import expect

    page, base, state = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    new_schedule(page, base)
    page.get_by_role("radio", name="Command", exact=False).check()
    page.get_by_label("Command *", exact=True).fill("printf ready")
    page.get_by_role("button", name="Create", exact=True).click()
    expect(page.get_by_text("Type: Command", exact=True)).to_be_visible()
    # Simulate a schedule supplied by the API/CLI, including values that look like new entries.
    original = {"SETTINGS": "REGION=us\nTIER=gold", "EMPTY": "",
                "LITERAL": '  C:\\reports\\n="quoted"  '}
    state["schedules"][0]["env"] = original.copy()
    page.reload()
    page.get_by_label("Name *", exact=True).fill("Renamed command")
    with page.expect_response(lambda r: r.url.endswith("/schedule_admin/update")) as response:
        page.get_by_role("button", name="Save", exact=True).click()
    assert response.value.request.post_data_json["fields"] == {"name": "Renamed command"}
    assert state["schedules"][0]["env"] == original

    env = page.get_by_label("Environment", exact=True)
    assert json.loads(env.input_value()) == original
    updated = {**original, "SETTINGS": "REGION=eu\nTIER=silver\n", "CRLF": "a\r\nb\r", "ADDED": " x=y "}
    env.fill(json.dumps(updated))
    with page.expect_response(lambda r: r.url.endswith("/schedule_admin/update")) as response:
        page.get_by_role("button", name="Save", exact=True).click()
    assert response.value.request.post_data_json["fields"] == {"env": updated}
    page.reload()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    assert json.loads(env.input_value()) == updated
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    env.scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f"command-environment-{width}.png"), full_page=True)

    for invalid in ('{"SETTINGS":', '{"SETTINGS": 1}', '{"SETTINGS": null}', '{"BAD-KEY": "value"}'):
        env.fill(invalid)
        expect(env).to_have_attribute("aria-invalid", "true")
        expect(page.locator("form").get_by_role("alert")).to_be_visible()
        expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
    env.fill("{}")
    with page.expect_response(lambda r: r.url.endswith("/schedule_admin/update")) as response:
        page.get_by_role("button", name="Save", exact=True).click()
    assert response.value.request.post_data_json["fields"] == {"env": {}}
    page.reload()
    expect(env).to_have_value("")


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "narrow"])
def test_command_run_history_deep_link_and_alerts(dashboard, width):
    """Read command outcomes and logs through the built dashboard at both supported widths."""
    from playwright.sync_api import expect

    page, base, state = dashboard
    page.set_viewport_size({"width": width, "height": 1100})
    stamp = "2026-10-30T08:00:00Z"
    sid = "sched-command"
    target = "run-failed"
    log = b"Preparing synthetic report\nReport failed: synthetic input unavailable\n"
    schedule = {"kind": "command", "command": "printf report", "env": {}, "env_file": None,
                "schedule_id": sid, "project_id": "proj-example", "project_name": "Example",
                "name": "Synthetic report", "manager_id": "manager", "manager_name": "Default",
                "cron": "0 * * * *", "timezone": "UTC", "description": "Every hour (UTC)",
                "requirement": "", "work_dir": "", "enabled": True, "next_fire_at": stamp,
                "last_fired_at": stamp, "last_success_at": stamp, "consecutive_failures": 1,
                "overlap": "skip", "catch_up": "once", "max_parallel_runs": 1,
                "run_timeout_seconds": 60, "run_timeout_effective_seconds": 60,
                "run_budget_usd": None, "daily_budget_usd": None, "spent_today_usd": 0,
                "profile_overrides": {}, "task_limits": {"worker": None, "reviewer": None},
                "gate_command": None, "gate_timeout_seconds": None,
                "notify": "alerts", "user_gates": "off", "notify_target": None,
                "template_key": None, "template_inputs": {}, "template_name": None,
                "planner_session": {"active": False}, "success_deadline": "08:00",
                "alert_policy": {"failures_after": {"value": 3, "default": True},
                                 "stale_after_seconds": {"value": 86400, "default": True},
                                 "overlap_after": {"value": 10, "default": True}},
                "alerts": {"failures_since": None, "stale_since": None,
                           "deadline_since": stamp, "overlap_since": stamp},
                "created_via": "dashboard", "created_at": stamp, "updated_at": stamp}
    statuses = [("run-success", "succeeded"), ("run-timeout", "timed_out"),
                ("run-interrupted", "interrupted"), ("run-overlap", "skipped_overlap"), (target, "failed")]
    for rid, status in statuses:
        state["runs"].append({"run_id": rid, "schedule_id": sid, "manager_id": "manager",
            "scheduled_for": stamp, "fired_at": stamp, "started_at": stamp,
            "finished_at": "2026-10-30T08:00:20Z", "trigger": "cron", "missed": 0,
            "status": status, "task_id": None, "task_status": None, "task_title": None,
            "summary": None, "cost_usd": None, "error": None, "gate_output": None,
            "carried_from": None, "firings": 1, "exit_code": 7 if status == "failed" else None,
            "exit_signal": None, "duration_seconds": 7 if status == "failed" else 20,
            "log": {"file": f"{rid}.log", "bytes": len(log), "truncated": False},
            "log_tail": "Report failed: synthetic input unavailable" if status == "failed" else ""})
        state["logs"][rid] = log
    skipped = next(run for run in state["runs"] if run["status"] == "skipped_overlap")
    skipped.update(log=None, started_at=None, duration_seconds=0)
    del state["logs"][skipped["run_id"]]
    state["logs"]["run-success"] = b"Report succeeded: synthetic output ready\n"
    state["runs"][0]["exit_code"] = 0
    schedule["last_run"] = state["runs"][0]
    state["schedules"] = [schedule]
    state["run_page_size"] = 3
    stats = {"runs": 4, "succeeded": 1, "failed": 3, "success_rate": 0.25,
             "avg_seconds": 7, "max_seconds": 20}
    state["stats"] = {"day": stats, "week": stats}
    page.goto(f"{base}/#projects/proj-example/schedules/{sid}/runs/{target}")
    history = page.get_by_role("region", name="Run history", exact=True)
    expect(history.get_by_text("exit 7", exact=True)).to_be_visible()
    expect(history.locator(f'li[data-run-id="{target}"]').get_by_role("button", expanded=True)).to_contain_text("7 s")
    expect(history.get_by_text("Report failed: synthetic input unavailable", exact=False)).to_be_visible()
    expect(history.get_by_role("button", name="Download log", exact=True)).to_be_visible()
    assert any(path == "schedule_admin/runs" and body.get("before") for path, body in state["calls"])
    expect(history.get_by_text("Last 24 h:", exact=False)).to_contain_text("1/4 succeeded")
    # The linked run reveals its tail without a separate expansion gesture.
    expect(history.locator("pre")).to_contain_text("Report failed: synthetic input unavailable")
    assert any(path == "schedule_admin/run_log" and body == {
                   "run_id": target, "tail_lines": 200, "limit_bytes": 4 * 1024 * 1024}
               for path, body in state["calls"])
    hide = history.get_by_role("checkbox", name="Hide routine skips", exact=False)
    if hide.is_checked():
        hide.uncheck()
    # Loading all outcomes leaves the linked failure visible beside the other statuses.
    older = history.get_by_role("button", name="Show older runs", exact=True)
    if older.count():
        older.click()
    runs = history.locator("li[data-run-id]")
    expect(runs.get_by_text("Succeeded", exact=True)).to_be_visible()
    expect(runs.get_by_text("Timed out", exact=True)).to_be_visible()
    expect(runs.get_by_text("Interrupted", exact=True)).to_be_visible()
    expect(runs.get_by_text("Skipped: busy", exact=True)).to_be_visible()
    expect(page.get_by_text("Missed its success deadline since", exact=False)).to_be_visible()
    expect(page.get_by_text("Skipping: the previous run is still going since", exact=False)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    artifacts = Path(__file__).resolve().parents[1] / ".local" / "command-run-history"
    artifacts.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": width, "height": max(1100, history.evaluate("element => element.scrollHeight") + 200)})
    history.scroll_into_view_if_needed()
    page.screenshot(path=str(artifacts / f"runs-{width}.png"), full_page=True)
    page.set_viewport_size({"width": width, "height": 1100})

    # Changing only the hash must reopen the collapsed schedule and reveal the newly named run.
    schedule_toggle = page.locator(f"#schedule-{sid}-button")
    schedule_toggle.click()
    expect(schedule_toggle).to_have_attribute("aria-expanded", "false")
    if width == 1440:
        assert schedule_toggle.locator("span").first.evaluate("element => element.scrollWidth <= element.clientWidth")
    page.screenshot(path=str(artifacts / f"closed-alert-schedule-{width}.png"), full_page=True)
    page.evaluate("hash => { location.hash = hash; }", f"#projects/proj-example/schedules/{sid}/runs/run-success")
    success = history.locator('li[data-run-id="run-success"]')
    expect(schedule_toggle).to_have_attribute("aria-expanded", "true")
    expect(success.get_by_role("button", expanded=True)).to_be_in_viewport()
    expect(success.get_by_text("exit 0", exact=True)).to_be_visible()
    expect(success.get_by_label("Run log", exact=True)).to_have_text("Report succeeded: synthetic output ready\n")
    page.screenshot(path=str(artifacts / f"linked-run-after-collapse-{width}.png"), full_page=True)

    # An overlap skip never started the command and must not request or offer a log.
    page.evaluate("hash => { location.hash = hash; }", f"#projects/proj-example/schedules/{sid}/runs/run-overlap")
    no_log = history.locator('li[data-run-id="run-overlap"]')
    expect(no_log.get_by_role("button", expanded=True)).to_be_visible()
    expect(no_log.get_by_text("This run did not start the command, so it has no log.", exact=True)).to_be_visible()
    expect(no_log.get_by_text("Duration", exact=True).locator("..")).to_contain_text("—")
    expect(no_log.get_by_label("Run log", exact=True)).to_have_count(0)
    expect(no_log.get_by_role("button", name="Refresh log", exact=True)).to_have_count(0)
    expect(no_log.get_by_role("button", name="Download log", exact=True)).to_have_count(0)
    assert not any(path == "schedule_admin/run_log" and body["run_id"] == "run-overlap"
                   for path, body in state["calls"])
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(artifacts / f"run-without-log-{width}.png"), full_page=True)

    page.evaluate("hash => { location.hash = hash; }", f"#projects/proj-example/schedules/{sid}/runs/{target}")
    failed = history.locator(f'li[data-run-id="{target}"]')
    expect(failed.get_by_role("button", expanded=True)).to_be_visible()
    with page.expect_download() as downloaded:
        failed.get_by_role("button", name="Download log", exact=True).click()
    assert downloaded.value.suggested_filename == f"{target}.log"
    assert Path(downloaded.value.path()).read_bytes() == log
    status_filter = history.get_by_role("combobox", name="Run status", exact=True)
    filter_left = status_filter.bounding_box()["x"]
    status_filter.select_option("failed")
    assert status_filter.bounding_box()["x"] == filter_left
    expect(history.get_by_role("checkbox", name="Hide routine skips", exact=False)).to_be_visible()
    expect(history.get_by_role("checkbox", name="Hide routine skips", exact=False)).to_be_disabled()
    expect(runs.get_by_text("Succeeded", exact=True)).to_have_count(0)
    assert any(path == "schedule_admin/runs" and body.get("status") == "failed"
               for path, body in state["calls"])

    state["overview"] = {"health": {"unhealthy": []},
        "tasks": {"total": 0, "in_progress": 0, "ready": 0, "attention": 0, "completed": 0,
                  "needs_you": 0, "cancelled": 0, "other": 0, "other_statuses": {}},
        "agents": {"manager": None, "planners": [], "workers": [], "reviewers": []},
        "projects": [], "idle_projects": 1,
        "spend_today": {"since": stamp, "provider_priced_usd": 0, "equivalent_usd": 0,
                        "runs": 0, "running": 0, "unpriced_runs": 0},
        "schedules": {"total": 1, "enabled": 1, "next": [],
            "failing": [{**schedule, "last_status": "failed", "failures_since": None,
                         "stale_since": None, "deadline_since": stamp, "overlap_since": stamp}],
            "skipped_today": {"skipped_gate": 0, "skipped_overlap": 1, "skipped_missed": 0, "skipped_budget": 0}}}
    page.goto(f"{base}/#dashboard")
    expect(page.get_by_role("heading", name="Dashboard", exact=True)).to_be_visible()
    expect(page.get_by_text("Missed its success deadline since", exact=False)).to_be_visible()
    expect(page.get_by_text("Skipping: the previous run is still going since", exact=False)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("region", name="Schedules", exact=True).scroll_into_view_if_needed()
    page.screenshot(path=str(artifacts / f"deadline-{width}.png"), full_page=True)

    # An overlap notification can point to a running command behind hundreds of newer skips.
    target_run = next(run for run in state["runs"] if run["run_id"] == target)
    target_run.update(status="running", finished_at=None, duration_seconds=None, exit_code=None)
    skip = next(run for run in state["runs"] if run["status"] == "skipped_overlap")
    state["runs"] = [{**skip, "run_id": f"run-newer-skip-{index}"} for index in range(220)] + state["runs"]
    state["run_page_size"] = 20
    schedule["last_run"] = state["runs"][0]
    page.goto(f"{base}/#projects/proj-example/schedules/{sid}/runs/{target}")
    expect(history.locator(f'li[data-run-id="{target}"]').get_by_role("button", expanded=True)).to_be_visible()
    expect(history.get_by_label("Run log", exact=True)).to_contain_text(log.decode().strip())
    expect(history.get_by_text("This run is no longer kept", exact=True)).to_have_count(0)
    page.screenshot(path=str(artifacts / f"older-linked-run-{width}.png"))

    page.goto(f"{base}/#projects/proj-example/schedules/{sid}/runs/run-gone")
    expect(history.get_by_text("This run is no longer kept", exact=True)).to_be_visible()
    expect(history.locator("li[data-run-id]")).to_have_count(len(state["runs"]))
    expect(history.get_by_role("button", name="Show older runs", exact=True)).to_have_count(0)


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "narrow"])
def test_command_form_create_edit_and_agent_regression(tmp_path, staged_dashboard_dir, width):
    """Exercise form serialization against a fresh database and a real serve process."""
    import os

    playwright = pytest.importorskip("playwright.sync_api", reason="UNTESTED: Playwright missing")
    expect, sync_playwright = playwright.expect, playwright.sync_playwright

    from tests.test_llm_models_dashboard import _running_dashboard

    admin_url = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("UNTESTED: disposable PostgreSQL is not configured")
    with _running_dashboard(tmp_path, admin_url) as server, sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).is_file():
            pytest.skip("UNTESTED: Chromium missing")
        browser = driver.chromium.launch(headless=True, timeout=15000)
        context = browser.new_context(viewport={"width": width, "height": 900})
        try:
            page = context.new_page()
            page.set_default_timeout(10000)

            def snapshot(name):
                form = page.locator("form").filter(has=page.get_by_label("Name *", exact=True))
                expect(form).not_to_contain_text("Checking the rule…")
                height = form.evaluate("element => element.scrollHeight") + 800
                page.set_viewport_size({"width": width, "height": height})
                form.evaluate("""element => {
                    for (let node = element; node; node = node.parentElement) node.scrollTop = 0;
                }""")
                page.locator("textarea").evaluate_all("elements => elements.forEach(element => element.scrollTop = 0)")
                page.screenshot(path=str(tmp_path / f"{name}-{width}.png"), full_page=True)
                page.set_viewport_size({"width": width, "height": 900})

            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            project = server.api.post("/api/v1/project_admin/list", {})["result"]["projects"][0]
            pid = project["project_id"]
            server.api.post("/api/v1/project_admin/update", {"project_id": pid, "fields": {
                "name": "Example", "work_dir": "/srv/example"}})
            base = server.api.base_url
            page.goto(f"{base}/#projects/{pid}/schedules", wait_until="domcontentloaded")
            page.get_by_role("button", name="New schedule", exact=True).click()
            expect(page.get_by_label("Requirement *", exact=True)).to_be_visible()
            page.get_by_role("radio", name="Command", exact=False).check()
            expect(page.get_by_label("Requirement *", exact=True)).to_have_count(0)
            expect(page.get_by_role("radio", name="Use the default", exact=False)).to_have_count(0)
            page.get_by_label("Time zone", exact=True).fill("UTC")
            page.get_by_label("Name *", exact=True).fill("Daily report")
            page.get_by_label("Rule (cron) *", exact=True).fill("* * * * *")
            command = "printf '%s\\n' " + "x" * 400
            page.get_by_label("Command *", exact=True).fill(command)
            env = page.get_by_label("Environment", exact=True)
            env.fill("# synthetic configuration\nBROKEN")
            expect(page.locator("form").get_by_role("alert")).to_contain_text("Line 2 is not KEY=VALUE")
            expect(page.get_by_role("button", name="Create", exact=True)).to_be_disabled()
            env.fill("REPORT=old\nREPORT=daily=summary\nZONE=west")
            expect(page.get_by_text("the last value is used.", exact=False)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            snapshot("command-create")
            page.get_by_text("Alerts", exact=True).click()
            page.get_by_label("Success deadline", exact=True).fill("08:00")
            page.get_by_label("Alert after N failures in a row", exact=True).fill("5")
            page.get_by_label("Alert after no success for (minutes)", exact=True).fill("5.5")
            page.get_by_label("Alert after N skipped firings in a row", exact=True).fill("0")
            env_file = page.get_by_label("Env file", exact=True)
            env_file.fill(str(tmp_path / "missing.env"))
            with page.expect_response(lambda r: r.url.endswith("/schedule_admin/create")) as refused:
                page.get_by_role("button", name="Create", exact=True).click()
            assert refused.value.status == 400
            expect(env_file.locator("..").get_by_role("alert")).to_contain_text("env_file")
            expect(env_file.locator("xpath=ancestor::form").get_by_role("alert")).to_have_count(2)
            env_file.fill("")
            with page.expect_response(lambda r: r.url.endswith("/schedule_admin/create")) as response:
                page.get_by_role("button", name="Create", exact=True).click()
            assert response.value.status == 200, response.value.text()
            fields = response.value.request.post_data_json["fields"]
            assert fields["kind"] == "command" and fields["run_timeout_seconds"] is None
            assert "requirement" not in fields and "profile_overrides" not in fields
            saved = response.value.json()["result"]["schedule"]
            assert saved["env"] == {"REPORT": "daily=summary", "ZONE": "west"}
            assert saved["alert_policy"]["stale_after_seconds"] == {
                "value": 330, "default": False, "source": "schedule"}
            sid = saved["schedule_id"]
            expect(page.get_by_text("Type: Command", exact=True)).to_be_visible()
            expect(page.get_by_role("radio", name="Agent task", exact=False)).to_have_count(0)
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_label("Command *", exact=True)).to_have_value(command)
            expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
            assert page.locator("details").filter(has=page.get_by_text("Alerts", exact=True)).evaluate("element => element.open")
            expect(page.get_by_text("Ownership cannot be changed.", exact=False)).to_have_count(0)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            snapshot("command-edit")
            page.get_by_label("Command *", exact=True).fill("printf updated")
            with page.expect_response(lambda r: r.url.endswith("/schedule_admin/update")) as response:
                page.get_by_role("button", name="Save", exact=True).click()
            assert response.value.status == 200
            assert response.value.request.post_data_json["fields"] == {"command": "printf updated"}
            if width == 1280:
                server.api.post("/api/v1/schedule_admin/update", {
                    "schedule_id": sid, "fields": {"work_dir": str(tmp_path / "work")}})
                run = server.api.post("/api/v1/schedule_admin/run", {"schedule_id": sid})["result"]["run"]
                rid = run["run_id"]
                page.goto(f"{base}/#projects/{pid}/schedules/{sid}/runs/{rid}", wait_until="domcontentloaded")
                row = page.locator("li[data-run-id]").filter(has=page.get_by_role("button", expanded=True))
                expect(row.get_by_text("Succeeded", exact=True)).to_be_visible(timeout=35000)
                expect(row.get_by_text("exit 0", exact=True)).to_be_visible()
                row.get_by_role("button", name="Refresh log", exact=True).click()
                expect(row.get_by_label("Run log", exact=True)).to_have_text("updated")
                with page.expect_download() as downloaded:
                    row.get_by_role("button", name="Download log", exact=True).click()
                assert downloaded.value.suggested_filename == f"{rid}.log"
                assert Path(downloaded.value.path()).read_bytes() == b"updated"
                assert server.api.post("/api/v1/schedule_admin/run_log", {"run_id": rid})["result"]["complete"]
            page.goto(f"{base}/#schedules/{sid}", wait_until="domcontentloaded")
            expect(page.get_by_text("printf updated", exact=True)).to_be_visible()
            expect(page.get_by_text("REPORT=daily=summary", exact=True)).to_be_visible()
            settings = page.locator('[aria-labelledby="schedule-settings"]')
            expect(settings.get_by_text("Success deadline: 08:00", exact=True)).to_be_visible()
            expect(settings.get_by_text("5 failures in a row", exact=True)).to_be_visible()
            expect(settings.get_by_text("No success for 330 s", exact=True)).to_be_visible()
            expect(settings.get_by_text("0 skipped firings in a row", exact=True)).to_be_visible()
            expect(page.get_by_text("Requirement", exact=True)).to_have_count(0)
            page.goto(f"{base}/#schedules", wait_until="domcontentloaded")
            expect(page.get_by_text("Command", exact=True)).to_be_visible()
            page.goto(f"{base}/#projects/{pid}/schedules", wait_until="domcontentloaded")
            page.get_by_role("button", name="New schedule", exact=True).click()
            page.get_by_label("Time zone", exact=True).fill("UTC")
            page.get_by_label("Name *", exact=True).fill("Agent report")
            page.get_by_label("Rule (cron) *", exact=True).fill("0 9 * * *")
            page.get_by_label("Requirement *", exact=True).fill("Review the daily reports.")
            expect(page.get_by_label("Command *", exact=True)).to_have_count(0)
            expect(page.get_by_label("Spend cap per run (USD)", exact=True)).to_be_visible()
            expect(page.get_by_role("radio", name="Use the default", exact=False)).to_be_checked()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            snapshot("agent-create")
            with page.expect_response(lambda r: r.url.endswith("/schedule_admin/create")) as response:
                page.get_by_role("button", name="Create", exact=True).click()
            assert response.value.status == 200, response.value.text()
            fields = response.value.request.post_data_json["fields"]
            assert fields == {"kind": "agent", "name": "Agent report", "cron": "0 9 * * *",
                              "timezone": "UTC", "requirement": "Review the daily reports.",
                              "overlap": "skip", "catch_up": "once", "run_timeout_seconds": "default",
                              "notify": "failures", "user_gates": "off"}
            assert not errors
        finally:
            context.close()
            browser.close()
