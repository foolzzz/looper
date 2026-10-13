"""The no-clone `install.sh` installs only a wheel from this fork's releases."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from loopx.install_contract import LOOPX_SOURCE_CLONE_URL, LOOPX_SOURCE_REPOSITORY


ROOT = Path(__file__).resolve().parents[1]
WHEEL_URL = (
    f"https://github.com/{LOOPX_SOURCE_REPOSITORY}/releases/download/v9.9.9/"
    "loopx-9.9.9-py3-none-any.whl"
)

# Imported by the installer's own Python: answers the GitHub releases request
# from a fixture and records which URL was asked for.
FAKE_RELEASE_API = '''
import io, os, urllib.request

def _urlopen(request, *args, **kwargs):
    url = getattr(request, "full_url", request)
    with open(os.environ["FAKE_RELEASE_URL_LOG"], "a", encoding="utf-8") as log:
        log.write(url + "\\n")
    return io.BytesIO(os.environ["FAKE_RELEASE_BODY"].encode())

urllib.request.urlopen = _urlopen
'''


def run_installer(tmp_path: Path, assets: list[dict[str, str]]) -> tuple[
    subprocess.CompletedProcess[str], Path, Path
]:
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(FAKE_RELEASE_API, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
    )
    install_log = tmp_path / "install.log"
    for manager in ("uv", "pipx"):
        (bin_dir / manager).write_text(
            f'#!/bin/sh\necho "{manager} $*" >> "{install_log}"\n', encoding="utf-8"
        )
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    url_log = tmp_path / "release-urls.log"
    result = subprocess.run(
        ["bash", str(ROOT / "install.sh")],
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "PYTHONPATH": str(site),
            "FAKE_RELEASE_BODY": json.dumps({"tag_name": "v9.9.9", "assets": assets}),
            "FAKE_RELEASE_URL_LOG": str(url_log),
        },
        text=True,
        capture_output=True,
        timeout=60,
    )
    return result, install_log, url_log


@pytest.mark.skipif(os.name == "nt", reason="install.sh is a POSIX installer")
def test_release_without_wheel_installs_nothing(tmp_path: Path) -> None:
    sdist = {
        "name": "loopx-9.9.9.tar.gz",
        "browser_download_url": WHEEL_URL.replace("-py3-none-any.whl", ".tar.gz"),
    }

    result, install_log, url_log = run_installer(tmp_path, [sdist])

    assert result.returncode == 1, result
    assert not install_log.exists(), install_log.read_text()
    assert url_log.read_text().splitlines() == [
        f"https://api.github.com/repos/{LOOPX_SOURCE_REPOSITORY}/releases/latest"
    ]
    output = " ".join(result.stdout.split())
    assert (
        f"The latest github.com/{LOOPX_SOURCE_REPOSITORY} release has no wheel asset, "
        "so nothing was installed."
    ) in output, result.stdout
    assert f"git clone {LOOPX_SOURCE_CLONE_URL} && cd loopx && git checkout vX.Y.Z" in output
    assert "uv sync" in output
    assert "Check your network" not in output


@pytest.mark.skipif(os.name == "nt", reason="install.sh is a POSIX installer")
def test_release_wheel_is_installed_with_uv(tmp_path: Path) -> None:
    wheel = {"name": "loopx-9.9.9-py3-none-any.whl", "browser_download_url": WHEEL_URL}

    result, install_log, _url_log = run_installer(tmp_path, [wheel])

    assert result.returncode == 0, result
    assert "Latest release: v9.9.9" in result.stdout
    [install] = install_log.read_text().splitlines()
    assert install.startswith("uv tool install --force --python "), install
    assert install.endswith(f"loopx @ {WHEEL_URL}"), install
