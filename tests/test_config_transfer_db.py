"""Configuration export and import (``loopx config``) against a real PostgreSQL.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database. Each
test works in new schemas of its own (one per installation), dropped
afterwards; the CLI reaches a schema through the URL's ``search_path``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_catalog_db import DSN, _call, _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")

SECRET = "sk-test-secret-0123456789"
ROLES = ("manager", "planner", "worker", "reviewer")


@contextlib.asynccontextmanager
async def _installation(*, schema: bool = True) -> AsyncIterator[tuple[Any, str]]:
    """A new schema (with LoopX's tables unless ``schema`` is False): its pool and a URL that reaches it."""
    import asyncpg

    from looper.db.connection import ensure_schema

    name = f"loopx_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{name}"')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": name})
    parts = urllib.parse.urlsplit(DSN)
    query = urllib.parse.urlencode([*urllib.parse.parse_qsl(parts.query), ("search_path", name)])
    try:
        if schema:
            await ensure_schema(pool)
        yield pool, urllib.parse.urlunsplit(parts._replace(query=query))
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        await admin.close()


async def _loopx(monkeypatch: pytest.MonkeyPatch, url: str, *argv: str) -> tuple[int, str, str]:
    """Run the ``loopx`` command on the installation at ``url``: its exit code, stdout and stderr."""
    from looper import cli

    def run() -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    monkeypatch.setenv("LOOPX_DATABASE_URL", url)
    return await asyncio.to_thread(run)


def _serve(monkeypatch: pytest.MonkeyPatch, pool: Any) -> None:
    """Make ``pool`` the server's, for the API calls that set an installation up."""
    from looper.db import connection

    monkeypatch.setattr(connection, "_pool", pool)


async def _ok(method: str, path: str, body: Any = None) -> Any:
    status, payload = await _call(method, path, body)
    assert status in (200, 201), (path, payload)
    return payload


async def _populate(monkeypatch: pytest.MonkeyPatch, pool: Any, work_dir: Path) -> dict[str, str]:
    """An installation using every section of the file, set up through the API as a user would."""
    from looper.agents.serve import seed_agent_configs, seed_channel_configs
    from looper.db import library as db_library

    _serve(monkeypatch, pool)
    await seed_agent_configs(pool)
    await seed_channel_configs(pool)
    # Runtimes, providers and agents: a runtime on a command (portable) and one on a path (left out), a provider
    # with an API key and a model catalog, and agents on the built-in and the registered pieces.
    beta = (await _ok("POST", "/api/runtimes", {"name": "claude-beta", "type": "claude-code",
                                                "executable": "claude-beta"}))["runtime"]["runtime_id"]
    await _ok("POST", "/api/runtimes", {"name": "claude-local", "type": "claude-code",
                                        "executable": "/opt/claude-local/bin/claude"})
    await _ok("POST", "/api/providers", {"name": "anthropic-key", "protocols": ["anthropic-messages"],
                                         "auth": "api_key", "api_key": SECRET, "base_url": "https://api.example.test",
                                         "models": [{"name": "claude-sonnet-4-5", "input_usd_per_mtok": 3,
                                                     "output_usd_per_mtok": 15}]})
    sonnet = (await _ok("POST", "/api/agents", {"name": "claude-code/sonnet", "runtime_id": "rt-claude-code",
                                                "provider_id": "prov-claude-subscription",
                                                "model": "sonnet"}))["agent"]["agent_id"]
    beta_haiku = (await _ok("POST", "/api/agents", {"name": "beta-haiku", "runtime_id": beta,
                                                    "provider_id": "prov-claude-subscription", "model": "haiku",
                                                    "effort": "low"}))["agent"]["agent_id"]
    await _ok("PUT", "/api/agent-settings/system", {"harness_prompt": "Be careful.", "agent": sonnet,
                                                    "effort": "high", "env": [{"key": "LOOPX_MODE", "value": "a"}]})
    await _ok("PUT", "/api/agent-settings/roles/worker", {"agent": "agent-claude-code-opus", "run_budget_usd": 2.5,
                                                          "env": [{"key": "LOOPX_MODE", "value": None}]})
    await _ok("PUT", "/api/agent-configs/planner", {"system_prompt": "Plan in small steps.", "timeout_seconds": 900})
    await _ok("PUT", "/api/channel-configs/channel-telegram", {"enabled": True, "config": {
        "bot_token_env": "MY_TG_TOKEN", "allowed_user_ids": ["123456789"], "webhook_secret": SECRET}})

    skill = (await _ok("POST", "/api/catalog/skills", {"name": "release-notes", "description": "Drafts notes.",
                                                       "content": "Reply NOTES-OK.\n"}))["item"]
    async with pool.acquire() as conn, conn.transaction():
        files = [{"path": "plugin.json", "content": '{"name": "kit"}\n'},
                 {"path": "bin/run.sh", "content": "#!/bin/sh\necho 中文\n", "executable": True}]
        plugin = await db_library.insert_item(conn, _item("plugin", "kit@market", files), origin="upload")
    # Imported from this machine: its source is a local path, which the file leaves out.
    await db_library.replace_snapshot([{**_item("skill", "local-only", [{"path": "SKILL.md", "content": "x\n"}]),
                                        "source": "/opt/claude/skills/local-only"}], pool=pool)
    mcp = (await _ok("POST", "/api/catalog/mcp", {"name": "search", "transport": "stdio", "command": "python3",
                                                  "args": ["server.py"], "env": {"API_KEY": SECRET}}))["item"]
    web = (await _ok("POST", "/api/catalog/mcp", {"name": "web", "transport": "http",
                                                  "url": "https://mcp.example.test/mcp",
                                                  "headers": {"Authorization": f"Bearer {SECRET}"}}))["item"]
    await _ok("POST", "/api/installs", {"item_id": skill["item_id"], "scope": "global", "roles": ["worker"]})
    await _ok("POST", "/api/installs", {"item_id": mcp["item_id"], "scope": "global", "roles": ["manager", "planner"]})
    install = await _ok("POST", "/api/installs", {"item_id": plugin["id"], "scope": "global", "roles": ["reviewer"]})
    await _ok("PUT", f"/api/installs/{install['install']['install_id']}", {"enabled": False})

    (work_dir / "docs").mkdir(parents=True)
    (work_dir / "docs" / "guide.md").write_text("# Guide\n")
    calc = (await _ok("POST", "/api/projects", {"name": "calc", "work_dir": str(work_dir), "description": "A calc.",
                                                "background": "Python 3.12.", "review_policy": "all",
                                                "distill_lessons": True}))["project_id"]
    docs = (await _ok("POST", "/api/projects", {"name": "docs"}))["project_id"]
    await _ok("PUT", f"/api/projects/{calc}/agent-settings", {"agent": beta_haiku})
    await _ok("PUT", f"/api/projects/{calc}/agent-settings/roles/worker", {"harness_prompt": "Test first."})
    from looper.agents.knowledge import add_document

    runtime_root = work_dir.parent / "runtime"
    await add_document(calc, {"source_kind": "text", "title": "Style", "summary": "How we write.",
                              "content": "Short lines.\n"}, runtime_root)
    await add_document(calc, {"source_kind": "path", "title": "Guide", "summary": "The guide.",
                              "path": "docs/guide.md"}, runtime_root)
    own_id = (await _ok("POST", f"/api/projects/{calc}/skills", {
        "name": "deploy-check", "description": "Before a deploy.", "content": "Reply DEPLOY-OK.\n"}))["skill"]["skill_id"]
    await _ok("POST", "/api/installs", {"item_id": own_id, "scope": "project", "project_id": docs,
                                        "roles": ["worker"]})
    await _ok("POST", "/api/installs", {"item_id": web["item_id"], "scope": "project", "project_id": calc,
                                        "roles": ["planner", "reviewer"]})
    return {"calc": calc, "docs": docs, "mcp": mcp["item_id"], "web": web["item_id"], "skill": skill["item_id"]}


def _item(kind: str, name: str, files: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    from looper.agents.library import item_sha256

    full = [{"executable": False, **f, "sha256": _sha(f["content"])} for f in files]
    return {"kind": kind, "name": name, "source": "", "version": "", "description": "", "files": full,
            "sha256": item_sha256(full), "size_bytes": sum(len(f["content"].encode()) for f in full),
            "file_count": len(full), **fields}


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _without_ids(view: dict[str, Any]) -> dict[str, Any]:
    """An effective settings view with its agent by name."""
    agent = view["agent"]
    return {**view, "agent": {**agent, "value": {k: v for k, v in agent["value"].items() if k != "agent_id"}}}


async def _state(pool: Any) -> dict[str, Any]:
    """An installation's configuration as agents and the dashboard see it, without ids or times."""
    from looper.agents.agent_settings import TASK_ROLES, index_layers, resolve
    from looper.agents.config_transfer import role_entry
    from looper.db import agent_setting_layers
    from looper.db import config_transfer as db

    async with pool.acquire() as conn:
        layers = await agent_setting_layers.list_layers(None, pool=pool)
        projects = [dict(r) for r in await conn.fetch("SELECT * FROM projects ORDER BY name")]
        names = {p["id"]: p["name"] for p in projects}
        items = await db.read_items(conn)
        catalog = {}
        for row in items:
            files = [(f["path"], f["sha256"], f["executable"]) for f in await db.read_item_files(conn, row["id"])]
            server = {k: row[k] for k in ("transport", "command", "args", "env", "url", "headers")}
            catalog[(row["kind"], row["name"], row["owner_project_name"])] = {
                **{k: row[k] for k in ("origin", "description", "version", "modified", "sha256", "size_bytes")},
                "files": files, "server": server}
        documents = await db.read_documents(conn, content=True)
        in_projects = {p: await agent_setting_layers.list_layers(p, pool=pool) for p in names}
        return {
            "settings": {role: _without_ids(resolve(role, index_layers(layers, None)).view()) for role in ROLES},
            "project_settings": {(names[p], role.value): _without_ids(resolve(role, index_layers(in_projects[p], p),
                                                                              p).view())
                                 for p in names for role in TASK_ROLES},
            "runtimes": [{k: r[k] for k in ("name", "type", "executable", "capabilities")}
                         for r in await db.read_runtimes(conn)],
            "providers": [{k: v for k, v in r.items() if k != "id"} for r in await db.read_providers(conn)],
            "agents": [{k: v for k, v in r.items() if k != "id"} for r in await db.read_agents(conn)],
            "roles": [role_entry(r) for r in await db.read_roles(conn)],
            "channels": {c["channel_type"]: (c["display_name"], c["enabled"], c["config"])
                         for c in await db.read_channels(conn)},
            "catalog": catalog,
            "installs": sorted((r["kind"], r["name"], r["owner_project_name"] or "", r["project_name"] or "",
                                tuple(r["roles"]), r["enabled"]) for r in await db.read_installs(conn)),
            "projects": {p["name"]: {k: v for k, v in p.items() if k not in ("id", "created_at", "updated_at")}
                         for p in projects},
            "knowledge": sorted((names[d["project_id"]], d["title"], d["summary"], d["source_kind"], d["filename"],
                                 d["path"], d["sha256"], d["content"]) for d in documents),
        }


async def _rows(pool: Any) -> dict[str, list[Any]]:
    """Every row of the tables an import writes, to tell that nothing changed."""
    tables = ("agent_setting_layers", "agent_configs", "channel_configs", "library_items", "library_files",
              "library_mcp_servers", "library_installs", "projects", "project_documents", "agent_runtimes",
              "llm_providers", "llm_provider_secrets", "agents")
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT to_regclass('library_installs') IS NOT NULL"):
            return {}
        return {t: sorted(map(repr, map(tuple, await conn.fetch(f"SELECT * FROM {t}")))) for t in tables}


def _plan(stdout: str) -> dict[str, Any]:
    return json.loads(stdout)


def _without_time(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    data.pop("exported_at")
    return data


def test_a_round_trip_into_a_new_database_reproduces_the_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test() -> None:
        async with _installation() as (source, source_url), _installation(schema=False) as (target, target_url):
            await _populate(monkeypatch, source, tmp_path / "calc")
            exported = tmp_path / "config.json"
            code, out, err = await _loopx(monkeypatch, source_url, "config", "export", str(exported),
                                          "--include-projects", "--include-secrets")
            assert code == 0, err
            assert "warning:" in err and "secret" in err

            code, out, err = await _loopx(monkeypatch, target_url, "config", "import", str(exported), "--apply",
                                          "--json")
            assert code == 0, err
            plan = _plan(out)
            assert plan["applied"] and plan["need_secret"] == [{"section": "channels", "key": "telegram",
                                                                "names": ["environment MY_TG_TOKEN"]}]
            assert not [c for c in plan["changes"] if c["action"] == "skipped"], plan

            # Agents resolve the same settings and load the same items; only the projects' directories and a
            # runtime's executable path stay behind.
            before, after = await _state(source), await _state(target)
            before["projects"]["calc"]["work_dir"] = ""
            next(r for r in before["runtimes"] if r["name"] == "claude-local")["executable"] = ""
            assert after == before
            assert [n.split(":")[0] for n in plan["notes"]] == ["runtime claude-local", "project calc"]

            # Exporting the new installation gives the same file, but for the executable path left behind.
            again = tmp_path / "again.json"
            assert (await _loopx(monkeypatch, target_url, "config", "export", str(again), "--include-projects",
                                 "--include-secrets"))[0] == 0
            first = _without_time(exported)
            local = next(r for r in first["runtimes"] if r["name"] == "claude-local")
            assert local["executable"] is None
            local["executable"] = ""  # the type's default here
            assert _without_time(again) == first

    asyncio.run(_test())


def test_importing_the_same_file_again_changes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _test() -> None:
        async with _installation() as (source, source_url), _installation() as (target, target_url):
            await _populate(monkeypatch, source, tmp_path / "calc")
            exported = tmp_path / "config.json"
            assert (await _loopx(monkeypatch, source_url, "config", "export", str(exported),
                                 "--include-projects"))[0] == 0
            # Here the Planner's recommended prompt is the prompt the file gives it, which it then follows.
            from looper.agents.serve import seed_agent_configs

            await seed_agent_configs(target)
            await target.execute("UPDATE agent_configs SET default_prompt = 'Plan in small steps.' "
                                 "WHERE agent_id = 'planner'")
            assert (await _loopx(monkeypatch, target_url, "config", "import", str(exported), "--apply"))[0] == 0
            first = await _rows(target)

            code, out, _ = await _loopx(monkeypatch, target_url, "config", "import", str(exported), "--apply",
                                        "--json")

            assert code == 0
            assert {c["action"] for c in _plan(out)["changes"]} == set()  # every entry unchanged
            assert await _rows(target) == first

    asyncio.run(_test())


@pytest.mark.parametrize("target_has_schema", [True, False], ids=["populated", "new-database"])
def test_a_preview_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target_has_schema: bool) -> None:
    async def _test() -> None:
        async with _installation() as (source, source_url), \
                _installation(schema=target_has_schema) as (target, target_url):
            await _populate(monkeypatch, source, tmp_path / "calc")
            if target_has_schema:
                _serve(monkeypatch, target)
                await _ok("PUT", "/api/agent-settings/system", {"agent": "agent-claude-code-opus"})
            exported = tmp_path / "config.json"
            assert (await _loopx(monkeypatch, source_url, "config", "export", str(exported),
                                 "--include-projects"))[0] == 0
            before = await _rows(target)

            code, out, err = await _loopx(monkeypatch, target_url, "config", "import", str(exported))

            assert code == 0, err
            assert "Nothing was written. Run again with --apply" in out
            assert "+ skill:release-notes" in out and "+ calc" in out
            assert await _rows(target) == before

    asyncio.run(_test())


def test_an_import_leaves_a_schema_about_to_change_to_serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Applying the newer schema drops columns; ``loopx serve`` backs the database up before it does."""
    async def _test() -> None:
        async with _installation() as (_, source_url), _installation() as (target, target_url):
            exported = tmp_path / "config.json"
            assert (await _loopx(monkeypatch, source_url, "config", "export", str(exported)))[0] == 0
            await target.execute("UPDATE schema_fingerprint SET sha256 = 'an older schema.sql'")
            await target.execute("ALTER TABLE agent_configs ADD COLUMN permission_mode TEXT")
            before = await _rows(target)

            code, _, err = await _loopx(monkeypatch, target_url, "config", "import", str(exported), "--apply")

            assert code == 1 and "start `loopx serve` once" in err, err
            assert await _rows(target) == before
            assert await target.fetchval("SELECT sha256 FROM schema_fingerprint") == "an older schema.sql"

    asyncio.run(_test())


def test_secrets_are_placeholders_by_default_and_never_replace_stored_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test() -> None:
        async with _installation() as (source, source_url), _installation() as (target, target_url):
            ids = await _populate(monkeypatch, source, tmp_path / "calc")
            exported = tmp_path / "config.json"
            code, out, err = await _loopx(monkeypatch, source_url, "config", "export", str(exported),
                                          "--include-projects")
            assert code == 0 and not err
            text = exported.read_text()
            assert SECRET not in text and str(tmp_path) not in text and "/opt/claude" not in text
            assert "4 secret values left out as placeholders" in out
            assert next(p for p in json.loads(text)["providers"] if p["name"] == "anthropic-key")["api_key"] == {
                "secret": "omitted"}
            data = json.loads(text)
            search = next(i for i in data["catalog"] if i["name"] == "search")
            assert search["server"]["env"] == {"API_KEY": {"secret": "omitted"}}
            assert next(i for i in data["catalog"] if i["name"] == "local-only")["source"] is None
            guide = next(d for d in data["projects"][0]["knowledge"] if d["title"] == "Guide")
            assert guide["path"] == "docs/guide.md" and guide["repoint"] is True

            # The target holds its own value for one of them, and a stale one in the file is not applied over it.
            _serve(monkeypatch, target)
            await _ok("POST", "/api/catalog/mcp", {"name": "search", "transport": "stdio", "command": "python3",
                                                   "args": ["server.py"], "env": {"API_KEY": "target-own"}})
            code, out, _ = await _loopx(monkeypatch, target_url, "config", "import", str(exported), "--apply",
                                        "--json")
            assert code == 0
            plan = _plan(out)
            from looper.db import library as db_library

            servers = {row["name"]: row for row in (await db_library.get_mcp_servers(
                [r["id"] for r in await db_library.list_items(pool=target) if r["kind"] == "mcp"],
                pool=target)).values()}
            assert json.loads(servers["search"]["env"]) == {"API_KEY": "target-own"}
            assert json.loads(servers["web"]["headers"]) == {"Authorization": ""}
            assert {(n["section"], n["key"]): n["names"] for n in plan["need_secret"]} == {
                ("channels", "telegram"): ["config webhook_secret", "environment MY_TG_TOKEN"],
                ("catalog", "mcp:web"): ["header Authorization"],
                ("providers", "anthropic-key"): ["api_key"],
            }
            # A placeholder leaves the provider without a key here; it is never shown either way.
            assert await target.fetchval("SELECT count(*) FROM llm_provider_secrets") == 0
            assert not [c for c in plan["changes"] if (c["section"], c["key"]) == ("catalog", "mcp:search")]
            assert ids  # the source's own values stay where they were
            source_servers = await db_library.get_mcp_servers([ids["mcp"]], pool=source)
            assert json.loads(source_servers[ids["mcp"]]["env"]) == {"API_KEY": SECRET}

    asyncio.run(_test())


def test_an_entry_with_the_same_key_is_updated_in_place_and_nothing_else_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test() -> None:
        async with _installation() as (source, source_url), _installation() as (target, target_url):
            await _populate(monkeypatch, source, tmp_path / "calc")
            exported = tmp_path / "config.json"
            assert (await _loopx(monkeypatch, source_url, "config", "export", str(exported),
                                 "--include-projects"))[0] == 0

            # The target already has a skill of the same name (other content, installed for the planner), a
            # project of the same name, and things the file does not mention.
            _serve(monkeypatch, target)
            mine = (await _ok("POST", "/api/catalog/skills", {"name": "release-notes", "description": "Mine.",
                                                              "content": "Reply MINE.\n"}))["item"]
            await _ok("POST", "/api/installs", {"item_id": mine["item_id"], "scope": "global", "roles": ["planner"]})
            other = (await _ok("POST", "/api/catalog/skills", {"name": "keep-me", "description": "Only here.",
                                                               "content": "Stay.\n"}))["item"]
            calc = (await _ok("POST", "/api/projects", {"name": "CALC", "work_dir": str(tmp_path),
                                                        "background": "Old."}))["project_id"]

            code, out, _ = await _loopx(monkeypatch, target_url, "config", "import", str(exported))
            assert code == 0
            assert "~ skill:release-notes (description, files)" in out
            assert "~ calc (description, background, review_policy, distill_lessons)" in out
            assert "~ skill:release-notes (roles)" in out

            assert (await _loopx(monkeypatch, target_url, "config", "import", str(exported), "--apply"))[0] == 0
            items = {(r["kind"], r["name"]): r for r in await _items(target)}
            assert items[("skill", "release-notes")]["id"] == mine["item_id"]
            assert items[("skill", "release-notes")]["description"] == "Drafts notes."
            assert ("skill", "keep-me") in items and items[("skill", "keep-me")]["id"] == other["item_id"]
            _, installs = await _call("GET", f"/api/catalog/{mine['item_id']}")
            assert [(i["scope"], i["roles"]) for i in installs["item"]["installs"]] == [("global", ["worker"])]
            _, project = await _call("GET", f"/api/projects/{calc}/agent-settings")
            state = await _state(target)
            assert state["projects"]["CALC"]["background"] == "Python 3.12."
            assert state["projects"]["CALC"]["work_dir"] == str(tmp_path)  # a directory is never imported
            assert project["project"]["values"]["agent"] == next(
                a["agent_id"] for a in (await _call("GET", "/api/registry"))[1]["agents"] if a["name"] == "beta-haiku")

    asyncio.run(_test())


async def _items(pool: Any) -> list[dict[str, Any]]:
    from looper.db import library as db_library

    return await db_library.list_items(pool=pool)


def test_an_export_over_the_request_cap_goes_through_the_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.http_server import REQUEST_BODY_MAX_BYTES

    async def _test() -> None:
        from looper.db import library as db_library

        async with _installation() as (source, source_url), _installation(schema=False) as (target, target_url):
            # Five skills of 1.9 MiB each, like a real catalog's larger ones.
            body = ("A line of a long reference document, 中文 included.\n" * 40_000)[: 1_900_000]
            async with source.acquire() as conn, conn.transaction():
                for n in range(5):
                    await db_library.insert_item(conn, _item("skill", f"big-{n}", [
                        {"path": "SKILL.md", "content": f"---\nname: big-{n}\n---\n"},
                        {"path": "reference.md", "content": body}]), origin="upload")
            exported = tmp_path / "big.json"
            assert (await _loopx(monkeypatch, source_url, "config", "export", str(exported)))[0] == 0
            assert exported.stat().st_size > REQUEST_BODY_MAX_BYTES

            code, out, err = await _loopx(monkeypatch, target_url, "config", "import", str(exported), "--apply")

            assert code == 0, err
            assert "catalog            added 5" in out
            assert (await _state(target))["catalog"] == (await _state(source))["catalog"]

    asyncio.run(_test())


def test_the_settings_download_streams_the_same_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _test() -> None:
        from looper.agents.http_server import AgentHTTPServer
        from looper.agents.tiers.task import TaskRouter
        from types import SimpleNamespace

        async with _installation() as (source, source_url):
            await _populate(monkeypatch, source, tmp_path / "calc")
            exported = tmp_path / "config.json"
            assert (await _loopx(monkeypatch, source_url, "config", "export", str(exported),
                                 "--include-projects"))[0] == 0
            server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                     task_router=TaskRouter(runtime_root=tmp_path / "runtime"))
            writer = _Writer()
            await server._route_api("GET", "/api/config/export", {"projects": "1"}, b"", writer)

            head, _, payload = writer.data.partition(b"\r\n\r\n")
            assert head.startswith(b"HTTP/1.1 200") and b"Content-Length" not in head
            assert b'Content-Disposition: attachment; filename="loopx-config-' in head
            downloaded = json.loads(payload)
            downloaded.pop("exported_at")
            assert downloaded == _without_time(exported)
            assert SECRET.encode() not in payload

    asyncio.run(_test())


def test_a_file_from_before_agents_selects_the_agent_of_each_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test() -> None:
        async with _installation(schema=False) as (target, target_url):
            old = tmp_path / "old.json"
            old.write_text(json.dumps({"format": "loopx-config/v1", "settings": {"layers": [
                {"scope": "system", "harness_prompt": None, "model": "haiku", "effort": "low", "env": [],
                 "run_budget_usd": None},
                {"scope": "role", "role": "manager", "harness_prompt": None, "model": "opus", "effort": None,
                 "env": [], "run_budget_usd": None}]}}))

            code, out, err = await _loopx(monkeypatch, target_url, "config", "import", str(old), "--apply", "--json")

            assert code == 0, err
            plan = _plan(out)
            assert [(c["section"], c["key"], c["action"]) for c in plan["changes"]] == [
                ("agents", "claude-code/haiku", "added"), ("settings.layers", "system", "added"),
                ("settings.layers", "role:manager", "added")]
            from looper.db import config_transfer as db

            async with target.acquire() as conn:
                layers = {r["scope"]: r["agent"] for r in await db.read_layers(conn)}
            assert layers == {"system": "claude-code/haiku", "role": "claude-code/opus"}  # opus: the built-in one

    asyncio.run(_test())
