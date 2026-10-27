"""Configuration export and import core against a real PostgreSQL.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in new schemas of its own (one per installation), dropped
afterwards; the transfer core reaches a schema through the URL's ``search_path``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, fresh_schema
from tests.test_catalog_db import _call, _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

SECRET = "sk-test-secret-0123456789"
ROLES = ("manager", "planner", "worker", "reviewer")


def _documented_v3() -> dict[str, Any]:
    """Explicit current data until the documentation card updates the architecture example."""
    return {"format": "looper-config/v4", "providers": [{
        "name": "Example account", "vendor_type": "custom", "auth": "api_key",
        "endpoints": {"anthropic-messages": {"base_url": "https://example.invalid"}},
    }], "models": [{"provider": "Example account", "name": "exact-model", "protocol": "anthropic-messages"}],
        "profiles": [{"name": "Example profile", "runtime": "claude-code", "model": {
            "provider": "Example account", "name": "exact-model", "protocol": "anthropic-messages"}}]}


def test_documented_v3_example_previews_applies_and_reads_back(monkeypatch, tmp_path):
    async def test():
        data = _documented_v3()
        path = tmp_path / "documented.json"
        path.write_text(json.dumps(data))
        async with _installation() as (pool, url):
            before = await _rows(pool)
            code, out, err = await _transfer_file(url, "import", str(path), json_output=True)
            assert code == 0, err
            preview = _plan(out)
            assert not preview["applied"]
            assert await _rows(pool) == before
            assert not any(section.get("skipped", 0) for section in preview["counts"].values())
            for section in ("providers", "models", "profiles"):
                assert preview["counts"][section]["added"] == 1
            code, out, err = await _transfer_file(url, "import", str(path), apply=True, json_output=True)
            assert code == 0, err
            assert _plan(out)["applied"]
            _serve(monkeypatch, pool)
            registry = await _ok("POST", "/api/v1/registry/view", {})
            profile = next(p for p in registry["profiles"] if p["name"] == data["profiles"][0]["name"])
            model = next(m for m in registry["models"] if m["model_id"] == profile["model_id"])
            assert (model["provider_name"], model["name"], model["protocol"]) == tuple(
                data["profiles"][0]["model"][key] for key in ("provider", "name", "protocol"))
            assert profile["model"]["protocol"] == model["protocol"]
            assert "system_prompt_mode" not in profile
            # An unknown price is NULL, never a zero-price override.
            assert all(price == {"override": None, "effective": None, "source": "unknown"}
                       for price in model["prices"].values())
            exported = tmp_path / "exported.json"
            code, _, err = await _transfer_file(url, "export", str(exported))
            assert code == 0, err
            saved = json.loads(exported.read_text())
            assert saved["format"] == "looper-config/v4"
            assert saved["profiles"][0]["model"] == data["profiles"][0]["model"]
            code, out, err = await _transfer_file(url, "import", str(path), json_output=True)
            assert code == 0, err
            assert all(counts["added"] == counts["changed"] == counts["skipped"] == 0
                       for counts in _plan(out)["counts"].values())

    asyncio.run(asyncio.wait_for(test(), timeout=30))


@pytest.fixture(autouse=True)
def _isolated_registry_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import profile_registry

    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})


@contextlib.asynccontextmanager
async def _installation(*, schema: bool = True) -> AsyncIterator[tuple[Any, str]]:
    """A new schema (with Looper's tables unless ``schema`` is False): its pool and a URL that reaches it."""
    from looper.db.connection import ensure_schema

    async with fresh_schema() as pool:
        name = await pool.fetchval("SHOW search_path")
        parts = urllib.parse.urlsplit(DSN)
        query = urllib.parse.urlencode([*urllib.parse.parse_qsl(parts.query), ("search_path", name)])
        if schema:
            await ensure_schema(pool)
        yield pool, urllib.parse.urlunsplit(parts._replace(query=query))


async def _transfer_file(
    url: str, action: str, path: str, *, include_projects: bool = False,
    include_secrets: bool = False, apply: bool = False, json_output: bool = False,
) -> tuple[int, str, str]:
    """Exercise file transfer directly; command-line behavior is tested at its own entry point."""
    import asyncpg

    from looper.agents import config_transfer

    file = Path(path)
    try:
        if action == "export":
            summary = await config_transfer.export_to_file(
                url, file, include_projects=include_projects, include_secrets=include_secrets)
            return 0, config_transfer.format_export(summary, file, include_secrets=include_secrets), ""
        assert action == "import"
        plan = await config_transfer.import_file(url, file, apply=apply)
        output = json.dumps(plan.view(), ensure_ascii=False) if json_output else config_transfer.format_plan(plan, file)
        return 0, output, ""
    except (config_transfer.ConfigFileError, config_transfer.SchemaChangePending, OSError, asyncpg.PostgresError) as exc:
        return 1, "", f"error: {exc}"


def _serve(monkeypatch: pytest.MonkeyPatch, pool: Any) -> None:
    """Make ``pool`` the server's, for the API calls that set an installation up."""
    from looper.db import connection

    monkeypatch.setattr(connection, "_pool", pool)


async def _ok(method: str, path: str, body: Any = None) -> Any:
    if path.startswith(("/api/v1/role_templates/", "/api/v1/channel_configs/")):
        from types import SimpleNamespace

        from looper.agents.ops.clients import ClientOps
        from tests.db_support import call_v1
        from tests.test_task_agents_db import _server

        server = _server()
        server._client_ops = ClientOps(SimpleNamespace())
        status, payload = await call_v1(server, path, body)
    else:
        status, payload = await _call(method, path, body)
    assert status in (200, 201), (path, payload)
    return payload


async def _populate(monkeypatch: pytest.MonkeyPatch, pool: Any, work_dir: Path) -> dict[str, str]:
    """An installation using every section of the file, set up through the API as a user would."""
    from looper.agents.serve import seed_role_configs, seed_channel_configs
    from looper.db import library as db_library

    _serve(monkeypatch, pool)
    await seed_role_configs(pool)
    await seed_channel_configs(pool)
    # Providers use current protocol endpoints and profiles select seeded model ids.
    await _ok("POST", "/api/v1/provider_accounts/create", {"name": "anthropic-key", "vendor_type": "custom",
                                         "api_key": SECRET, "endpoints": [
                                             {"protocol": "anthropic-messages", "base_url": "https://api.example.test"}]})
    provider = await pool.fetchval("SELECT id FROM llm_providers WHERE name = 'anthropic-key'")
    await _ok("POST", "/api/v1/ops/models/create", {"provider": provider, "name": "claude-sonnet-4-5",
                                     "protocol": "anthropic-messages", "input_usd_per_mtok": 3,
                                     "output_usd_per_mtok": 15})
    # Login suggestions are seeded models; profiles select their existing ids.
    login_models = {row["name"]: row["id"] for row in await pool.fetch(
        "SELECT id, name FROM llm_models WHERE provider_id = $1 AND protocol = $2",
        "prov-claude-subscription", "anthropic-messages")}
    sonnet_model, haiku_model = login_models["sonnet"], login_models["haiku"]
    sonnet = (await _ok("POST", "/api/agent-profiles", {"name": "claude-code/sonnet", "runtime_id": "rt-claude-code",
                                                "model_id": sonnet_model, "effort": "high", "agent_teams": True}))["profile"][
        "profile_id"]
    beta_haiku = (await _ok("POST", "/api/agent-profiles", {"name": "beta-haiku", "runtime_id": "rt-claude-code",
                                                    "model_id": haiku_model,
                                                            "effort": "low"}))["profile"]["profile_id"]
    await _ok("PUT", "/api/agent-settings/system", {"harness_prompt": "Be careful.", "profile_id": sonnet,
                                                    "env": [{"key": "LOOPER_MODE", "value": "a"}]})
    await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": "agent-claude-code-opus", "run_budget_usd": 2.5,
                                                          "env": [{"key": "LOOPER_MODE", "value": None}],
                                                          "max_concurrency": 6})
    await _ok("PUT", "/api/roles/planner", {"system_prompt": "Plan in small steps.", "timeout_seconds": 900})
    await _ok("POST", "/api/v1/channel_configs/update", {"channel_id": "channel-telegram", "fields": {"enabled": True, "config": {
        "bot_token_env": "MY_TG_TOKEN", "allowed_user_ids": ["123456789"]}}})
    # An older installation may still hold an extension secret; export must mask it.
    await pool.execute("UPDATE channel_configs SET config = config || $1::jsonb WHERE channel_id = 'channel-telegram'",
                       json.dumps({"webhook_secret": SECRET}))

    skill = (await _ok("POST", "/api/v1/catalog_sources/create_skill", {"name": "release-notes", "description": "Drafts notes.",
                                                       "content": "Reply NOTES-OK.\n"}))["item"]
    async with pool.acquire() as conn, conn.transaction():
        files = [{"path": "plugin.json", "content": '{"name": "kit"}\n'},
                 {"path": "bin/run.sh", "content": "#!/bin/sh\necho 中文\n", "executable": True}]
        plugin = await db_library.insert_item(conn, _item("plugin", "kit@market", files), origin="upload")
    # Imported from this machine: its source is a local path, which the file leaves out.
    await db_library.replace_snapshot([{**_item("skill", "local-only", [{"path": "SKILL.md", "content": "x\n"}]),
                                        "source": "/opt/claude/skills/local-only"}], pool=pool)
    mcp = (await _ok("POST", "/api/v1/catalog_sources/create_mcp", {"name": "search", "transport": "stdio", "command": "python3",
                                                  "args": ["server.py"], "env": {"API_KEY": SECRET}}))["item"]
    web = (await _ok("POST", "/api/v1/catalog_sources/create_mcp", {"name": "web", "transport": "http",
                                                  "url": "https://mcp.example.test/mcp",
                                                  "headers": {"Authorization": f"Bearer {SECRET}"}}))["item"]
    await _ok("POST", "/api/v1/installs/create", {"item_id": skill["item_id"], "scope": "global", "roles": ["worker"]})
    await _ok("POST", "/api/v1/installs/create", {"item_id": mcp["item_id"], "scope": "global", "roles": ["manager", "planner"]})
    install = await _ok("POST", "/api/v1/installs/create", {"item_id": plugin["id"], "scope": "global", "roles": ["reviewer"]})
    await _ok("POST", "/api/v1/installs/update", {"install_id": install['install']['install_id'], "enabled": False})

    (work_dir / "docs").mkdir(parents=True)
    (work_dir / "docs" / "guide.md").write_text("# Guide\n")
    calc = (await _ok("POST", "/api/v1/project_admin/create", {"name": "calc", "work_dir": str(work_dir), "description": "A calc.",
                                                "background": "Python 3.12.", "review_policy": "all",
                                                "distill_lessons": True}))["project_id"]
    docs = (await _ok("POST", "/api/v1/project_admin/create", {"name": "docs"}))["project_id"]
    await _ok("PUT", f"/api/projects/{calc}/agent-settings", {"profile_id": beta_haiku})
    await _ok("PUT", f"/api/projects/{calc}/agent-settings/roles/worker", {"harness_prompt": "Test first."})
    from looper.agents.knowledge import add_document

    runtime_root = work_dir.parent / "runtime"
    await add_document(calc, {"source_kind": "text", "title": "Style", "summary": "How we write.",
                              "content": "Short lines.\n"}, runtime_root)
    await add_document(calc, {"source_kind": "path", "title": "Guide", "summary": "The guide.",
                              "path": "docs/guide.md"}, runtime_root)
    own_id = (await _ok("POST", "/api/v1/project_skills/add", {"project_id": calc, "skill": {
        "name": "deploy-check", "description": "Before a deploy.", "content": "Reply DEPLOY-OK.\n"}}))["skill"]["skill_id"]
    await _ok("POST", "/api/v1/installs/create", {"item_id": own_id, "scope": "project", "project_id": docs,
                                        "roles": ["worker"]})
    await _ok("POST", "/api/v1/installs/create", {"item_id": web["item_id"], "scope": "project", "project_id": calc,
                                        "roles": ["planner", "reviewer", "task_role"]})
    await _ok("PUT", f"/api/projects/{calc}/allowed-profiles", {"profile_ids": [sonnet]})
    await _ok("POST", "/api/v1/role_templates/put", {"project_id": calc, "key": "security",
        "kind": "review", "description": "Steps that touch auth.", "role_prompt": "Check for security problems.",
        "harness": "## How you work\nRead first.", "profile_id": sonnet, "items": [web["item_id"]]})
    await _ok("POST", "/api/v1/role_templates/put", {"project_id": calc, "key": "tester",
                "kind": "work", "role_prompt": "Write tests.", "description": "", "harness": None,
                "profile_id": None, "items": []})
    # Schedules: an enabled one with every setting, a directory of its own and a chat the allowlist lets in; a
    # disabled one on the defaults.
    nightly = (await _ok("POST", "/api/v1/schedule_admin/create", {"project_id": calc, "fields": {
        "name": "nightly", "cron": "0 2 * * *", "timezone": "Asia/Shanghai", "requirement": "Check the deps.",
        "work_dir": str(work_dir), "overlap": "parallel", "max_parallel_runs": 2, "catch_up": "skip",
        "run_timeout_seconds": 1800, "run_budget_usd": 1.5, "gate_command": "echo new", "gate_timeout_seconds": 30,
        "daily_budget_usd": 3, "notify": "always", "notify_target": {"channel": "telegram", "chat": "123456789"},
        "profile_overrides": {"worker": "claude-code/sonnet"}, "task_limits": {"worker": 2}}}))["schedule"]
    await _ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": nightly['schedule_id'], "fields": {"enabled": True}})
    await _ok("POST", "/api/v1/schedule_admin/create", {"project_id": calc, "fields": {"name": "weekly", "cron": "0 9 * * 1",
                                                         "timezone": "UTC", "requirement": "Sum up the week."}})
    return {"calc": calc, "docs": docs, "mcp": mcp["item_id"], "web": web["item_id"], "skill": skill["item_id"],
            "nightly": nightly["schedule_id"]}


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
    """An effective settings view with its Agent Profile by name."""
    profile = view["profile"]
    return {**view, "profile": {**profile, "value": {k: v for k, v in profile["value"].items() if k != "profile_id"}}}


async def _state(pool: Any) -> dict[str, Any]:
    """An installation's configuration as agents and the dashboard see it, without ids or times."""
    from looper.agents.agent_settings import TASK_LIMIT_ROLES, TASK_ROLES, default_concurrency, index_layers, resolve
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
        templates = await db.read_role_templates(conn)
        item_names = {row["id"]: (row["kind"], row["name"]) for row in items}
        in_projects = {p: await agent_setting_layers.list_layers(p, pool=pool) for p in names}
        from looper.db.schedules import list_schedules

        profile_names = {r["id"]: r["name"] for r in await db.read_profiles(conn)}
        schedules = {(r["project_name"], r["name"]): {
            **{k: r[k] for k in (*_SCHEDULE_COLUMNS, "work_dir", "enabled", "created_via")},
            "profiles": {role: profile_names[pid] for role, pid in r["profile_overrides"].items()},
            "task_limits": r["task_limits"]} for r in await list_schedules(pool=conn)}
        return {
            "settings": {role: _without_ids(resolve(role, index_layers(layers, None)).view()) for role in ROLES},
            "project_settings": {(names[p], role.value): _without_ids(resolve(role, index_layers(in_projects[p], p),
                                                                              p).view())
                                 for p in names for role in TASK_ROLES},
            "concurrency": {role.value: default_concurrency(role, index_layers(layers, None)).view()
                            for role in TASK_LIMIT_ROLES},
            "runtimes": [{k: r[k] for k in ("name", "type", "executable", "capabilities")}
                         for r in await db.read_runtimes(conn)],
            "providers": [{**{k: v for k, v in r.items() if k not in ("id", "endpoints")},
                           "endpoints": [{k: v for k, v in e.items() if k != "provider_id"}
                                         for e in r["endpoints"]]} for r in await db.read_providers(conn)],
            "models": [{k: v for k, v in r.items() if k not in ("id", "provider_id", "created_at", "updated_at")}
                       for r in await db.read_models(conn)],
            "profiles": [{k: v for k, v in r.items() if k != "id"} for r in await db.read_profiles(conn)],
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
            "schedules": schedules,
            "role_templates": sorted((names[t["project_id"]], t["template_key"], t["kind"], t["description"],
                                      t["role_prompt"], t["harness"] or "", t["profile"] or "",
                                      tuple(item_names[i] for i in t["items"])) for t in templates),
        }


# A schedule's own settings, as its row has them.
_SCHEDULE_COLUMNS = ("cron", "timezone", "requirement", "overlap", "max_parallel_runs", "catch_up",
                     "run_timeout_mode", "run_timeout_seconds", "run_budget_usd", "gate_command",
                     "gate_timeout_seconds", "daily_budget_usd", "notify",
                     "notify_channel", "notify_chat")


async def _rows(pool: Any) -> dict[str, list[Any]]:
    """Every row of the tables an import writes, to tell that nothing changed."""
    tables = ("agent_setting_layers", "role_configs", "channel_configs", "library_items", "library_files",
              "library_mcp_servers", "library_installs", "projects", "project_documents", "agent_runtimes",
              "llm_providers", "llm_provider_endpoints", "llm_models", "llm_provider_secrets", "agent_profiles",
              "project_role_templates", "schedules",
              "schedule_profile_overrides", "schedule_task_limits")
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT to_regclass('library_installs') IS NOT NULL"):
            return {}
        return {t: sorted(map(repr, map(tuple, await conn.fetch(f"SELECT * FROM {t}")))) for t in tables}


def _by_name(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(e for e in entries if e["name"] == name)


def _plan(stdout: str) -> dict[str, Any]:
    return json.loads(stdout)


def _without_time(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    data.pop("exported_at")
    return data


def test_configuration_export_excludes_api_session_state(tmp_path: Path) -> None:
    async def run():
        from looper.db import agent_sessions, api_sessions

        async with _installation() as (pool, url):
            await api_sessions.create("runtime-only-session", "openai-responses", pool=pool)
            await api_sessions.append("runtime-only-session", 1, "openai-responses", "runtime-only-model", [
                {"version": 1, "kind": "message", "role": "user", "text": "runtime-only-history"}], pool=pool)
            await agent_sessions.save_session("manager", "runtime-only-session", {}, runtime_type="api", pool=pool)
            exported = tmp_path / "configuration.json"
            code, _, err = await _transfer_file(url, "export", str(exported), include_projects=True)
            assert code == 0, err
            raw = exported.read_text()
            assert json.loads(raw)["format"] == "looper-config/v4"
            assert all(value not in raw for value in (
                "api_sessions", "api_session_items", "runtime-only-session", "runtime-only-model", "runtime-only-history"))

    asyncio.run(asyncio.wait_for(run(), timeout=15))


@pytest.mark.usefixtures("lark_cli_stand_in")
def test_a_round_trip_into_a_new_database_reproduces_the_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test() -> None:
        async with _installation() as (source, source_url), _installation(schema=False) as (target, target_url):
            await _populate(monkeypatch, source, tmp_path / "calc")
            exported = tmp_path / "config.json"
            code, out, err = await _transfer_file(source_url, "export", str(exported), include_projects=True, include_secrets=True)
            assert code == 0, err

            code, out, err = await _transfer_file(target_url, "import", str(exported), apply=True, json_output=True)
            assert code == 0, err
            plan = _plan(out)
            assert plan["applied"] and plan["need_secret"] == [{"section": "channels", "key": "channel-telegram",
                                                                "names": ["environment MY_TG_TOKEN", "bot token (Settings → Channels)"]}]
            assert not [c for c in plan["changes"] if c["action"] == "skipped"], plan

            # Agents resolve the same settings and load the same items; only the projects' and the schedules'
            # directories stay behind and the schedules come in disabled.
            before, after = await _state(source), await _state(target)
            before["projects"]["calc"]["work_dir"] = ""
            for schedule in before["schedules"].values():
                schedule.update(work_dir="", enabled=False, created_via="import")
            assert after == before
            assert after["schedules"][("calc", "nightly")]["notify_chat"] == "123456789"
            assert [n.split(":")[0] for n in plan["notes"]] == [
                "project calc", "2 schedule(s) come in disabled (calc / nightly, calc / weekly)", "calc / nightly"]

            # Exporting the new installation gives the same portable configuration.
            again = tmp_path / "again.json"
            assert (await _transfer_file(target_url, "export", str(again), include_projects=True, include_secrets=True))[0] == 0
            first = _without_time(exported)
            assert first["runtimes"] == []
            # Here the schedule runs in its project's directory: it has none of its own to re-point.
            assert _by_name(_by_name(first["projects"], "calc")["schedules"], "nightly").pop("repoint") is True
            assert _without_time(again) == first

    asyncio.run(_test())


@pytest.mark.usefixtures("lark_cli_stand_in")
def test_importing_the_same_file_again_changes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _test() -> None:
        async with _installation() as (source, source_url), _installation() as (target, target_url):
            await _populate(monkeypatch, source, tmp_path / "calc")
            exported = tmp_path / "config.json"
            assert (await _transfer_file(source_url, "export", str(exported), include_projects=True))[0] == 0
            # Here the Planner's recommended prompt is the prompt the file gives it, which it then follows.
            from looper.agents.serve import seed_role_configs

            await seed_role_configs(target)
            await target.execute("UPDATE role_configs SET default_prompt = 'Plan in small steps.' "
                                 "WHERE role = 'planner'")
            assert (await _transfer_file(target_url, "import", str(exported), apply=True))[0] == 0
            first = await _rows(target)

            code, out, _ = await _transfer_file(target_url, "import", str(exported), apply=True, json_output=True)

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
                await _ok("PUT", "/api/agent-settings/system", {"profile_id": "agent-claude-code-opus"})
            exported = tmp_path / "config.json"
            assert (await _transfer_file(source_url, "export", str(exported), include_projects=True))[0] == 0
            before = await _rows(target)

            code, out, err = await _transfer_file(target_url, "import", str(exported))

            assert code == 0, err
            assert "Nothing was written. Run again with --apply" in out
            assert "+ skill:release-notes" in out and "+ calc" in out
            assert await _rows(target) == before

    asyncio.run(_test())


def test_an_import_leaves_a_schema_about_to_change_to_serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Applying the newer schema drops columns; ``looper serve`` backs the database up before it does."""
    async def _test() -> None:
        async with _installation() as (_, source_url), _installation() as (target, target_url):
            exported = tmp_path / "config.json"
            assert (await _transfer_file(source_url, "export", str(exported)))[0] == 0
            await target.execute("UPDATE schema_fingerprint SET sha256 = 'an older schema.sql'")
            await target.execute("ALTER TABLE role_configs ADD COLUMN permission_mode TEXT")
            before = await _rows(target)

            code, _, err = await _transfer_file(target_url, "import", str(exported), apply=True)

            assert code == 1 and "start `looper serve` once" in err, err
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
            code, out, err = await _transfer_file(source_url, "export", str(exported), include_projects=True)
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
            await _ok("POST", "/api/v1/catalog_sources/create_mcp", {"name": "search", "transport": "stdio", "command": "python3",
                                                   "args": ["server.py"], "env": {"API_KEY": "target-own"}})
            code, out, _ = await _transfer_file(target_url, "import", str(exported), apply=True, json_output=True)
            assert code == 0
            plan = _plan(out)
            from looper.db import library as db_library

            servers = {row["name"]: row for row in (await db_library.get_mcp_servers(
                [r["id"] for r in await db_library.list_items(pool=target) if r["kind"] == "mcp"],
                pool=target)).values()}
            assert json.loads(servers["search"]["env"]) == {"API_KEY": "target-own"}
            assert json.loads(servers["web"]["headers"]) == {"Authorization": ""}
            assert {(n["section"], n["key"]): n["names"] for n in plan["need_secret"]} == {
                ("channels", "channel-telegram"): ["config webhook_secret", "environment MY_TG_TOKEN", "bot token (Settings → Channels)"],
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
            assert (await _transfer_file(source_url, "export", str(exported), include_projects=True))[0] == 0

            # The target already has a skill of the same name (other content, installed for the planner), a
            # project of the same name, and things the file does not mention.
            _serve(monkeypatch, target)
            mine = (await _ok("POST", "/api/v1/catalog_sources/create_skill", {"name": "release-notes", "description": "Mine.",
                                                              "content": "Reply MINE.\n"}))["item"]
            await _ok("POST", "/api/v1/installs/create", {"item_id": mine["item_id"], "scope": "global", "roles": ["planner"]})
            other = (await _ok("POST", "/api/v1/catalog_sources/create_skill", {"name": "keep-me", "description": "Only here.",
                                                               "content": "Stay.\n"}))["item"]
            calc = (await _ok("POST", "/api/v1/project_admin/create", {"name": "CALC", "work_dir": str(tmp_path),
                                                        "background": "Old."}))["project_id"]

            code, out, _ = await _transfer_file(target_url, "import", str(exported))
            assert code == 0
            assert "~ skill:release-notes (description, files)" in out
            assert "~ calc (description, background, review_policy, distill_lessons)" in out
            assert "~ skill:release-notes (roles)" in out

            assert (await _transfer_file(target_url, "import", str(exported), apply=True))[0] == 0
            items = {(r["kind"], r["name"]): r for r in await _items(target)}
            assert items[("skill", "release-notes")]["id"] == mine["item_id"]
            assert items[("skill", "release-notes")]["description"] == "Drafts notes."
            assert ("skill", "keep-me") in items and items[("skill", "keep-me")]["id"] == other["item_id"]
            _, installs = await _call("POST", "/api/v1/catalog_items/get", {"item_id": mine['item_id']})
            assert [(i["scope"], i["roles"]) for i in installs["item"]["installs"]] == [("global", ["worker"])]
            _, project = await _call("GET", f"/api/projects/{calc}/agent-settings")
            state = await _state(target)
            assert state["projects"]["CALC"]["background"] == "Python 3.12."
            assert state["projects"]["CALC"]["work_dir"] == str(tmp_path)  # a directory is never imported
            assert project["project"]["values"]["profile_id"] == next(
                a["profile_id"] for a in (await _call("POST", "/api/v1/registry/view", {}))[1]["profiles"]
                if a["name"] == "beta-haiku")

    asyncio.run(_test())


async def _items(pool: Any) -> list[dict[str, Any]]:
    from looper.db import library as db_library

    return await db_library.list_items(pool=pool)


def test_file_transfer_accepts_an_export_over_the_http_request_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
            assert (await _transfer_file(source_url, "export", str(exported)))[0] == 0
            assert exported.stat().st_size > REQUEST_BODY_MAX_BYTES

            code, out, err = await _transfer_file(target_url, "import", str(exported), apply=True)

            assert code == 0, err
            assert "catalog            added 5" in out
            assert (await _state(target))["catalog"] == (await _state(source))["catalog"]

    asyncio.run(_test())


def test_the_settings_download_streams_the_same_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _test() -> None:
        from looper.agents.http_server import AgentHTTPServer
        from looper.agents.ops.clients import ClientOps
        from tests.test_api_v1_auth import _bearer, _send_raw
        from tests.test_operator_auth import TOKEN
        from types import SimpleNamespace

        async with _installation() as (source, source_url):
            await _populate(monkeypatch, source, tmp_path / "calc")
            exported = tmp_path / "config.json"
            assert (await _transfer_file(source_url, "export", str(exported), include_projects=True))[0] == 0
            server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                                     task_router=SimpleNamespace(), operator_token=TOKEN)
            server._client_ops = ClientOps(SimpleNamespace())
            status, headers, payload = await _send_raw(server, "POST /api/v1/config/export", _bearer(TOKEN),
                                                       json.dumps({"include_projects": True}))

            assert status == 200 and "content-length" not in headers
            assert headers["content-type"].startswith("application/json")
            envelope = json.loads(payload)
            assert envelope["status"] == "ok"
            downloaded = envelope["result"]["document"]
            downloaded.pop("exported_at")
            assert downloaded == _without_time(exported)
            assert SECRET not in payload

    asyncio.run(_test())


def test_a_profile_without_agent_teams_imports_with_its_runtimes_default_and_another_runtime_cannot_have_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test() -> None:
        async with _installation() as (target, target_url):
            _serve(monkeypatch, target)
            await _ok("POST", "/api/agent-profiles", {"name": "cc/off", "runtime_id": "rt-claude-code",
                                                      "model_id": await target.fetchval(
                                                          "SELECT id FROM llm_models WHERE provider_id = $1 "
                                                          "AND name = $2 AND protocol = $3",
                                                          "prov-claude-subscription", "sonnet", "anthropic-messages"),
                                                      "agent_teams": False})
            await target.execute("INSERT INTO agent_runtimes (id, name, type, capabilities, builtin) "
                                 "VALUES ('rt-codex', 'codex', 'codex', $1, true)", ["resident_session", "resume", "run"])
            path = tmp_path / "config.json"
            path.write_text(json.dumps({"format": "looper-config/v4", "providers": [
                {"name": "cpa", "vendor_type": "custom", "auth": "api_key", "oauth_runtime_type": None,
                 "endpoints": {"openai-responses": {"base_url": "https://example.invalid/v1"}},
                 "env": [], "max_concurrency": None, "api_key": None}],
                "models": [{"provider": "cpa", "name": "gpt-5.5", "protocol": "openai-responses"}],
                "profiles": [{"name": "cc/off", "runtime": "claude-code", "model": {"provider": "claude-subscription",
                              "name": "sonnet", "protocol": "anthropic-messages"}, "effort": None},
                             {"name": "codex/gpt", "runtime": "codex", "model": {"provider": "cpa", "name": "gpt-5.5", "protocol": "openai-responses"},
                              "effort": None},
                             {"name": "codex/teams", "runtime": "codex", "model": {"provider": "cpa", "name": "gpt-5.5", "protocol": "openai-responses"},
                              "effort": None, "agent_teams": True}]}))

            code, out, err = await _transfer_file(target_url, "import", str(path), apply=True, json_output=True)

            assert code == 0, err
            changes = {(c["section"], c["key"]): c for c in _plan(out)["changes"]}
            # Without the field: on for Claude Code, off for Codex.
            assert (changes[("profiles", "cc/off")]["action"], changes[("profiles", "cc/off")]["fields"]) == (
                "changed", ["agent_teams"])
            assert changes[("profiles", "codex/gpt")]["action"] == "added"
            assert changes[("profiles", "codex/teams")]["action"] == "skipped"
            assert "agent teams are a Claude Code feature" in changes[("profiles", "codex/teams")]["reason"]
            rows = await target.fetch("SELECT name, agent_teams FROM agent_profiles WHERE NOT builtin")
            assert {r["name"]: r["agent_teams"] for r in rows} == {"cc/off": True, "codex/gpt": False}

    asyncio.run(_test())


def test_a_projects_role_templates_import_checked_and_the_ones_the_file_does_not_give_stay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A template is checked as the dashboard checks one: a bad one is skipped with the reason, a profile that is
    not here is left out with a note, a template already here is changed in place, and one the file does not give
    stays. A project without templates exports none."""
    async def _test() -> None:
        async with _installation() as (target, target_url):
            _serve(monkeypatch, target)
            calc = (await _ok("POST", "/api/v1/project_admin/create", {"name": "calc"}))["project_id"]
            await _ok("POST", "/api/v1/role_templates/put", {"project_id": calc, "key": "mine",
                "kind": "work", "role_prompt": "Mine.", "description": "", "harness": None,
                "profile_id": None, "items": []})
            await _ok("POST", "/api/v1/role_templates/put", {"project_id": calc, "key": "tester",
                "kind": "work", "role_prompt": "Old.", "description": "", "harness": None,
                "profile_id": None, "items": []})
            path = tmp_path / "config.json"
            good = {"kind": "review", "description": "Auth steps.", "role_prompt": "Check security."}
            path.write_text(json.dumps({"format": "looper-config/v4", "projects": [
                {"name": "calc", "description": "", "background": "", "execution": {"role_text_max_chars": 500},
                 "role_templates": [
                     {**good, "key": "security", "profile": "claude-code/nowhere",
                      "items": [{"kind": "skill", "name": "nowhere"}]},
                     {"key": "tester", "kind": "work", "role_prompt": "Write tests.", "harness": "## How\nTDD."},
                     {**good, "key": "security"},
                     {**good, "key": "Worker"},
                     {**good, "key": "long", "role_prompt": "x" * 501},
                     {**good, "key": "plan", "kind": "plan"},
                     {**good, "key": "empty", "role_prompt": ""},
                     {**good, "key": "wordy", "description": "x" * 301},
                     "not an object",
                 ]},
                {"name": "docs", "description": "", "background": "", "execution": {}, "role_templates": {"security": good}},
            ]}))

            code, out, err = await _transfer_file(target_url, "import", str(path), apply=True, json_output=True)

            assert code == 0, err
            plan = _plan(out)
            changes = [c for c in plan["changes"] if c["section"] == "project.role_templates"]
            assert [(c["key"], c["action"], c.get("fields", [])) for c in changes if c["action"] != "skipped"] == [
                ("calc / security", "added", []), ("calc / tester", "changed", ["role_prompt", "harness"])]
            skipped = {c["key"]: c["reason"] for c in changes if c["action"] == "skipped"}
            assert skipped["calc / security"] == "the file gives this template twice"
            assert "names a built-in role" in skipped["calc / Worker"]
            assert "this project allows at most 500" in skipped["calc / long"]
            assert "kind must be one of" in skipped["calc / plan"]
            assert "role_prompt is required" in skipped["calc / empty"]
            assert "description is over 300 characters" in skipped["calc / wordy"]
            assert "is not a JSON object" in skipped["calc / None"]
            assert skipped["docs / role_templates"] == "role_templates is not a list"
            assert [n for n in plan["notes"] if "role template" in n] == [
                "calc / role template security: no item skill:nowhere here or in the file; imported without it",
                "calc / role template security: no Agent Profile 'claude-code/nowhere' here or in the file; imported "
                "without one"]
            rows = {r["template_key"]: (r["kind"], r["role_prompt"], r["harness"], r["profile_id"]) for r in
                    await target.fetch("SELECT * FROM project_role_templates")}
            assert rows == {"mine": ("work", "Mine.", None, None), "security": ("review", "Check security.", None, None),
                            "tester": ("work", "Write tests.", "## How\nTDD.", None)}

            exported = tmp_path / "out.json"
            assert (await _transfer_file(target_url, "export", str(exported), include_projects=True))[0] == 0
            new = {p["name"]: p for p in _without_time(exported)["projects"]}
            assert [t["key"] for t in new["calc"]["role_templates"]] == ["mine", "security", "tester"]
            assert new["calc"]["role_templates"][1] == {**good, "key": "security"}
            assert "role_templates" not in new["docs"]

    asyncio.run(_test())


# --- Schedules ---


def test_a_schedule_whose_settings_changed_is_rewritten_disabled_and_one_that_did_not_keeps_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test() -> None:
        async with _installation() as (source, source_url), _installation() as (target, target_url):
            await _populate(monkeypatch, source, tmp_path / "calc")
            exported = tmp_path / "config.json"
            assert (await _transfer_file(source_url, "export", str(exported), include_projects=True))[0] == 0
            calc = _by_name(json.loads(exported.read_text())["projects"], "calc")
            nightly = _by_name(calc["schedules"], "nightly")
            # Not whether it was enabled, nor its directory: a flag says it had one.
            assert "enabled" not in nightly and "work_dir" not in nightly and nightly["repoint"] is True
            assert (nightly["profiles"], nightly["task_limits"]) == ({"worker": "claude-code/sonnet"}, {"worker": 2})
            assert (await _transfer_file(target_url, "import", str(exported), apply=True))[0] == 0
            _serve(monkeypatch, target)
            # As the note asks: a directory here, then each schedule enabled.
            await target.execute("UPDATE projects SET work_dir = $1 WHERE name = 'calc'", str(tmp_path))
            for row in await target.fetch("SELECT id FROM schedules"):
                await _ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": row['id'], "fields": {"enabled": True}})

            nightly["cron"] = "0 3 * * *"
            exported.write_text(json.dumps(json.loads(exported.read_text()) | {"projects": [
                {**calc, "schedules": [nightly, _by_name(calc["schedules"], "weekly")]}]}))
            code, out, err = await _transfer_file(target_url, "import", str(exported), apply=True, json_output=True)

            assert code == 0, err
            plan = _plan(out)
            assert [c for c in plan["changes"] if c["section"] == "project.schedules"] == [
                {"section": "project.schedules", "key": "calc / nightly", "action": "changed", "fields": ["cron"]}]
            rows = {r["name"]: r for r in await target.fetch("SELECT * FROM schedules")}
            assert (rows["nightly"]["cron"], rows["nightly"]["enabled"], rows["nightly"]["next_fire_at"]) == (
                "0 3 * * *", False, None)
            assert rows["nightly"]["rule_changed_at"] is not None
            assert rows["weekly"]["enabled"] and rows["weekly"]["next_fire_at"] is not None
            assert "1 schedule(s) come in disabled (calc / nightly)" in " ".join(plan["notes"])

    asyncio.run(_test())


_GOOD = {"name": "good", "cron": "0 9 * * *", "timezone": "UTC", "requirement": "Run it.",
         "profiles": {"reviewer": "claude-code/opus"}, "task_limits": {"reviewer": 1}}


@pytest.mark.parametrize(("bad", "reason"), [
    ({"cron": "*/2 * * * *"}, "fires 2 minutes apart"),
    ({"timezone": "Mars/Base"}, "unknown time zone"),
    ({"requirement": ""}, "requirement is required"),
    ({"profiles": {"worker": "nope"}}, "profiles.worker: no Agent Profile 'nope' here or in the file"),
    ({"profiles": {"manager": "claude-code/opus"}}, "not 'manager'"),
    ({"task_limits": {"planner": 2}}, "task_limits: one Planner runs a task"),
    ({"enabled": True}, "unknown field 'enabled'"),
    ({"name": "GOOD"}, "the file gives this schedule twice"),
])
def test_a_schedule_the_import_cannot_use_is_skipped_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: dict[str, Any], reason: str,
) -> None:
    async def _test() -> None:
        async with _installation() as (target, target_url):
            path = tmp_path / "config.json"
            path.write_text(json.dumps({"format": "looper-config/v4", "projects": [
                {"name": "calc", "schedules": [_GOOD, {**_GOOD, "name": "bad", **bad}]}]}))
            code, out, err = await _transfer_file(target_url, "import", str(path), apply=True, json_output=True)

            assert code == 0, err
            changes = [c for c in _plan(out)["changes"] if c["section"] == "project.schedules"]
            assert changes[0] == {"section": "project.schedules", "key": "calc / good", "action": "added"}
            assert changes[1]["action"] == "skipped" and reason in changes[1]["reason"], changes
            rows = await target.fetch("SELECT name, enabled, created_via, work_dir FROM schedules")
            assert [tuple(r) for r in rows] == [("good", False, "import", "")]
            limits = await target.fetch("SELECT role, max_concurrency FROM schedule_task_limits")
            assert [tuple(r) for r in limits] == [("reviewer", 1)]

    asyncio.run(_test())


def test_a_schedules_chat_this_machine_does_not_allow_is_dropped_with_a_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test() -> None:
        async with _installation() as (target, target_url):
            path = tmp_path / "config.json"
            told = {**_GOOD, "notify_target": {"channel": "lark", "chat": "ou_elsewhere"}}
            path.write_text(json.dumps({"format": "looper-config/v4", "projects": [
                {"name": "calc", "schedules": [told]}]}))
            code, out, err = await _transfer_file(target_url, "import", str(path), apply=True, json_output=True)

            assert code == 0, err
            assert await target.fetchval("SELECT notify_channel FROM schedules WHERE name = 'good'") is None
            assert any("lark chat 'ou_elsewhere' is not in this machine's sender allowlist" in note
                       for note in _plan(out)["notes"])

    asyncio.run(_test())
