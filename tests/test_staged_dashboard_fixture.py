"""The staged-dashboard guard fails in CI but only skips in local checkouts."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import _require_staged_dashboard


def test_missing_dashboard_skips_outside_ci(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    with pytest.raises(pytest.skip.Exception, match="UNTESTED: staged dashboard missing"):
        _require_staged_dashboard(tmp_path)


@pytest.mark.parametrize("variable", ["CI", "GITHUB_ACTIONS"])
def test_missing_dashboard_fails_in_ci(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str,
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv(variable, "true")

    with pytest.raises(pytest.fail.Exception, match="staged dashboard missing"):
        _require_staged_dashboard(tmp_path)


def test_present_dashboard_is_returned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CI", raising=False)
    (tmp_path / "index.html").write_text("<!doctype html>\n", encoding="utf-8")

    assert _require_staged_dashboard(tmp_path) == tmp_path
