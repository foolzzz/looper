#!/usr/bin/env python3
"""A release wheel runs `looper serve`: it carries the schema and the dashboard.

Builds the wheel from this checkout, checks its contents, installs it into a
fresh virtualenv and resolves both files from the installed package (not the
checkout), and checks that it installs the ``looper`` command (the retired ``loopx``
alias is gone). With LOOPER_TEST_DATABASE_URL pointing at a disposable database it
also starts the installed `looper serve` and fetches the dashboard and the API.
Run `scripts/stage-dashboard.sh` first; the staged dashboard is not in Git.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("looper/db/schema.sql", "looper/web/dashboard/index.html")


def run(*cmd: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve_check(venv: Path, work: Path, dsn: str) -> None:
    port = free_port()
    env = {**os.environ, "LOOPER_DATABASE_URL": dsn}
    for name in ("LARK_APP_ID", "TELEGRAM_BOT_TOKEN"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [str(venv / "bin" / "looper"), "serve", "--port", str(port), "--runtime-root", str(work / "rt")],
        cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        token_file = work / "rt" / "operator-token"
        for _ in range(120):
            try:
                # /api/ needs the operator token, which serve writes into the runtime root on its first start.
                auth = {"Authorization": f"Bearer {token_file.read_text().strip()}",
                        "Content-Type": "application/json"}
                request = urllib.request.Request(f"{base}/api/v1/system/status", headers=auth,
                                                 method="POST", data=b"{}")
                with urllib.request.urlopen(request, timeout=2) as response:
                    status = json.load(response)
                assert status["status"] == "ok" and status["result"]["status"] == "running", status
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise SystemExit("installed looper serve did not come up")
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}/api/v1/project_admin/list",
                                                         method="POST", data=b"{}"), timeout=5)
            raise SystemExit("installed looper serve answered /api/ without the operator token")
        except urllib.error.HTTPError as error:
            assert error.code == 401, error.code
        # A local browser's top-level navigation signs in by itself.
        navigation = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        page = urllib.request.urlopen(urllib.request.Request(f"{base}/", headers=navigation), timeout=5).read().decode()
        assert "<div id=\"root\">" in page, page[:200]
        request = urllib.request.Request(f"{base}/api/v1/project_admin/list", headers=auth,
                                         method="POST", data=b"{}")
        with urllib.request.urlopen(request, timeout=5) as response:
            projects = json.load(response)
        assert projects["status"] == "ok" and isinstance(projects["result"]["projects"], list), projects
        print("PASS installed looper serve answers the dashboard and the API, and refuses /api/ without the token")
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def main() -> int:
    if not (ROOT / "looper/web/dashboard/index.html").is_file():
        print("looper/web/dashboard is not staged; run scripts/stage-dashboard.sh first", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        # Build from a clean copy: a checkout's build/ directory can carry
        # files from an earlier build into the wheel and hide a regression.
        src = work / "src"
        for name in run("git", "ls-files", "-z", cwd=ROOT).split("\0"):
            if name and (ROOT / name).is_file():
                (src / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, src / name)
        shutil.copytree(ROOT / "looper/web/dashboard", src / "looper/web/dashboard")
        run("uv", "build", "--wheel", "--out-dir", str(work / "dist"), cwd=src)
        (wheel,) = (work / "dist").glob("looper-*.whl")
        names = set(zipfile.ZipFile(wheel).namelist())
        missing = [n for n in REQUIRED if n not in names]
        assert not missing, f"wheel lacks {missing}"
        assert any(n.startswith("looper/web/dashboard/assets/") for n in names), "wheel lacks dashboard assets"
        print(f"PASS {wheel.name} carries the schema and the dashboard")

        venv = work / "venv"
        run("uv", "venv", "--python", sys.executable, str(venv), cwd=work)
        run("uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel), cwd=work)
        located = run(
            str(venv / "bin" / "python"), "-c",
            "from looper.agents.http_server import default_static_dir;"
            "from looper.db.connection import SCHEMA_PATH;"
            "print(default_static_dir()); print(SCHEMA_PATH)",
            cwd=work,
        ).split()
        static_dir, schema = Path(located[0]), Path(located[1])
        assert str(work) in str(static_dir) and (static_dir / "index.html").is_file(), static_dir
        assert str(work) in str(schema) and schema.is_file(), schema
        print("PASS the installed package resolves its own schema and dashboard")
        version = run(str(venv / "bin" / "looper"), "--version", cwd=work).split()
        assert version[0] == "looper", version
        assert not (venv / "bin" / "loopx").exists(), "the retired loopx alias is installed"
        print(f"PASS the wheel installs the looper command ({' '.join(version)}) and not the retired loopx alias")

        dsn = os.environ.get("LOOPER_TEST_DATABASE_URL")
        if dsn:
            serve_check(venv, work, dsn)
        else:
            print("SKIP serve check: LOOPER_TEST_DATABASE_URL is not set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
