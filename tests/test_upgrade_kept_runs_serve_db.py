"""A real launchd upgrade preserves a running CLI child on disposable PostgreSQL."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

import pytest

from evals.prompt_regression.runner import Api, free_port, fresh_database
from looper import ops_config
from looper.agents import keeper, operator_auth, service
from tests.db_support import DSN
from tests.runner_support import require_disposable_database
from tests.test_drain_serve_db import _completed, _events, _project, _task, _until
from tests.test_kept_run_serve_db import CLI, _live, _running
from tests.test_service_upgrade_serve_db import _commit, _git

pytestmark = [
    pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set"),
    pytest.mark.skipif(sys.platform != "darwin", reason="launchd verification requires macOS"),
]
ROOT = Path(__file__).resolve().parents[1]


def _source(root: Path) -> tuple[str, str, str]:
    root.mkdir()
    for name in ("looper", "runners"):
        shutil.copytree(ROOT / name, root / name, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"))
    for name in ("pyproject.toml", "README.md", "LICENSE", "LICENSE-MIT", "NOTICE"):
        shutil.copyfile(ROOT / name, root / name)
    (root / ".gitignore").write_text(".venv/\n**/__pycache__/\n*.egg-info/\nuv.lock\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Upgrade fixture")
    _git(root, "config", "user.email", "upgrade@example.invalid")
    base = _commit(root, "initial source", "looper", "runners", "pyproject.toml", "README.md",
                   "LICENSE", "LICENSE-MIT", "NOTICE", ".gitignore")
    path = root / "looper/agents/http_server.py"
    path.write_text(path.read_text() + "\n# Control-plane upgrade fixture.\n")
    target = _commit(root, "control-plane update", "looper/agents/http_server.py")
    path = root / "looper/agents/system_prompt.py"
    path.write_text(path.read_text().replace('WORK_CONTRACT = """\\\n',
        'WORK_CONTRACT = """\\\nUpdated work contract.\n', 1))
    incompatible = _commit(root, "work contract update", "looper/agents/system_prompt.py")
    _git(root, "checkout", "-q", "--detach", base)
    return base, target, incompatible


def test_launchd_upgrade_keeps_the_child_and_reports_adoption():
    require_disposable_database(str(DSN), served=False)
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is required for checkout upgrades")
    probe = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}"],
                           capture_output=True, text=True, timeout=10)
    if probe.returncode:
        pytest.skip("launchd GUI domain is unavailable")
    # Runtime socket names must fit macOS's short Unix socket path limit.
    with tempfile.TemporaryDirectory(prefix="lu.", dir="/tmp") as temporary:
        root = Path(temporary)
        source, runtime, home, bin_dir, state = (root / name for name in ("src", "r", "home", "bin", "state"))
        _, target, incompatible = _source(source)
        for directory in (runtime, home, bin_dir, state):
            directory.mkdir()
        script = root / "fake.py"
        script.write_text(CLI)
        claude = bin_dir / "claude"
        claude.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "
                          f"{shlex.quote(str(state))} \"$@\"\n")
        claude.chmod(0o755)
        env = {"HOME": str(home), "PATH": os.pathsep.join((str(bin_dir), str(Path(uv).parent), os.defpath)),
               "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv")),
               "UV_OFFLINE": "true", "UV_PROJECT_ENVIRONMENT": str(source / ".venv"),
               "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
               "GROK_HOME": str(home / ".grok")}
        installed = subprocess.run([uv, "sync", "--offline", "--project", str(source), "--python", sys.executable,
                                    "--extra", "test", "--inexact"],
                                   capture_output=True, text=True, env=env, timeout=120)
        assert installed.returncode == 0, installed.stderr
        python = source / ".venv/bin/python"
        label = "com.looper.test-upgrade-" + uuid.uuid4().hex[:12]

        def command(operation, *args, timeout=120, check=True):
            result = subprocess.run([str(python), "-m", "looper.cli", "service", operation,
                                     "--label", label, *args], cwd=source, env=env,
                                    text=True, capture_output=True, timeout=timeout)
            if check:
                assert result.returncode == 0, result.stdout + result.stderr
            return result

        port = free_port()
        assert port != 8767
        pids = set()
        with fresh_database(str(DSN)) as (url, _):
            require_disposable_database(url, served=True)
            ops = ops_config.OpsConfig(runtime_root=runtime, path=runtime / ops_config.FILE,
                database_url=url, host="127.0.0.1", port=port,
                operator_notes_root=str(home / "notes"), keeper_mode=ops_config.KeeperMode.ON)
            ops_config.write_private(ops.path, ops_config.render(ops))
            try:
                command("install", "--runtime-root", str(runtime), "--config", str(ops.path),
                        "--port", str(port), "--env", "HOME", "--timeout", "45", timeout=70)
                api = Api(f"http://127.0.0.1:{port}", token=operator_auth.read_token(runtime))
                host = service.Host(system="Darwin", home=home, uid=os.getuid(), environ=env,
                    executable=str(python), cwd=source, package_root=source, run=service._run)
                mgr = service.Launchd(host, label)
                before_pid = mgr.state().pid
                assert before_pid
                pids.add(before_pid)
                task = _task(api, _project(api, root, "upgrade"), "held")
                _until(lambda: _events(state, "worker", "held"), "the CLI Worker reading its prompt")
                directory, before = next(iter(_running(runtime).values()))
                pids.update((before["keeper_pid"], before["child_pid"]))
                preview = command("upgrade", "--to", target, "--dry-run").stdout
                assert "plan       CONTROL_PLANE" in preview and "keepers    on (1 live)" in preview
                output = command("upgrade", "--to", target, "--turns-timeout", "10", "--drain-timeout", "30",
                                 "--timeout", "45", timeout=110).stdout
                report = json.loads(output.splitlines()[-1])
                assert report["status"] == "ok" and report["plan"] == "CONTROL_PLANE", report
                assert report["kept_before"] == report["adopted"] == 1, report
                assert report["interrupted"] == [] and not report.get("alerts"), report
                after_pid = mgr.state().pid
                pids.add(after_pid)
                assert after_pid and before_pid != after_pid
                after = keeper.read_json(directory / keeper.STATE)
                assert after["child_pid"] == before["child_pid"] and _live(before["child_pid"])
                status = api.post("/api/v1/system/status", {})["result"]["keepers"]
                assert status["adopted_at_start"] == 1 and status["pending"] == 0, status
                preview = command("upgrade", "--to", incompatible, "--dry-run").stdout
                assert "plan       FULL" in preview and "adoption" in preview.lower(), preview
                (state / "release-s1").touch()
                _completed(api, task)
                print("service upgrade:", json.dumps(report, sort_keys=True))
                print("same child pid:", before["child_pid"])
                print("system.status.keepers:", json.dumps(status, sort_keys=True))
            except BaseException:
                log = runtime / service.LOG_RELATIVE_PATH
                if log.exists():
                    print(log.read_text()[-12000:])
                raise
            finally:
                cleanup = command("uninstall", check=False, timeout=60)
                assert cleanup.returncode == 0, cleanup.stdout + cleanup.stderr

                async def end_runs():
                    from looper.agents.kept_dirs import KeeperRecord, end_keeper
                    for directory in (runtime / keeper.RUNS_DIR).glob("*"):
                        record = KeeperRecord.read(directory / keeper.STATE)
                        if record:
                            await end_keeper(record, directory)
                asyncio.run(end_runs())
                _until(lambda: all(not _live(pid) for pid in pids if pid), "all test-owned processes stopping")
                assert not (home / "Library/LaunchAgents" / f"{label}.plist").exists()
                unloaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                    capture_output=True, text=True, timeout=10)
                assert unloaded.returncode != 0
