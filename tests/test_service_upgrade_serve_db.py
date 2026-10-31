"""Real HTTP/serve verification of dashboard upgrades and control-plane restarts.

Git and npm run for real in a disposable source checkout. Wheel package
installation is replaced to retain the test interpreter.
The zero-dependency dashboard fixture tests installation and HTTP delivery,
without downloading or rebuilding the product's full frontend.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

from evals.prompt_regression import runner
from looper import components
from looper.agents import service
from tests.db_support import DSN
from tests.runner_support import require_disposable_database

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "-C", str(root), *args],
        capture_output=True, text=True, check=True, timeout=20,
    ).stdout.strip()


def _commit(root: Path, name: str, *paths: str) -> str:
    _git(root, "add", "--", *paths)
    _git(root, "commit", "-q", "-m", name)
    return _git(root, "rev-parse", "HEAD")


def _source(root: Path) -> tuple[str, list[tuple[str, str, str]]]:
    root.mkdir()
    for name in ("looper", "runners"):
        shutil.copytree(ROOT / name, root / name, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"))
    shutil.copyfile(ROOT / "pyproject.toml", root / "pyproject.toml")
    (root / ".gitignore").write_text("web/dist/\n**/__pycache__/\n.DS_Store\n*.pyc\n")
    web = root / "web"
    (web / "src").mkdir(parents=True)
    (web / "src/label.txt").write_text("initial dashboard")
    (web / "index.html").write_text("<html><body>LABEL</body></html>\n")
    (web / "package.json").write_text(json.dumps({
        "name": "upgrade-dashboard-fixture", "version": "1.0.0", "private": True,
        "scripts": {"build": "node build.mjs"},
    }))
    (web / "package-lock.json").write_text(json.dumps({
        "name": "upgrade-dashboard-fixture", "version": "1.0.0", "lockfileVersion": 3,
        "requires": True, "packages": {"": {"name": "upgrade-dashboard-fixture", "version": "1.0.0"}},
    }))
    (web / "build.mjs").write_text(
        "import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';\n"
        "import {resolve} from 'node:path';\n"
        "import {execFileSync} from 'node:child_process';\n"
        "execFileSync(process.env.PYTHON ?? 'python3', ['-c', 'import tomllib']);\n"
        "const out = resolve(process.argv[process.argv.indexOf('--outDir') + 1]);\n"
        "mkdirSync(out, {recursive: true});\n"
        "writeFileSync(resolve(out, 'index.html'), readFileSync('index.html', 'utf8')"
        ".replace('LABEL', readFileSync('src/label.txt', 'utf8')));\n"
    )
    (root / "docs").mkdir()
    (root / "docs/upgrade.md").write_text("initial documentation\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Upgrade fixture")
    _git(root, "config", "user.email", "upgrade@example.invalid")
    base = _commit(root, "initial source", "looper", "runners", "pyproject.toml", "web", "docs", ".gitignore")
    (root / "docs/upgrade.md").write_text("updated documentation\n")
    docs = _commit(root, "documentation only", "docs/upgrade.md")
    targets = [(docs, "NOOP", "initial dashboard")]
    for label in ("first dashboard update", "second dashboard update"):
        (web / "src/label.txt").write_text(label)
        target = _commit(root, label, "web/src/label.txt")
        targets.append((target, "WEB_ONLY", label))
    _git(root, "checkout", "-q", "--detach", base)
    subprocess.run(["node", "build.mjs", "--outDir", str(web / "dist")],
                   cwd=web, check=True, capture_output=True, text=True, timeout=20,
                   env={**os.environ, "PYTHON": sys.executable})
    components.record_web_source(root, web / "dist")
    return base, targets


def test_docs_and_two_dashboard_upgrades_keep_real_serve_pid_and_refresh_components(tmp_path, monkeypatch):
    require_disposable_database(str(DSN), served=False)
    if not shutil.which("npm") or not shutil.which("node"):
        pytest.skip("npm and node are required for the real dashboard install path")
    source = tmp_path / "source"
    base, targets = _source(source)
    runtime, home, bin_dir = (tmp_path / name for name in ("runtime", "home", "bin"))
    for directory in (runtime, home, bin_dir):
        directory.mkdir()
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\nprintf '2.0.0\\n'\n")
    claude.chmod(0o755)
    old_python = bin_dir / "python3"
    old_python.write_text("#!/bin/sh\nprintf 'old Python has no tomllib\\n' >&2\nexit 1\n")
    old_python.chmod(0o755)
    # Keep running_server's actual serve startup/cleanup, with a pid readback.
    monkeypatch.setattr(runner, "_SERVE", (
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['UPGRADE_FIXTURE_PID']).write_text(str(os.getpid()))\n" + runner._SERVE
    ))
    pid_file = tmp_path / "serve.pid"
    env = {"PYTHONPATH": os.pathsep.join(filter(None, (str(source), os.environ.get("PYTHONPATH")))),
           "PYTEST_LOOPER_STATIC_DIR": str(source / "web/dist"),
           "UPGRADE_FIXTURE_PID": str(pid_file)}
    installed_commands: list[list[str]] = []

    def run(argv):
        argv = list(argv)
        installed_commands.append(argv)
        if argv[:3] == [sys.executable, "-m", "pip"]:
            if "install" in argv:
                assert "--no-deps" in argv
                with zipfile.ZipFile(argv[-1]) as archive:
                    assert "looper/cli.py" in archive.namelist()
                    assert not any(name.startswith("looper/web/dashboard/") for name in archive.namelist())
            else:
                assert argv[-1] == "--version"
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert Path(argv[0]).name in ("git", "npm", "uv", "env")
        if Path(argv[0]).name == "uv":
            pytest.fail("NOOP and WEB_ONLY must not sync the live service's dependencies")
        return subprocess.run(argv, cwd=source, text=True, capture_output=True, timeout=30,
                              env={**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                                   "npm_config_cache": str(tmp_path / "npm-cache")})

    def forbidden(*args, **kwargs):
        pytest.fail("NOOP and WEB_ONLY must not stop, start, restart or drain the service")

    with runner.fresh_database(str(DSN)) as (database_url, _):
        require_disposable_database(database_url, served=True)
        with runner.running_server(database_url=database_url, runtime_root=runtime, home=home,
                                   bin_dir=bin_dir, log_path=tmp_path / "serve.log",
                                   notes_root=home / "notes", env=env) as api:
            pid = int(pid_file.read_text())
            port = urlsplit(api.base_url).port
            assert port != 8767
            unit = tmp_path / "looper.service"
            unit.write_text("isolated service manager fixture\n")
            state = service.State(installed=True, loaded=True, running=True, pid=pid, port=port,
                                  log_path=tmp_path / "serve.log", runtime_root=runtime)
            manager = SimpleNamespace(
                unit_path=unit, state=lambda: state,
                unit_settings=lambda **kwargs: (port, state.log_path, runtime),
                unit_argv=lambda: service.looper_command(sys.executable) + ["serve"],
                start=forbidden, stop=forbidden, restart=forbidden,
            )
            monkeypatch.setattr(service, "manager", lambda host, label: manager)
            monkeypatch.setattr(service, "_unit_package_root", lambda host, mgr: source.resolve())
            monkeypatch.setattr(service, "__file__", str(source / "looper/agents/service.py"))
            host = service.Host(system="Linux", home=home, uid=os.getuid(), environ={},
                                executable=sys.executable, cwd=source, package_root=source, run=run)
            host.system_operation = forbidden
            initial = api.post("/api/v1/system/components", {})["result"]
            assert initial["components"]["web"] == components.manifest_from_git(source, base).components["web"]
            previous_web = initial["components"]["web"]
            previous_assets = initial["verification"]["web"]

            for target, expected_plan, expected_text in targets:
                preview = service.upgrade(host, to=target, dry_run=True)
                assert f"plan       {expected_plan}" in preview
                result = service.upgrade(host, to=target)
                assert f"plan       {expected_plan}" in result
                report = json.loads(result.splitlines()[-1])
                assert report["plan"] == expected_plan
                assert report["drain_seconds"] == report["gap_seconds"] == 0
                assert report["interrupted"] == []
                assert _git(source, "rev-parse", "HEAD") == target
                os.kill(pid, 0)
                assert int(pid_file.read_text()) == pid
                current = api.post("/api/v1/system/components", {})["result"]
                assert current["loaded_at"] == initial["loaded_at"]
                assert current["components"]["control_plane"] == initial["components"]["control_plane"]
                assert current["components"]["web"] == components.manifest_from_git(source, target).components["web"]
                with urlopen(Request(api.base_url + "/", headers={"Cookie": f"looper_operator_{port}={api.token}"}),
                             timeout=5) as response:
                    assert response.status == 200
                    assert expected_text in response.read().decode()
                if expected_plan == "WEB_ONLY":
                    assert current["components"]["web"] != previous_web
                    assert current["verification"]["web"] != previous_assets
                else:
                    assert current["components"]["web"] == previous_web
                    assert current["verification"]["web"] == previous_assets
                previous_web, previous_assets = current["components"]["web"], current["verification"]["web"]
                assert "plan       NOOP" in service.upgrade(host, to=target, dry_run=True)

            # Exercise WEB_ONLY, then repeat the same wheel as NOOP on the live process.
            # Package the unchanged backend and runner aliases with a new dashboard.
            wheel = tmp_path / "dashboard-update.whl"
            aliases = tomllib.loads((source / "looper/components.toml").read_text())["aliases"]
            with zipfile.ZipFile(wheel, "w") as archive:
                for path in (source / "looper").rglob("*"):
                    relative = path.relative_to(source).as_posix()
                    if path.is_file() and "__pycache__" not in path.parts and not relative.startswith("looper/web/dashboard/"):
                        archive.write(path, relative)
                for alias, prefix in aliases.items():
                    for path in (source / prefix).rglob("*"):
                        if path.is_file() and "__pycache__" not in path.parts:
                            archive.write(path, alias + path.relative_to(source / prefix).as_posix())
                archive.writestr(components.RUNTIME_RECORD, components.runtime_fingerprint(source))
                archive.writestr("looper/web/dashboard/index.html", "wheel dashboard update")
                archive.writestr("looper/web/dashboard/assets/app.js", "wheel dashboard script")
                archive.writestr("looper/web/dashboard/" + components.WEB_SOURCE_RECORD, "e" * 64)
            monkeypatch.setenv("PYTEST_LOOPER_STATIC_DIR", str(source / "web/dist"))
            for expected_plan in ("WEB_ONLY", "NOOP"):
                assert f"plan       {expected_plan}" in service.upgrade(host, to=str(wheel), dry_run=True)
                result = service.upgrade(host, to=str(wheel))
                deployment = json.loads(result.splitlines()[-1])
                assert deployment["plan"] == expected_plan
                assert deployment["installed_package_version"] == initial["version"]
                current = api.post("/api/v1/system/components", {})["result"]
                assert current["loaded_at"] == initial["loaded_at"]
                assert current["components"] == components.manifest_from_wheel(wheel).components
                assert current["verification"] == components.manifest_from_wheel(wheel).verification
                assert int(pid_file.read_text()) == pid
                for path, content in (("/", b"wheel dashboard update"), ("/assets/app.js", b"wheel dashboard script")):
                    with urlopen(Request(api.base_url + path, headers={"Cookie": f"looper_operator_{port}={api.token}"}),
                                 timeout=5) as response:
                        assert response.status == 200 and response.read() == content
                assert _git(source, "rev-parse", "HEAD") == target

    assert not any(Path(argv[0]).name == "uv" for argv in installed_commands)
    assert sum("npm" in argv and "build" in argv for argv in installed_commands) == 2
    assert sum("install" in argv and "--no-deps" in argv for argv in installed_commands) == 2


def test_ignored_web_files_allow_dashboard_and_full_upgrade_on_real_serve(tmp_path, monkeypatch):
    require_disposable_database(str(DSN), served=False)
    if not shutil.which("npm") or not shutil.which("node"):
        pytest.skip("npm and node are required for the real dashboard install path")
    source = tmp_path / "source"
    base, targets = _source(source)
    _git(source, "checkout", "-q", "--detach", targets[-1][0])
    backend = source / "looper/agents/proc_tree.py"
    backend.write_text(backend.read_text() + "\n# Updated process supervision implementation.\n")
    (source / "web/src/label.txt").write_text("full dashboard update")
    full = _commit(source, "process supervision and dashboard update", "looper/agents/proc_tree.py",
                   "web/src/label.txt")
    _git(source, "checkout", "-q", "--detach", base)
    ignored = (source / "web/src/.DS_Store", source / "web/src/x.pyc")
    for path in ignored:
        path.write_bytes(b"ignored local file")
    assert _git(source, "ls-files", "--others", "--exclude-standard") == ""
    runtime, home, bin_dir = (tmp_path / name for name in ("runtime", "home", "bin"))
    for directory in (runtime, home, bin_dir):
        directory.mkdir()
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\nprintf '2.0.0\\n'\n")
    claude.chmod(0o755)
    pid_file = tmp_path / "serve.pid"
    monkeypatch.setattr(runner, "_SERVE", (
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['UPGRADE_FIXTURE_PID']).write_text(str(os.getpid()))\n" + runner._SERVE
    ))
    env = {"PYTHONPATH": os.pathsep.join(filter(None, (str(source), os.environ.get("PYTHONPATH")))),
           "PYTEST_LOOPER_STATIC_DIR": str(source / "web/dist"), "UPGRADE_FIXTURE_PID": str(pid_file)}
    installed_commands = []

    def run(argv, **kwargs):
        argv = list(argv)
        installed_commands.append(argv)
        if argv[:3] == [sys.executable, "-I", "-c"]:
            return subprocess.run(argv, text=True, capture_output=True, timeout=10)
        if argv[:2] == ["uv", "sync"]:
            assert kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(Path(sys.prefix).resolve())
            assert argv[argv.index("--python") + 1] == sys.executable
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert Path(argv[0]).name in ("git", "npm", "env")
        return subprocess.run(argv, cwd=source, text=True, capture_output=True, timeout=30,
                              env={**os.environ, "HOME": str(home),
                                   "npm_config_cache": str(tmp_path / "npm-cache")})

    with runner.fresh_database(str(DSN)) as (database_url, _), ExitStack() as servers:
        require_disposable_database(database_url, served=True)

        def start():
            api = servers.enter_context(runner.running_server(
                database_url=database_url, runtime_root=runtime, home=home, bin_dir=bin_dir,
                log_path=tmp_path / "serve.log", notes_root=home / "notes", env=env))
            state.running, state.pid = True, int(pid_file.read_text())
            return api

        state = service.State(installed=True, loaded=True, running=False,
                              log_path=tmp_path / "serve.log", runtime_root=runtime)
        api = start()
        port = urlsplit(api.base_url).port
        assert port != 8767
        state.port = port
        monkeypatch.setattr(runner, "free_port", lambda: port)

        def stop():
            servers.close()
            state.running, state.pid = False, None

        unit = tmp_path / "looper.service"
        unit.write_text("isolated service manager fixture\n")
        manager = SimpleNamespace(unit_path=unit, state=lambda: state,
                                  unit_settings=lambda **kwargs: (port, state.log_path, runtime),
                                  unit_argv=lambda: service.looper_command(sys.executable) + ["serve"],
                                  start=start, stop=stop)
        host = service.Host(system="Linux", home=home, uid=os.getuid(),
                            environ={"UV_PROJECT_ENVIRONMENT": sys.prefix},
                            executable=sys.executable, cwd=source, package_root=source, run=run)
        manager.host = host
        monkeypatch.setattr(service, "manager", lambda host, label: manager)
        monkeypatch.setattr(service, "_unit_package_root", lambda host, mgr: source.resolve())
        monkeypatch.setattr(service, "__file__", str(source / "looper/agents/service.py"))
        for target, kind, label in ((targets[1][0], "WEB_ONLY", targets[1][2]),
                                    (full, "FULL", "full dashboard update")):
            previous_pid = state.pid
            assert f"plan       {kind}" in service.upgrade(host, to=target, dry_run=True)
            result = service.upgrade(host, to=target, timeout=30, drain_timeout=10)
            report = json.loads(result.splitlines()[-1])
            assert report["status"] == "ok" and report["serving"] is True
            assert report["plan"] == kind
            actual = api.post("/api/v1/system/components", {})["result"]
            assert actual["components"] == components.manifest_from_git(source, target).components
            assert all(path.read_bytes() == b"ignored local file" for path in ignored)
            assert (state.pid == previous_pid) is (kind == "WEB_ONLY")
            with urlopen(Request(api.base_url + "/", headers={"Cookie": f"looper_operator_{port}={api.token}"}),
                         timeout=5) as response:
                assert response.status == 200 and label in response.read().decode()

    assert sum(argv[:2] == ["uv", "sync"] for argv in installed_commands) == 1
