"""Real loopback proxy → disposable database → served dashboard and CLI readback.

The upstream and agent record are synthetic; this does not claim a logged-in model run.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from threading import Thread
import urllib.request

import pytest

from evals.prompt_regression import runner
from looper.agents.llmrec import ROUTES_DIR, RouteFile
from looper.agents.llmrec.control import ProxyControl
from tests.db_support import DSN
from tests.runner_support import require_disposable_database

pytestmark = [pytest.mark.installed, pytest.mark.skipif(not DSN, reason="requires disposable database")]


@pytest.fixture
def recording_server(tmp_path, monkeypatch, staged_dashboard_dir):
    require_disposable_database(str(DSN), served=False)
    runtime, home, bin_dir = (tmp_path / name for name in ("runtime", "home", "bin"))
    home.mkdir()
    bin_dir.mkdir()
    # No probe or agent subprocess can read the operator's runtime logins.
    for name in ("claude", "codex", "grok", "omp", "pi", "dsh", "cursor-agent", "agent", "ttadk", "lark-cli"):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\nimport sys\n"
                        "if '--version' in sys.argv: print('2.1.285 (synthetic runtime)'); sys.exit(0)\n"
                        "print('No runtime login in this fixture', file=sys.stderr)\nsys.exit(1)\n")
        path.chmod(0o755)
    monkeypatch.setattr(runner, "_SERVE", "import sys\nfrom looper.cli import main\n"
        "sys.exit(main(['serve', '--host', '127.0.0.1', '--port', sys.argv[1], '--runtime-root', sys.argv[2]]))\n")
    environment = {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
        "GROK_HOME": str(home / ".grok"), "CURSOR_CONFIG_DIR": str(home / ".cursor-config"),
        "CURSOR_DATA_DIR": str(home / ".cursor-data"), "LOOPER_TELEGRAM_BOT_TOKEN": ""}
    with runner.fresh_database(str(DSN)) as (url, _):
        try:
            with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                    notes_root=home / "notes", env=environment, log_path=tmp_path / "serve.log") as api:
                settings = api.post("/api/v1/llmrec_settings/update", {"enabled": True})["result"]
                assert settings["proxy"]["state"] == "running", settings
                yield api, url, runtime, settings["proxy"]["port"]
        finally:
            async def stop_proxy():
                control = ProxyControl(runtime, url, enabled=False)
                state = control._state()
                if state:
                    await control._stop(state)
            asyncio.run(stop_proxy())


@pytest.mark.parametrize("translated", [False, True])
def test_recorded_requests_viewer_and_jsonl_download(recording_server, tmp_path, translated):
    pw = pytest.importorskip("playwright.sync_api")
    api, url, runtime, port = recording_server
    agent_id = "worker-recording-example"

    async def seed_run():
        import asyncpg
        from looper.db import llm_records

        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        try:
            record = await llm_records.start_record(agent_id=agent_id, input_text="Explain the sample calculation.",
                metadata={"llmrec": {"state": "recording"}}, pool=pool)
            return record
        finally:
            await pool.close()

    record_id = asyncio.run(seed_run())

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.dumps({"role": "assistant", "content": [{"type": "text", "text": "The result is 4."}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 14, "output_tokens": 8}}).encode()
            if translated:
                payload = json.dumps({"choices": [{"message": {"content": "The result is 4."},
                    "finish_reason": "stop"}], "usage": {"prompt_tokens": 200, "completion_tokens": 8,
                    "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 20}}}).encode()
            else:
                reply = json.loads(payload)
                reply["usage"].update(input_tokens=100, cache_read_input_tokens=80, cache_creation_input_tokens=20)
                payload = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = Thread(target=upstream.serve_forever)
    thread.start()
    try:
        route = RouteFile(id=secrets.token_urlsafe(16), agent_id=agent_id, role="worker",
            upstream=f"http://127.0.0.1:{upstream.server_port}", protocol="anthropic-messages", model="example-model",
            created_at=datetime.now(timezone.utc).isoformat(), record_id=record_id)
        if translated:
            route = replace(route, kind="translate", record=True, api="openai-chat", dialect="openai",
                            models={"example-model": "upstream-model"})
        route_path = runtime / ROUTES_DIR / f"{route.id}.json"
        body = {"model": "example-model", "system": [{"type": "text", "text": "Explain calculations clearly."}],
            "tools": [{"name": "calculator", "description": "Calculator documentation " * 3000,
                       "input_schema": {"type": "object"}}], "max_tokens": 64,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "What is 2 + 2?"}]}]}
        for index in range(3):
            if index == 2:
                route = replace(route, record_id=None, last_record_id=record_id)
            route.write(route_path)
            request = urllib.request.Request(f"http://127.0.0.1:{port}/r/{route.id}/v1/messages",
                json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": "Bearer synthetic"})
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 200
            body["messages"].append({"role": "assistant" if index % 2 == 0 else "user", "content": "Continue."})
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

    async def finish_run():
        import asyncpg
        conn = await asyncpg.connect(url)
        try:
            await conn.execute("UPDATE llm_records SET status='completed', finished_at=now(), duration_ms=1000, "
                               "input_tokens=100, cache_read_tokens=80, cache_creation_tokens=20, output_tokens=8 WHERE id=$1", record_id)
        finally:
            await conn.close()
    asyncio.run(finish_run())
    artifacts = Path("output/playwright/llm-requests")
    artifacts.mkdir(parents=True, exist_ok=True)
    errors = []
    with pw.sync_playwright() as driver:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        browser = driver.chromium.launch(headless=True, **({"executable_path": str(chrome)} if chrome.is_file() else {}))
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1000}, accept_downloads=True)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{api.base_url}/#history/{agent_id}")
            section = page.get_by_role("region", name="Model requests", exact=True)
            toggle = section.get_by_role("button", name="Model requests · 3", exact=True)
            pw.expect(toggle).to_be_visible(timeout=15000)
            toggle.click()
            pw.expect(section.get_by_text("After the turn ended", exact=True)).to_be_visible()
            pw.expect(page.get_by_text("cache hit 40%", exact=True)).to_be_visible()
            pw.expect(section.get_by_text("hit 40%", exact=True)).to_have_count(3)
            pw.expect(section.get_by_text("prefix reuse 100%", exact=True)).to_have_count(2 if translated else 0)
            if translated:
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.screenshot(path=str(artifacts / "cache-hit-1440.png"), full_page=True)
            for width in (1280, 768):
                page.set_viewport_size({"width": width, "height": 1000})
                section.screenshot(path=str(artifacts / f"requests-{width}.png"))
            section.locator("button[aria-expanded]").nth(2).click()
            pw.expect(section.get_by_text("New in this request", exact=True)).to_be_visible()
            pw.expect(section.get_by_text("Explain calculations clearly.", exact=True)).to_be_visible()
            pw.expect(section.get_by_role("button", name="calculator", exact=True)).to_be_visible()
            for width in (1280, 768):
                page.set_viewport_size({"width": width, "height": 2200})
                section.screenshot(path=str(artifacts / f"viewer-{width}.png"))
            with page.expect_download() as event:
                section.get_by_role("button", name="Export JSONL", exact=True).click()
            dashboard_file = tmp_path / "dashboard.jsonl"
            event.value.save_as(dashboard_file)
            command = [sys.executable, "-m", "looper.loopercli", "llm-requests", "export", "--record", str(record_id),
                "--out", str(tmp_path / "run.jsonl"), "--operator-token-file", str(runtime / "operator-token"), "--url", api.base_url]
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stderr
            cli_lines = (tmp_path / "run.jsonl").read_text().splitlines()
            web_lines = dashboard_file.read_text().splitlines()
            assert len(cli_lines) == len(web_lines) == 4
            cli_rows, web_rows = [json.loads(line) for line in cli_lines], [json.loads(line) for line in web_lines]
            assert cli_rows[0]["type"] == web_rows[0]["type"] == "llmrec_export"
            assert cli_rows[1:] == web_rows[1:]
            assert cli_rows[2]["new_from_index"] == 1
            assert cli_rows[1]["request_headers"]["authorization"] == "<redacted>"
            print("JSONL first 600 bytes:", (tmp_path / "run.jsonl").read_text()[:600])
            print(f"CLI: {len(cli_lines)} lines; dashboard: {len(web_lines)} lines; every line parses")
            assert os.stat(tmp_path / "run.jsonl").st_mode & 0o777 == 0o600
            page.goto(f"{api.base_url}/#settings/recording")
            switch = page.get_by_role("switch", name="Record model requests", exact=True)
            pw.expect(switch).to_have_attribute("aria-checked", "true")
            switch.click()
            pw.expect(switch).to_have_attribute("aria-checked", "false")
            switch.click()
            pw.expect(page.get_by_role("group", name="Confirm request recording")).to_be_visible()
            for width in (1280, 768):
                page.set_viewport_size({"width": width, "height": 1000})
                page.screenshot(path=str(artifacts / f"settings-confirm-{width}.png"), full_page=True)
            page.get_by_role("button", name="Cancel", exact=True).click()
            assert not errors
        finally:
            browser.close()
