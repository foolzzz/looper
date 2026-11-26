"""profile.get keeps one cached block and replaces it only when the compiled body changes."""

import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents.operator_profile import profile_view, update_source
from looper.db import connection, profile_blocks
from tests.db_support import dropped_tables, run_db


async def _arm(pool: Any, root: Path) -> None:
    await pool.execute(
        "INSERT INTO system_settings (key, value) VALUES ($1, $2)",
        "knowledge_vault",
        json.dumps({"path": str(root.resolve()), "writable": [], "rules_file": None}))


def _vault(root: Path, name: str = "Ada") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "关于我.md").write_text(f"## 名字\n{name}\n\n## 爱好\n下棋\n", encoding="utf-8")
    folder = root / "偏好"
    folder.mkdir()
    (folder / "rule.md").write_text("---\nsummary: 短句\n---\n# 规则\n", encoding="utf-8")


def _body(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def test_a_read_keeps_the_version_until_the_compiled_body_changes(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _vault(root)

    async def test(pool: Any) -> None:
        assert await pool.fetchval("SELECT to_regclass('profile_blocks') IS NOT NULL")
        await _arm(pool, root)
        first = await profile_view()
        assert first["configured"] is True and first["version"] == 1
        assert first["text"] == "Name: Ada"
        assert first["fields"]["name"]["path"] == "关于我.md"
        assert "下棋" not in first["text"]
        row = await pool.fetchrow("SELECT version, text, body FROM profile_blocks WHERE id = TRUE")
        assert row["version"] == 1 and row["text"] == first["text"]
        assert _body(row["body"])["sources"] == ["关于我.md"]
        await pool.execute("UPDATE profile_blocks SET compiled_at = '2020-01-01T00:00:00Z'")
        second = await profile_view()
        assert second["version"] == 1
        assert (await pool.fetchval("SELECT compiled_at FROM profile_blocks")).year == 2020

        (root / "关于我.md").write_text(
            (root / "关于我.md").read_text(encoding="utf-8") + "\n## 别的\n不进档案\n", encoding="utf-8")
        untouched = await profile_view()
        assert untouched["version"] == 1
        assert (await pool.fetchval("SELECT compiled_at FROM profile_blocks")).year == 2020

        (root / "关于我.md").write_text("## Name\nBee\n", encoding="utf-8")
        changed = await profile_view()
        assert changed["version"] == 2 and changed["text"] == "Name: Bee"
        assert (await pool.fetchval("SELECT compiled_at FROM profile_blocks")).year != 2020

    run_db(test, monkeypatch)


@pytest.mark.parametrize("with_fact", [False, True])
def test_main_cache_version_changes_only_with_compiled_text(monkeypatch, tmp_path, with_fact):
    note = "## Name\nAda\n" + ("- Builds compilers [kind:: fact]\n" if with_fact else "")
    (tmp_path / "关于我.md").write_text(note, encoding="utf-8")
    # The cache payload on main, before the About group existed.
    legacy = {"text": "Name: Ada" + ("\nStanding rules:\n- Builds compilers" if with_fact else ""),
              "fields": {"name": {"value": "Ada", "path": "关于我.md", "heading": "Name", "line": 1},
                         "address": None, "working_hours": None, "time_zone": None, "reply_language": None,
                         "communication_preferences": [], "standing_rules": []},
              "omitted": [], "skipped": [], "sources": ["关于我.md"]}
    if with_fact:
        legacy["fields"]["standing_rules"] = [{"value": "Builds compilers", "path": "关于我.md",
                                               "heading": "Name", "line": 3, "kind": "fact"}]

    async def test(pool):
        await _arm(pool, tmp_path)
        assert await profile_blocks.retain_or_replace(text=legacy["text"], body=legacy) == 1
        view = await profile_view()
        expected = "Name: Ada" + ("\nAbout:\n- Builds compilers" if with_fact else "")
        version = 2 if with_fact else 1
        assert (view["text"], view["version"]) == (expected, version)
        assert view["fields"]["about"] == legacy["fields"]["standing_rules"]
        assert (await profile_view())["version"] == version
        # Citations refresh without a version bump when the rendered text stays the same.
        (tmp_path / "关于我.md").write_text("\n" + note, encoding="utf-8")
        moved = await profile_view()
        assert (moved["text"], moved["version"]) == (expected, version)
        assert moved["fields"]["name"]["line"] == 2
        row = await pool.fetchrow("SELECT version, text, body FROM profile_blocks")
        assert (row["text"], row["version"]) == (expected, version)
        assert _body(row["body"]) == {key: moved[key] for key in legacy}
        (tmp_path / "关于我.md").write_text(note.replace("Ada", "Bee"), encoding="utf-8")
        assert (await profile_view())["version"] == version + 1

    run_db(test, monkeypatch)


def test_resetting_the_source_restores_the_default_without_changing_other_vault_settings(monkeypatch, tmp_path):
    root = tmp_path / "vault"
    _vault(root)
    (root / "profile.md").write_text("## Name\nBee\n", encoding="utf-8")

    async def test(pool: Any) -> None:
        await _arm(pool, root)
        await update_source("profile.md")
        assert (await profile_view())["text"] == "Name: Bee"
        assert await update_source(None) == {"source": "关于我.md"}
        view = await profile_view()
        assert view["text"] == "Name: Ada" and view["version"] == 2
        stored = _body(await pool.fetchval("SELECT value FROM system_settings WHERE key = 'knowledge_vault'"))
        assert stored["path"] == str(root.resolve()) and "profile_source" not in stored

    run_db(test, monkeypatch)


def test_an_unreadable_source_does_not_serve_the_cached_block(monkeypatch, tmp_path, caplog) -> None:
    root = tmp_path / "vault"
    _vault(root)

    async def test(pool: Any) -> None:
        await _arm(pool, root)
        assert (await profile_view())["text"] == "Name: Ada"
        (root / "关于我.md").unlink()
        view = await profile_view()
        assert view["configured"] is False and view["text"] == ""
        assert await pool.fetchval("SELECT version FROM profile_blocks") == 1
        assert await pool.fetchval("SELECT text FROM profile_blocks") == "Name: Ada"
        assert "omitting it" in caplog.text

    run_db(test, monkeypatch)


def test_changing_the_source_keeps_the_block_until_the_next_different_profile(monkeypatch, tmp_path) -> None:
    _vault(tmp_path, "Ada")
    (tmp_path / "other.md").write_text("## Name\nBee\n", encoding="utf-8")

    async def test(pool: Any) -> None:
        await _arm(pool, tmp_path)
        await update_source("关于我.md")
        assert await pool.fetchval("SELECT count(*) FROM profile_blocks") == 0
        await profile_view()
        await update_source("other.md")
        kept = await pool.fetchrow("SELECT version, text FROM profile_blocks")
        assert kept["version"] == 1 and kept["text"] == "Name: Ada"
        view = await profile_view()
        assert view["version"] == 2 and view["text"] == "Name: Bee"
        await update_source("other.md")
        assert (await profile_view())["version"] == 2

    run_db(test, monkeypatch)


def test_the_table_rejects_an_oversized_text_and_a_bad_version(monkeypatch) -> None:
    import asyncpg

    async def test(pool: Any) -> None:
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(
                "INSERT INTO profile_blocks (id, version, text, body) VALUES (TRUE, 1, $1, '{}'::jsonb)",
                "x" * 1501)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(
                "INSERT INTO profile_blocks (id, version, text, body) VALUES (TRUE, 0, 'ok', '{}'::jsonb)")
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(
                "INSERT INTO profile_blocks (id, version, text, body) VALUES (TRUE, 1, 'ok', '[]'::jsonb)")
        await pool.execute(
            "INSERT INTO profile_blocks (id, version, text, body) VALUES (TRUE, 1, $1, '{}'::jsonb)", "名" * 1500)

    run_db(test, monkeypatch)


def test_a_database_without_the_table_gets_it_on_upgrade(monkeypatch) -> None:
    async def test(pool: Any) -> None:
        async with dropped_tables(pool, "profile_blocks"):
            await pool.execute("DELETE FROM schema_migrations WHERE name = 'profile_blocks'")
            async with pool.acquire() as conn:
                assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval(
            "SELECT count(*) FROM schema_migrations WHERE name = 'profile_blocks'") == 1
        await pool.execute(
            "INSERT INTO profile_blocks (id, version, text, body) VALUES (TRUE, 1, 'Name: Ada', '{}'::jsonb)")
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT count(*) FROM profile_blocks") == 1

    run_db(test, monkeypatch)
