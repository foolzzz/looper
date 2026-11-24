"""A profile source is one confined Markdown file, independent of other vault tools."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import projects, vault


def test_profile_source_defaults_without_requiring_a_profile(tmp_path: Path) -> None:
    config = vault.check_config({"path": str(tmp_path)})
    assert config.profile_source == "关于我.md"


@pytest.mark.parametrize("source", ["../outside.md", "/outside.md", "missing.md", "notes.md", "note.txt", "", None])
def test_profile_source_refuses_invalid_files(tmp_path: Path, source: str | None) -> None:
    (tmp_path / "notes.md").mkdir()
    (tmp_path / "note.txt").write_text("profile")
    with pytest.raises(ValueError, match="profile_source"):
        vault.check_config({"path": str(tmp_path), "profile_source": source})


@pytest.mark.parametrize("outside", [False, True])
def test_profile_source_resolves_symlinks(tmp_path: Path, outside: bool) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    target = (tmp_path if outside else root) / "note.md"
    target.write_text("## Name\nAda\n")
    (root / "link.md").symlink_to(target)
    if outside:
        with pytest.raises(ValueError, match="profile_source.*outside"):
            vault.check_config({"path": str(root), "profile_source": "link.md"})
    else:
        config = vault.check_config({"path": str(root), "profile_source": "link.md"})
        assert config.profile_source == "link.md"
        assert vault.profile_path(config.path, config.profile_source) == target.resolve()


def test_profile_source_update_replaces_and_read_rechecks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    note = tmp_path / "notes" / "profile.md"
    note.parent.mkdir()
    note.write_text("## Name\nAda\n")
    stored: dict[str, str] = {}

    async def get(keys):
        return {key: stored[key] for key in keys if key in stored}

    async def put(values):
        stored.update(values)

    monkeypatch.setattr(vault.system_settings, "get_settings", get)
    monkeypatch.setattr(vault.system_settings, "put_settings", put)
    monkeypatch.setattr(vault, "_git_root", AsyncMock(return_value=None))

    async def run():
        body = {"path": str(tmp_path), "profile_source": "notes/profile.md"}
        view = await vault.update_settings(body)
        assert view["profile_source"] == "notes/profile.md"
        assert json.loads(stored[vault.SETTING_KEY])["profile_source"] == "notes/profile.md"
        note.unlink()
        # The profile reader must refuse the missing file; unrelated vault tools remain available.
        config, problem = await vault.load()
        assert config is not None and problem is None
        with pytest.raises(ValueError, match="regular file"):
            vault.profile_path(config.path, config.profile_source)
        with pytest.raises(projects.RequestError, match="regular file"):
            await vault.update_settings(body)
        assert (await vault.update_settings({"path": str(tmp_path)}))["profile_source"] == "关于我.md"
        assert "profile_source" not in json.loads(stored[vault.SETTING_KEY])

    asyncio.run(run())


def test_an_implicit_default_cannot_escape_on_save(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("## Name\nOutside\n", encoding="utf-8")
    (root / vault.DEFAULT_PROFILE_SOURCE).symlink_to(outside)
    with pytest.raises(ValueError, match="profile_source.*outside"):
        vault.check_config({"path": str(root)})
