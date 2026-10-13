"""The plugin and skill library: scanning the operator's Claude Code, caps, materializing and launching.

These tests use a synthetic operator home (``library_fixtures``) and stub the
database; ``test_library_db.py`` covers the tables and the API against a real
PostgreSQL.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from library_fixtures import PLUGIN_FILES, fake_claude_home, write

from loopx.agents import library
from loopx.agents.agent_settings import BUILTIN_DEFAULTS, resolve_launch
from loopx.agents.claude_cli import library_args
from loopx.agents.library import (
    SKILLS_PLUGIN,
    AgentLibrary,
    ItemKind,
    LibraryError,
    LoadedItem,
    item_sha256,
    load_library,
    materialize_item,
    scan_machine,
    skills_plugin,
)


def _sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The synthetic ``~/.claude``, with ``HOME`` pointing at it."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return fake_claude_home(tmp_path / "home")


def _by_name(scan: Any) -> dict[str, Any]:
    return {item.name: item for item in scan.items}


def _files(item: Any) -> dict[str, Any]:
    return {f.path: f for f in item.files}


class TestScan:
    def test_the_enabled_user_plugins_and_every_personal_skill(self, home: Path) -> None:
        scan = scan_machine()  # the operator's ~/.claude
        items = _by_name(scan)
        assert sorted((i.kind.value, i.name) for i in scan.items) == [
            ("plugin", "demo@market"), ("skill", "alpha"), ("skill", "beta")]
        demo = items["demo@market"]
        # The user-scope install, not the project-scope copy; a disabled plugin is not imported.
        assert demo.version == "1.2.0"
        assert demo.source == "~/.claude/plugins/cache/market/demo/1.2.0"
        files = _files(demo)
        assert {path: files[path].content for path in PLUGIN_FILES} == PLUGIN_FILES
        # The whole plugin: its skills, agents and hooks, executable bits kept.
        assert files["hooks/start.sh"].executable and not files["README.md"].executable
        # A link inside the plugin is read as its target; VCS and Claude Code bookkeeping are left out.
        assert files["AGENTS.md"].content == "# demo\n"
        assert not any(path.startswith((".git/", ".in_use/")) for path in files)
        # A linked skill directory is followed, as Claude Code does.
        assert _files(items["beta"])["SKILL.md"].content.endswith("Reply BETA-OK.\n")
        assert _files(items["alpha"])["scripts/run.py"].executable
        assert (items["alpha"].source, items["beta"].source) == ("~/.claude/skills/alpha", "~/.claude/skills/beta")

    def test_what_was_left_out_is_reported(self, home: Path) -> None:
        skipped = {(s.item, s.path, s.reason) for s in scan_machine(home).skipped}
        assert ("demo@market", "assets/logo.png", "binary") in skipped
        assert ("ghost@market", "", "not installed for the user") in skipped
        assert ("notes", "", "no SKILL.md") in skipped
        # Neither the disabled plugin nor the hidden skills directory is mentioned.
        assert not any(s[0] in ("off@market", ".trash") for s in skipped)

    def test_personal_settings_and_hooks_are_not_imported(self, home: Path) -> None:
        contents = "".join(f.content for item in scan_machine(home).items for f in item.files)
        assert "prompt-log" not in contents and "UserPromptSubmit" not in contents

    def test_a_link_out_of_the_item_is_skipped(self, home: Path, tmp_path: Path) -> None:
        secret = write(tmp_path / "outside.txt", "outside\n")
        os.symlink(secret, home / "skills" / "alpha" / "outside.txt")
        scan = scan_machine(home)
        assert "outside.txt" not in _files(_by_name(scan)["alpha"])
        assert ("alpha", "outside.txt", "link out of the item") in {(s.item, s.path, s.reason) for s in scan.skipped}

    def test_caps(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        write(home / "skills" / "alpha" / "big.md", "x" * 300)
        monkeypatch.setattr(library, "FILE_MAX_BYTES", 200)
        scan = scan_machine(home)
        assert ("alpha", "big.md", "oversized") in {(s.item, s.path, s.reason) for s in scan.skipped}
        assert "big.md" not in _files(_by_name(scan)["alpha"])

        monkeypatch.setattr(library, "FILE_MAX_BYTES", 2 * 1024 * 1024)
        monkeypatch.setattr(library, "ITEM_MAX_FILES", 2)  # alpha has three files
        scan = scan_machine(home)
        assert "alpha" not in _by_name(scan) and "beta" in _by_name(scan)  # beta has one file
        (reason,) = [s.reason for s in scan.skipped if s.item == "alpha"]
        assert "more than 2 files" in reason

        monkeypatch.setattr(library, "ITEM_MAX_FILES", 5_000)
        monkeypatch.setattr(library, "ITEM_MAX_BYTES", 100)
        assert "alpha" not in _by_name(scan_machine(home))

        monkeypatch.setattr(library, "ITEM_MAX_BYTES", 10_000)
        monkeypatch.setattr(library, "LIBRARY_MAX_BYTES", 300)
        with pytest.raises(LibraryError, match="over the cap"):
            scan_machine(home)

    def test_without_a_claude_configuration_the_import_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(LibraryError, match="no Claude Code configuration"):
            scan_machine(tmp_path / "nobody" / ".claude")

    def test_an_unreadable_settings_file_is_refused(self, home: Path) -> None:
        (home / "settings.json").write_text("{not json")
        with pytest.raises(LibraryError, match="settings.json"):
            scan_machine(home)

    def test_the_version_is_the_hash_of_the_files(self, home: Path) -> None:
        first = _by_name(scan_machine(home))["alpha"].sha256
        assert _by_name(scan_machine(home))["alpha"].sha256 == first
        (home / "skills" / "alpha" / "scripts" / "run.py").chmod(0o644)
        assert _by_name(scan_machine(home))["alpha"].sha256 != first  # the executable bit counts
        (home / "skills" / "alpha" / "scripts" / "run.py").chmod(0o755)
        (home / "skills" / "alpha" / "SKILL.md").write_text("changed\n")
        assert _by_name(scan_machine(home))["alpha"].sha256 != first


def _rows(*items: tuple[str, str, dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Database rows for enabled items ``(kind, name, {path: content})`` and their files by item id."""
    rows, files = [], {}
    for index, (kind, name, contents) in enumerate(items):
        item_files = [{"path": path, "content": text, "sha256": _sha(text), "executable": path.endswith(".sh")}
                      for path, text in sorted(contents.items())]
        item_id = f"lib-{index:012d}"
        rows.append({"id": item_id, "kind": kind, "name": name, "sha256": item_sha256(item_files)})
        files[item_id] = item_files
    return rows, files


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What the library tables hold (``rows``: the enabled items; ``files`` by item id) and the file reads made."""
    from loopx.db import library as db_library

    state: dict[str, Any] = {"rows": [], "files": {}, "reads": []}

    async def _enabled(**_: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in state["rows"]]

    async def _files(item_id: str, **_: Any) -> list[dict[str, Any]]:
        state["reads"].append(item_id)
        return [dict(f) for f in state["files"][item_id]]

    monkeypatch.setattr(db_library, "list_enabled_items", _enabled)
    monkeypatch.setattr(db_library, "get_item_files", _files)
    return state


DEMO = ("plugin", "demo@market", {".claude-plugin/plugin.json": '{"name": "demo"}', "hooks/start.sh": "echo hi\n"})
ALPHA = ("skill", "alpha", {"SKILL.md": "---\nname: alpha\n---\nReply ALPHA-OK.\n"})
BETA = ("skill", "beta", {"SKILL.md": "---\nname: beta\n---\nReply BETA-OK.\n"})


class TestMaterialize:
    def test_an_item_is_written_once_read_only_under_its_hash(self, tmp_path: Path) -> None:
        (row,), files = _rows(DEMO)
        item = LoadedItem(ItemKind.PLUGIN, "demo@market", row["sha256"])
        path = materialize_item(tmp_path, item, files[row["id"]])
        assert path == tmp_path.absolute() / ".local" / "library" / row["sha256"] / "demo"
        assert (path / "hooks" / "start.sh").read_text() == "echo hi\n"
        assert os.access(path / "hooks" / "start.sh", os.X_OK)
        assert not os.access(path / ".claude-plugin" / "plugin.json", os.X_OK)
        for written in (path / "hooks" / "start.sh", path / ".claude-plugin" / "plugin.json", path / "hooks", path):
            assert not written.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH), written
        # A second materialization reuses the directory; no temporary directory is left behind.
        assert materialize_item(tmp_path, item, files[row["id"]]) == path
        assert [p.name for p in (tmp_path / ".local" / "library").iterdir()] == [row["sha256"]]

    def test_files_that_do_not_match_the_hash_are_refused(self, tmp_path: Path) -> None:
        (row,), files = _rows(ALPHA)
        tampered = [{**f, "content": "Reply EVIL.\n"} for f in files[row["id"]]]
        with pytest.raises(LibraryError, match="do not match"):
            materialize_item(tmp_path, LoadedItem(ItemKind.SKILL, "alpha", row["sha256"]), tampered)
        retagged = [{**f, "content": "Reply EVIL.\n", "sha256": _sha("Reply EVIL.\n")} for f in files[row["id"]]]
        with pytest.raises(LibraryError, match="do not match"):
            materialize_item(tmp_path, LoadedItem(ItemKind.SKILL, "alpha", row["sha256"]), retagged)
        assert not (tmp_path / ".local" / "library" / row["sha256"]).exists()

    def test_a_path_out_of_the_item_is_refused(self, tmp_path: Path) -> None:
        files = [{"path": "../escape.md", "content": "x", "sha256": _sha("x"), "executable": False}]
        with pytest.raises(LibraryError, match="path"):
            materialize_item(tmp_path, LoadedItem(ItemKind.SKILL, "alpha", item_sha256(files)), files)

    def test_the_skills_plugin_links_each_enabled_skill(self, tmp_path: Path) -> None:
        rows, files = _rows(ALPHA, BETA)
        items = [LoadedItem(ItemKind.SKILL, row["name"], row["sha256"]) for row in rows]
        for item, row in zip(items, rows):
            materialize_item(tmp_path, item, files[row["id"]])
        plugin = skills_plugin(tmp_path, items)
        assert plugin.name == SKILLS_PLUGIN
        assert json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())["name"] == SKILLS_PLUGIN
        assert (plugin / "skills" / "beta" / "SKILL.md").read_text().endswith("Reply BETA-OK.\n")
        assert os.readlink(plugin / "skills" / "alpha") == f"../../../{rows[0]['sha256']}/alpha"
        # Another set of skills is another directory; the same set is the same one.
        assert skills_plugin(tmp_path, items[:1]) != plugin
        assert skills_plugin(tmp_path, list(reversed(items))) == plugin


class TestLoadLibrary:
    def test_enabled_plugins_then_the_skills_plugin(self, tmp_path: Path, stored: dict[str, Any]) -> None:
        stored["rows"], stored["files"] = _rows(DEMO, ALPHA, BETA)
        loaded = asyncio.run(load_library(tmp_path))
        sha = {row["name"]: row["sha256"] for row in stored["rows"]}
        library_dir = tmp_path.absolute() / ".local" / "library"
        assert loaded.plugin_dirs[0] == str(library_dir / sha["demo@market"] / "demo")
        assert len(loaded.plugin_dirs) == 2 and Path(loaded.plugin_dirs[1]).name == SKILLS_PLUGIN
        assert sorted(p.name for p in (Path(loaded.plugin_dirs[1]) / "skills").iterdir()) == ["alpha", "beta"]
        assert loaded.record() == {
            "sha256": loaded.sha256,
            "items": [{"kind": "plugin", "name": "demo@market", "sha256": sha["demo@market"]},
                      {"kind": "skill", "name": "alpha", "sha256": sha["alpha"]},
                      {"kind": "skill", "name": "beta", "sha256": sha["beta"]}],
        }
        # Files are read from the database only to materialize an item for the first time.
        assert len(stored["reads"]) == 3
        assert asyncio.run(load_library(tmp_path)) == loaded and len(stored["reads"]) == 3

    def test_the_library_hash_follows_the_enabled_set(self, tmp_path: Path, stored: dict[str, Any]) -> None:
        stored["rows"], stored["files"] = _rows(DEMO, ALPHA, BETA)
        everything = asyncio.run(load_library(tmp_path))
        stored["rows"] = [row for row in stored["rows"] if row["name"] != "beta"]
        without_beta = asyncio.run(load_library(tmp_path))
        assert without_beta.sha256 != everything.sha256
        assert sorted(p.name for p in (Path(without_beta.plugin_dirs[1]) / "skills").iterdir()) == ["alpha"]
        assert "beta" not in json.dumps(without_beta.record())
        stored["rows"] = []
        assert asyncio.run(load_library(tmp_path)) == AgentLibrary()

    def test_an_unreadable_library_runs_the_agent_without_it_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from loopx.db import library as db_library

        async def _down(**_: Any) -> list[dict[str, Any]]:
            raise OSError("database is down")

        monkeypatch.setattr(db_library, "list_enabled_items", _down)
        loaded = asyncio.run(load_library(tmp_path))
        assert loaded.plugin_dirs == () and loaded.record() == {
            "sha256": None, "items": [], "error": "the library could not be read"}

    def test_a_damaged_item_runs_the_agent_without_the_library(self, tmp_path: Path, stored: dict[str, Any]) -> None:
        stored["rows"], stored["files"] = _rows(DEMO, ALPHA)
        stored["files"]["lib-000000000001"][0]["content"] = "Reply EVIL.\n"
        loaded = asyncio.run(load_library(tmp_path))
        assert loaded.plugin_dirs == () and "do not match" in loaded.record()["error"]


class TestLaunch:
    def test_library_args_name_each_plugin_directory(self) -> None:
        assert library_args(AgentLibrary()) == []
        loaded = AgentLibrary(items=(LoadedItem(ItemKind.SKILL, "alpha", "a" * 64),), plugin_dirs=("/l/a", "/l/b"))
        assert library_args(loaded) == ["--plugin-dir", "/l/a", "--plugin-dir", "/l/b"]

    def test_a_launch_carries_and_records_its_library(self) -> None:
        assert "--plugin-dir" not in resolve_launch(BUILTIN_DEFAULTS).args()
        assert "library" not in resolve_launch(BUILTIN_DEFAULTS).record()
        loaded = AgentLibrary(items=(LoadedItem(ItemKind.PLUGIN, "demo@market", "b" * 64),), plugin_dirs=("/l/demo",))
        launch = resolve_launch(BUILTIN_DEFAULTS, None, loaded)
        assert launch.args() == ["--model", "opus", "--plugin-dir", "/l/demo"]
        assert launch.record()["library"] == loaded.record()
        assert launch.library.sha256 == loaded.sha256 != AgentLibrary().sha256
