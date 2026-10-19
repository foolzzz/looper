#!/usr/bin/env python3
"""Capture Python v2.10.0 config exports, without loading operator configuration.

--db-url replaces public in an owned disposable loopgo_* DB at localhost:54329,
loads fixture.sql and runs Python's legacy effort migration before capture.
--offline uses explicitly synthetic rows; it does not validate PostgreSQL reads.
The two modes must produce identical export and HTTP bytes (apart from IDs that
are deliberately absent from exports). No channel or model calls are made.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import make_goldens as goldens

HERE = Path(__file__).resolve().parent / "config_export"
sys.path.insert(0, str(goldens.REPO))
FROZEN = dt.datetime(2026, 6, 15, 12, tzinfo=dt.timezone.utc)
SECRET = "SYNTHETIC_CONFIG_EXPORT_SECRET_DO_NOT_USE"
STORED_CHANNEL_SECRET = "SYNTHETIC_CHANNEL_STORAGE_ONLY_DO_NOT_EXPORT"


def check_legacy_conversion(rows):
    """Run the reference's pure conversion before using the post-upgrade rows."""
    from looper.agents import layer_effort
    from looper.agents.agent_settings import AgentType, Layer
    from looper.agents.profile_registry import AgentProfile, BUILTIN_PROFILE

    runtimes = {row["name"]: row for row in rows["runtimes"]}
    providers = {row["name"]: row for row in rows["providers"]}
    profiles = {BUILTIN_PROFILE.id: BUILTIN_PROFILE}
    for row in rows["profiles"]:
        if row["builtin"] or row["id"] == "agent-export-high":
            continue
        runtime, provider = runtimes[row["runtime"]], providers[row["provider"]]
        profiles[row["id"]] = AgentProfile.from_row({
            **row, "runtime_id": runtime["id"], "runtime_name": runtime["name"],
            "runtime_type": runtime["type"], "runtime_executable": runtime["executable"],
            "runtime_capabilities": runtime["capabilities"], "provider_id": provider["id"],
            "provider_name": provider["name"], "provider_auth": provider["auth"],
            "provider_protocols": provider["protocols"], "provider_models": provider["models"],
            "provider_env": provider["env"], "provider_base_url": provider["base_url"],
        })
    before = {}
    for row in rows["layers"]:
        key = (Layer(row["scope"]), AgentType(row["role"]) if row["role"] else None, row["project_id"])
        before[key] = layer_effort.Before(
            profiles["agent-export"] if row["scope"] == "system" else profiles.get(row["profile_id"]),
            "high" if row["scope"] == "system" else None)
    result = layer_effort.convert(before, profiles.values())
    assert len(result.derived) == 1
    derived = result.derived[0]
    assert (derived.name, derived.effort) == ("Fixture Agent · high", "high")
    assert result.layers == {layer_effort.SYSTEM: derived}


def fixture_sql(rows):
    """Seed the pre-migration state; the synthetic read rows show its upgrade."""
    statements = ["-- Synthetic config export fixture; run the canonical schema first.",
                  "-- Python layer_effort.migrate must run after this seed."]
    json_columns = {"env", "headers", "models", "config"}

    def literal(value, column):
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        if column in json_columns:
            value = json.dumps(value, ensure_ascii=False)
            return "'" + value.replace("'", "''") + "'::jsonb"
        if isinstance(value, list):
            return "ARRAY[" + ",".join(literal(v, "") for v in value) + "]::text[]"
        return "'" + value.replace("'", "''") + "'"

    def insert(table, values):
        columns = list(values)
        statements.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                          f"({', '.join(literal(values[c], c) for c in columns)});")

    for row in rows["runtimes"]:
        insert("agent_runtimes", {k: row[k] for k in ("id", "name", "type", "executable", "capabilities", "builtin")})
    for row in rows["providers"]:
        insert("llm_providers", {k: v for k, v in row.items() if k != "api_key"})
        insert("llm_provider_secrets", {"provider_id": row["id"], "api_key": row["api_key"]})
    runtime_ids = {r["name"]: r["id"] for r in rows["runtimes"]}
    provider_ids = {r["name"]: r["id"] for r in rows["providers"]}
    for row in rows["profiles"]:
        if row["builtin"] or row["id"] == "agent-export-high":
            continue
        insert("agent_profiles", {**{k: v for k, v in row.items() if k not in ("runtime", "provider")},
                                  "runtime_id": runtime_ids[row["runtime"]],
                                  "provider_id": provider_ids[row["provider"]]})
    for row in rows["roles"]:
        insert("role_configs", row)
    for row in rows["channels"]:
        insert("channel_configs", row)
    insert("channel_secrets", {"channel_id": "telegram-export", "bot_token": STORED_CHANNEL_SECRET})
    for row in rows["projects"]:
        insert("projects", {**row, "work_dir": "/synthetic/project/workdir"})
    statements.append("DELETE FROM schema_migrations WHERE name = 'layer_effort_on_profiles';")
    for row in rows["layers"]:
        values = {k: v for k, v in row.items() if k != "profile"}
        if row["scope"] == "system":
            values.update(profile_id="agent-export", effort="high")
        insert("agent_setting_layers", values)
    for row in rows["items"]:
        insert("library_items", {k: row[k] for k in (
            "id", "kind", "name", "origin", "owner_project_id", "description", "source", "version", "sha256",
            "size_bytes", "file_count", "modified")})
        if row["kind"] == "mcp":
            insert("library_mcp_servers", {"item_id": row["id"], **{k: row[k] for k in (
                "transport", "command", "args", "env", "url", "headers")}})
    for item_id, files in rows["files"].items():
        for row in files:
            insert("library_files", {"item_id": item_id, **row})
    for row in rows["installs"]:
        insert("library_installs", {k: row[k] for k in (
            "id", "item_id", "scope", "project_id", "roles", "enabled")})
    for index, row in enumerate(rows["documents"]):
        insert("project_documents", {**row, "created_at": f"2020-01-01T00:00:0{index}+00:00"})
    for row in rows["templates"]:
        insert("project_role_templates", {k: v for k, v in row.items() if k != "profile"})
    for row in rows["schedules"]:
        insert("schedules", {k: v for k, v in row.items() if k not in (
            "project_name", "profile_overrides", "task_limits")})
        for role, profile_id in row["profile_overrides"].items():
            insert("schedule_profile_overrides", {"schedule_id": row["id"], "role": role, "profile_id": profile_id})
        for role, limit in row["task_limits"].items():
            insert("schedule_task_limits", {"schedule_id": row["id"], "role": role, "max_concurrency": limit})
    return "\n".join(statements) + "\n"


class FrozenDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN.astimezone(tz) if tz else FROZEN.replace(tzinfo=None)


class FrozenDate(dt.date):
    @classmethod
    def today(cls):
        return FROZEN.date()


class Capture:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass


class OfflineRows:
    """Synthetic asyncpg-shaped SQL results, consumed by the real export reads."""
    def __init__(self, rows):
        self.rows = rows

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self, **kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        yield

    async def fetch(self, query, *args):
        sources = (
            ("FROM agent_setting_layers l", "layers"),
            ("FROM agent_runtimes ", "runtimes"),
            ("FROM llm_providers p", "providers"),
            ("FROM agent_profiles a JOIN", "profiles"),
            ("FROM role_configs ", "roles"),
            ("FROM library_items i", "items"),
            ("FROM library_installs s", "installs"),
            ("FROM channel_configs ", "channels"),
            ("FROM projects ", "projects"),
            ("FROM project_documents ", "documents"),
            ("FROM project_role_templates t", "templates"),
            ("FROM schedules s JOIN", "schedules"),
        )
        if "FROM library_files " in query:
            return copy.deepcopy(self.rows["files"].get(args[0], []))
        if "FROM schedule_profile_overrides " in query:
            return [{"schedule_id": row["id"], "role": role, "profile_id": pid}
                    for row in self.rows["schedules"] for role, pid in sorted(row["profile_overrides"].items())]
        if "FROM schedule_task_limits " in query:
            return [{"schedule_id": row["id"], "role": role, "max_concurrency": limit}
                    for row in self.rows["schedules"] for role, limit in sorted(row["task_limits"].items())]
        for pattern, source in sources:
            if pattern in query:
                return copy.deepcopy(self.rows[source])
        raise AssertionError(f"unexpected SQL: {query}")


CASES = [
    {"name": "default", "query": {}},
    {"name": "secrets", "query": {"secrets": "1"}},
    {"name": "projects", "query": {"projects": "1"}},
    {"name": "projects-secrets", "query": {"projects": "1", "secrets": "1"}},
    {"name": "invalid-query", "query": {"projects": "true", "secrets": "yes", "unknown": "1"}},
    {"name": "blank-query", "query": {"projects": "", "secrets": ""}},
    {"name": "zero-query", "query": {"projects": "0", "secrets": "0"}},
    {"name": "spaces-query", "query": {"projects": " 1", "secrets": "1 "}},
]


async def capture(pool):
    import looper
    from looper.agents import config_transfer
    from looper.agents.http_server import AgentHTTPServer
    from looper.db import connection

    outputs = {}
    server = object.__new__(AgentHTTPServer)
    with (patch.object(connection, "get_pool", return_value=pool),
          patch.object(config_transfer, "datetime", FrozenDatetime),
          patch.object(dt, "date", FrozenDate), patch.object(looper, "__version__", "2.10.0")):
        for case in CASES:
            writer = Capture()
            await server._handle_config_export(case["query"], writer)
            raw = bytes(writer.buf)
            headers, body = raw.split(b"\r\n\r\n", 1)
            assert headers.startswith(b"HTTP/1.1 200 OK\r\n")
            assert b"Cache-Control: no-store" in headers
            assert b'filename="looper-config-2026-06-15.json"' in headers
            value = json.loads(body)
            include_secrets = case["query"].get("secrets") == "1"
            include_projects = case["query"].get("projects") == "1"
            assert (SECRET.encode() in body) == include_secrets
            assert STORED_CHANNEL_SECRET.encode() not in body
            assert ("projects" in value) == include_projects
            if include_projects:
                assert any(p["knowledge"] for p in value["projects"])
                assert any(p.get("role_templates") for p in value["projects"])
                assert any(p.get("schedules") for p in value["projects"])
            outputs[case["name"] + ".json"] = body
            outputs[case["name"] + ".http"] = raw

        for case in CASES[:4]:
            with tempfile.TemporaryDirectory(prefix="loopgo-export-golden-") as temp:
                path = Path(temp) / "export.json"
                async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
                    summary = await config_transfer.write_export(
                        conn, path, include_projects=case["query"].get("projects") == "1",
                        include_secrets=case["query"].get("secrets") == "1")
                assert path.read_bytes() == outputs[case["name"] + ".json"]
                assert summary.bytes == path.stat().st_size
                if case["query"].get("secrets") == "1":
                    assert path.stat().st_mode & 0o077 == 0
                outputs[case["name"] + ".summary.json"] = (
                    json.dumps(vars(summary), indent=2, ensure_ascii=False) + "\n").encode()
    outputs["cases.json"] = (json.dumps(CASES, indent=2) + "\n").encode()
    return outputs


async def main(args):
    rows = json.loads((HERE / "rows.json").read_text())
    check_legacy_conversion(rows)
    sql = fixture_sql(rows)
    if args.offline:
        outputs = await capture(OfflineRows(rows))
        mode = "synthetic SQL rows (PostgreSQL NOT validated)"
    else:
        goldens.check_db_url(args.db_url)
        from looper.agents import layer_effort
        from looper.db import connection

        connection._load_env = lambda: None
        pool = await connection.init_pool(args.db_url)
        try:
            await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            await pool.execute((goldens.REPO / "looper/db/schema.sql").read_text())
            await pool.execute(sql)
            async with pool.acquire() as conn, conn.transaction():
                await layer_effort.migrate(conn)
            outputs = await capture(pool)
            offline = await capture(OfflineRows(rows))
            for name, data in outputs.items():
                expected = offline[name]
                if data != expected:
                    first = next((i for i, (a, b) in enumerate(zip(data, expected)) if a != b),
                                 min(len(data), len(expected)))
                    raise AssertionError(
                        f"PostgreSQL differs from synthetic rows: {name} at byte {first} "
                        f"(database {len(data)} bytes, synthetic {len(expected)} bytes)")
        finally:
            await connection.close_pool()
        mode = "disposable PostgreSQL + Python legacy effort migration"
    outputs["fixture.sql"] = sql.encode()
    for name, data in outputs.items():
        (HERE / name).write_bytes(data)
    print(f"Generated {len(CASES)} config export HTTP goldens using {mode}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--db-url")
    mode.add_argument("--offline", action="store_true")
    asyncio.run(main(parser.parse_args()))
