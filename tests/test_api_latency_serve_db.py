"""Latency diagnostics on the public serve path, using an isolated runtime."""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from zoneinfo import TZPATH

import pytest

from evals.prompt_regression import runner
from tests.db_support import DSN, fresh_database_without_recording
from tests.runner_support import require_disposable_database

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# Instrument only the child test process. No debug operation ships in the server.
_SERVE_WITH_STALLS = '''
import sys, time
from looper.cli import main
from looper.agents import http_server, operator_auth
original_answer = http_server.AgentHTTPServer._answer_v1
original_token = operator_auth.ensure_token

def blocked_startup(*args, **kwargs):
    time.sleep(1.5)
    return original_token(*args, **kwargs)

async def blocked_request(self, method, path, operation, *args, **kwargs):
    if operation is not None and operation.operation_id == "whoami":
        time.sleep(2.05)
    return await original_answer(self, method, path, operation, *args, **kwargs)

operator_auth.ensure_token = blocked_startup
http_server.AgentHTTPServer._answer_v1 = blocked_request
http_server.API_SUMMARY_SECONDS = 0.5
sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], "--runtime-root", sys.argv[2]]))
'''


def test_serve_logs_startup_and_request_stacks_recovery_and_slow_op(tmp_path, monkeypatch):
    require_disposable_database(str(DSN), served=False)
    monkeypatch.setattr(runner, "_SERVE", _SERVE_WITH_STALLS)
    runtime, home, bin_dir = (tmp_path / name for name in ("runtime", "home", "bin"))
    home.mkdir()
    bin_dir.mkdir()
    # Only UTC is needed here; scanning the host's zone database can exhaust
    # the readiness deadline before the latency checks run.
    zones = tmp_path / "zoneinfo"
    zones.mkdir()
    utc = next(Path(root) / "UTC" for root in TZPATH if (Path(root) / "UTC").is_file())
    shutil.copyfile(utc, zones / "UTC")
    script = (f"#!{sys.executable}\nimport sys\n"
              "if '--version' in sys.argv:\n    print('2.1.285 (test runtime)'); sys.exit(0)\n"
              "if sys.argv[1:] == ['auth', '--help']:\n    print('Usage: claude auth status'); sys.exit(0)\n"
              "if sys.argv[1:3] == ['auth', 'status']:\n    print('{\\\"loggedIn\\\": false}'); sys.exit(1)\n"
              "sys.exit(76)\n")
    for name in ("claude", "codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "lark-cli", "coco"):
        executable = bin_dir / name
        executable.write_text(script)
        executable.chmod(0o755)
    environment = {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
                   "GROK_HOME": str(home / ".grok"), "CURSOR_CONFIG_DIR": str(home / ".cursor-config"),
                   "CURSOR_DATA_DIR": str(home / ".cursor-data"), "XDG_CONFIG_HOME": str(home / ".config"),
                   "XDG_CACHE_HOME": str(home / ".cache"), "LOOPER_TELEGRAM_BOT_TOKEN": "",
                   "TZ": "UTC", "PYTHONTZPATH": str(zones)}
    log_path = tmp_path / "serve.log"
    with fresh_database_without_recording(str(DSN)) as (url, _):
        require_disposable_database(url, served=True)
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                                   notes_root=home / "notes", env=environment, startup_timeout=30,
                                   log_path=log_path) as api:
            assert api.request("GET", "/api/v1/whoami")["status"] == "ok"
            # Let the independent observer record recovery after the response.
            time.sleep(0.6)
            logs = log_path.read_text()
            assert "blocked_startup" in logs
            assert "blocked_request" in logs
            assert logs.count("event loop was blocked for") >= 2
            assert "api slow client=operator op=whoami status=ok" in logs
            assert "api summary window=600s op=whoami n=1" in logs
            assert "over_2s=1" in logs
            assert "loop summary window=600s stalls=" in logs
        assert "agent system stopped" in log_path.read_text()
