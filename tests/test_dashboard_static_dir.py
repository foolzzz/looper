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


@pytest.mark.parametrize("checkout_built", [True, False])
def test_checkout_build_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout_built: bool) -> None:
    checkout, packaged = _dirs(tmp_path, monkeypatch, checkout_built=checkout_built)
    # An installed wheel has no checkout build and serves its packaged copy.
    assert http_server.default_static_dir() == (checkout if checkout_built else packaged)


