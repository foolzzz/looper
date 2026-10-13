"""Keep install, update and recovery entry points on this fork's repository."""

from pathlib import Path
import re
import tomllib

from loopx.install_contract import LOOPX_SOURCE_REPOSITORY, NO_CLONE_INSTALL_URL
from loopx.self_update import DEFAULT_UPDATE_REPO, _source_config


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_REPOSITORY = "loopx-project/loopx"
LOOPX_INSTALLER_URL = re.compile(r"https://[^\s`'\")]*loopx[^\s`'\")]*/install\.sh")


def test_default_recovery_and_package_identity():
    assert LOOPX_SOURCE_REPOSITORY == "foolzzz/looper"
    assert DEFAULT_UPDATE_REPO == LOOPX_SOURCE_REPOSITORY
    assert NO_CLONE_INSTALL_URL == (
        "https://raw.githubusercontent.com/foolzzz/looper/main/install.sh"
    )
    assert f'REPO="{LOOPX_SOURCE_REPOSITORY}"' in (ROOT / "install.sh").read_text()
    installer = (ROOT / "scripts/install-from-github.sh").read_text()
    assert f"${{LOOPX_REPO:-{LOOPX_SOURCE_REPOSITORY}}}" in installer
    # Package metadata is not an install source; it still names the upstream
    # project this fork branched from.
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["project"]["urls"]["Repository"] == (
        f"https://github.com/{UPSTREAM_REPOSITORY}"
    )
    assert metadata["project"]["urls"]["Homepage"] == "https://loopx-project.github.io/loopx/"


def test_documented_no_clone_installer_is_the_contract_url():
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    stale = {
        path.relative_to(ROOT).as_posix(): url
        for path in documents
        for url in LOOPX_INSTALLER_URL.findall(path.read_text(encoding="utf-8"))
        if url != NO_CLONE_INSTALL_URL
    }
    assert stale == {}


def test_explicit_archive_source_remains_operator_owned(monkeypatch):
    for key in ("LOOPX_REPO", "LOOPX_REF", "LOOPX_ARCHIVE_URL"):
        monkeypatch.delenv(key, raising=False)
    source = _source_config(repo="example/fork", ref="custom", archive_url=None)
    assert source["archive_url"] == "https://codeload.github.com/example/fork/tar.gz/custom"
