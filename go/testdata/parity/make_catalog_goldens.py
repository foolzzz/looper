#!/usr/bin/env python3
"""Capture catalog goldens with Python v2.10.0's real catalog HTTP handler.

--db-url must name an owned disposable loopgo_* database on localhost:54329;
its public schema is replaced. All reads run against PostgreSQL so file order
follows the database collation. Offline generation is deliberately unsupported.
The fixture database must use en_US.utf8 for LC_COLLATE and LC_CTYPE, as CI does.
No configured DSN, .env, operator runtime, channels or model calls are used.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

import make_goldens as goldens

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(goldens.REPO))
TABLES = ("projects", "library_items", "library_files", "library_mcp_servers", "library_installs", "system_settings")
ITEMS = ("lib-machineplugin01", "lib-uploadplugin01", "lib-gitplugin01", "lib-machineskill01",
         "lib-uploadskill01", "lib-gitskill01", "lib-uiskill01", "skill-ownedalpha01",
         "skill-ownedbeta01", "skill-missingfile01", "mcp-machine01", "mcp-http01", "mcp-sse01")


def sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "ARRAY[" + ",".join(sql_literal(v) for v in value) + "]::text[]"
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "'" + value.replace("'", "''") + "'"


def fixture_sql(tables):
    statements = []
    for table in TABLES:
        for row in tables[table]:
            columns = sorted(row)
            statements.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                              f"({', '.join(sql_literal(row[c]) for c in columns)});")
    return "\n".join(statements) + "\n"


async def capture(pool):
    outputs = {}
    from looper.agents import catalog
    from looper.agents.http_server import AgentHTTPServer, HTTP_200
    server = object.__new__(AgentHTTPServer)

    class Capture:
        def __init__(self):
            self.buf = bytearray()
        def write(self, data):
            self.buf.extend(data)
        async def drain(self):
            pass

    async def take(name, call, status=200):
        writer = Capture()
        await server._handle_catalog(writer, HTTP_200, call)
        raw = bytes(writer.buf)
        value = goldens.response_json(raw, status)
        outputs[f"{name}.json"] = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if status != 200:
            outputs[f"raw/{name}.http"] = raw

    await take("catalog", catalog.catalog_view)
    for item_id in ITEMS:
        await take("catalog-" + item_id, lambda i=item_id: catalog.item_detail(i))
    await take("catalog-missing", lambda: catalog.item_detail("lib-absent01"), 404)
    for suffix in ("01", "02", "03"):
        await take("catalog-installs" + suffix, lambda s=suffix: catalog.project_view("proj-catalog" + s))
    await take("catalog-installs-missing", lambda: catalog.project_view("proj-absent01"), 404)
    # The same state transitions are applied by TestCatalogGoldenParity.
    for name, values in (("catalog-invalid-reports", {"library.last_import": "{bad", "library.last_mcp_import": "["}),
                         ("catalog-scalar-reports", {"library.last_import": "7", "library.last_mcp_import": "[true, null]"})):
        await pool.executemany("UPDATE system_settings SET value=$2 WHERE key=$1", list(values.items()))
        await take(name, catalog.catalog_view)
    await pool.execute("DELETE FROM system_settings WHERE key IN ('library.last_import','library.last_mcp_import')")
    await take("catalog-no-reports", catalog.catalog_view)
    await pool.execute("DELETE FROM library_items")
    await take("catalog-empty", catalog.catalog_view)
    return outputs


async def main(args):
    goldens.check_db_url(args.db_url)
    from looper.db import connection
    tables = json.loads((HERE / "catalog_fixture_input.json").read_text())
    # Generated SQL is consumed by Go, with JSON as its sole fixture source.
    sql = fixture_sql(tables)
    connection._load_env = lambda: None
    pool = await connection.init_pool(args.db_url)
    try:
        locale = await pool.fetchrow(
            "SELECT datcollate, datctype FROM pg_database WHERE datname=current_database()")
        if locale["datcollate"] != "en_US.utf8" or locale["datctype"] != "en_US.utf8":
            raise ValueError("catalog fixtures require LC_COLLATE=LC_CTYPE=en_US.utf8 (matching CI)")
        await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await pool.execute((goldens.REPO / "looper/db/schema.sql").read_text())
        await pool.execute(sql)
        outputs = await capture(pool)
    finally:
        await connection.close_pool()
    # A failed connection or capture must leave the previous fixture set intact.
    outputs["catalog_fixture.sql"] = sql.encode("utf-8")
    for name, data in outputs.items():
        (HERE / name).write_bytes(data)
    print("Generated 23 catalog goldens through Python handlers and disposable PostgreSQL.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", required=True)
    asyncio.run(main(parser.parse_args()))
