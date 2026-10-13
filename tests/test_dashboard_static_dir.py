"""`loopx serve` serves the checkout's dashboard, else the copy packaged in the wheel."""

from __future__ import annotations

from pathlib import Path

import pytest

from loopx.agents import http_server


def _dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, checkout_built: bool) -> tuple[Path, Path]:
    checkout, packaged = tmp_path / "web" / "dist", tmp_path / "loopx" / "web" / "dashboard"
    for d in (checkout, packaged):
        d.mkdir(parents=True)
    (packaged / "index.html").write_text("packaged")
    if checkout_built:
        (checkout / "index.html").write_text("checkout")
    monkeypatch.setattr(http_server, "_CHECKOUT_DASHBOARD", checkout)
    monkeypatch.setattr(http_server, "_PACKAGED_DASHBOARD", packaged)
    return checkout, packaged


def test_checkout_build_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, _ = _dirs(tmp_path, monkeypatch, checkout_built=True)
    assert http_server.default_static_dir() == checkout


def test_installed_wheel_uses_the_packaged_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, packaged = _dirs(tmp_path, monkeypatch, checkout_built=False)
    assert http_server.default_static_dir() == packaged
