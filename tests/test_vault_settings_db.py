"""Knowledge vault settings persisted on PostgreSQL; only a disposable database is supported."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import vault
from looper.db import system_settings
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_vault_settings_store_read_back_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "codex-mem").mkdir()
    (tmp_path / "rules.md").write_text("Keep notes short.", encoding="utf-8")
    monkeypatch.setattr(vault, "_git_root", AsyncMock(return_value=None))

    async def test(pool: Any) -> None:
        assert (await vault.settings_view())["configured"] is False
        view = await vault.update_settings({"path": str(tmp_path), "writable": ["codex-mem/"],
                                            "rules_file": "rules.md"})
        assert view == {"configured": True, "path": str(tmp_path.resolve()), "writable": ["codex-mem"],
                        "rules_file": "rules.md", "git": False, "problem": None}
        assert (await system_settings.get_settings([vault.SETTING_KEY]))[vault.SETTING_KEY]
        assert await vault.settings_view() == view
        cleared = await vault.update_settings({"path": None})
        assert cleared["configured"] is False and cleared["path"] is None
        assert vault.SETTING_KEY not in await system_settings.get_settings([vault.SETTING_KEY])

    run_db(test, monkeypatch)


def test_saved_vault_becomes_unconfigured_when_its_folder_vanishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "notes"
    root.mkdir()

    async def test(pool: Any) -> None:
        await vault.update_settings({"path": str(root)})
        root.rmdir()
        view = await vault.settings_view()
        assert view["configured"] is False and view["path"] == str(root)
        assert view["problem"] and "no longer holds" in view["problem"]
        assert await vault.active() is None

    run_db(test, monkeypatch)
