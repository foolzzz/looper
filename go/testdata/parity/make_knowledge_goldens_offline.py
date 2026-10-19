#!/usr/bin/env python3
"""Generate knowledge goldens through real Python v2.10.0 HTTP handlers.

Fallback for an environment which cannot connect to disposable PostgreSQL.
Only database readers are substituted, using the actual INSERT fixture as
input. This does not establish SQL/database parity. The normal database-backed
make_goldens.py remains authoritative; run it and TestKnowledgeGoldenParity
against a disposable loopgo_* database when PostgreSQL is reachable.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

import make_goldens as goldens

sys.path.insert(0, str(goldens.REPO))


def fixture_rows(sql: str) -> dict[str, list[dict]]:
    """Read this fixture's INSERT literals, without a second copy of its data.

    The grammar intentionally accepts only strings, integers and NULL. An
    unsupported SQL expression fails immediately rather than guessing a value.
    """
    sql = re.sub(r"(?m)^\s*--[^\n]*", "", sql)
    tables = {}
    for match in re.finditer(r"INSERT INTO (\w+)\s*\((.*?)\)\s*VALUES\s*(.*?);", sql, re.S):
        table, columns, values = match.groups()
        columns = [x.strip() for x in columns.split(",")]
        pos = 0
        rows = []
        while pos < len(values):
            while pos < len(values) and values[pos] in " \t\r\n,":
                pos += 1
            if pos == len(values):
                break
            if values[pos] != "(":
                raise ValueError(f"unsupported fixture SQL at {values[pos:pos+20]!r}")
            pos += 1
            row = []
            while True:
                while values[pos].isspace():
                    pos += 1
                escaped = values.startswith("E'", pos)
                if escaped:
                    pos += 1
                if values[pos] == "'":
                    pos += 1
                    chars = []
                    while True:
                        if values[pos:pos+2] == "''":
                            chars.append("'")
                            pos += 2
                        elif values[pos] == "'":
                            pos += 1
                            break
                        elif escaped and values[pos] == "\\":
                            pos += 1
                            chars.append({"n": "\n", "r": "\r", "t": "\t"}.get(values[pos], values[pos]))
                            pos += 1
                        else:
                            chars.append(values[pos])
                            pos += 1
                    value = "".join(chars)
                else:
                    token = re.match(r"NULL|-?\d+", values[pos:])
                    if not token:
                        raise ValueError(f"unsupported fixture literal: {values[pos:pos+20]!r}")
                    value = None if token[0] == "NULL" else int(token[0])
                    pos += len(token[0])
                row.append(value)
                while values[pos].isspace():
                    pos += 1
                delim = values[pos]
                pos += 1
                if delim == ")":
                    break
                if delim != ",":
                    raise ValueError("unsupported fixture delimiter")
            if len(row) != len(columns):
                raise ValueError("fixture INSERT column/value count differs")
            item = dict(zip(columns, row))
            for field in ("created_at", "updated_at", "imported_at"):
                if field in item:
                    item[field] = datetime.fromisoformat(item[field].replace("Z", "+00:00"))
            rows.append(item)
        tables.setdefault(table, []).extend(rows)
    return tables


async def main():
    from looper.agents.http_server import AgentHTTPServer
    from looper.db import connection, library, project_documents, projects
    tables = {}

    class FixturePool:
        async def execute(self, sql):
            tables.update(fixture_rows(sql))

    async def project(project_id):
        return next((r for r in tables["projects"] if r["id"] == project_id), None)

    async def documents(project_id):
        rows = [dict(r) for r in tables["project_documents"] if r["project_id"] == project_id]
        for row in rows:
            del row["content"]
        return sorted(rows, key=lambda r: (r["created_at"], r["id"]))

    async def content(document_id):
        return next((r["content"] for r in tables["project_documents"] if r["id"] == document_id), None)

    async def drafts(project_id):
        return sorted([r for r in tables["project_document_drafts"] if r["project_id"] == project_id],
                      key=lambda r: (r["created_at"], r["id"]))

    async def item(item_id):
        return next((r for r in tables["library_items"] if r["id"] == item_id), None)

    async def file(item_id, name):
        return next((r["content"] for r in tables["library_files"] if r["item_id"] == item_id and r["path"] == name), None)

    with tempfile.TemporaryDirectory(prefix="loopgo-knowledge-offline-") as tmp:
        server = object.__new__(AgentHTTPServer)
        with (patch.object(connection, "get_pool", lambda: FixturePool()),
              patch.object(projects, "get_project", project),
              patch.object(project_documents, "list_documents", documents),
              patch.object(project_documents, "get_content", content),
              patch.object(project_documents, "list_drafts", drafts),
              patch.object(library, "get_item", item),
              patch.object(library, "get_file", file)):
            bodies = await goldens._capture_knowledge(server, Path(tmp))
        # Shared SQL-derived input for database-free view tests. Dates and the
        # unique root use exactly the same representation as the goldens.
        inputs = goldens.normalize(tables)
        def encode(value):
            if isinstance(value, datetime):
                return value.isoformat()
            raise TypeError(type(value).__name__)
        (goldens.HERE / "knowledge_fixture_input.json").write_text(
            json.dumps(inputs, ensure_ascii=False, indent=2, sort_keys=True, default=encode) + "\n", encoding="utf-8")
        for name, body in bodies.items():
            (goldens.HERE / f"{name}.json").write_text(
                json.dumps(goldens.normalize(body), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for name, raw in zip(bodies, goldens._RAW):
            if goldens._status_code(raw) != 200:
                (goldens.HERE / "raw" / f"{name}.http").write_bytes(raw)
        print(f"Generated {len(bodies)} offline Python handler goldens; PostgreSQL not exercised.")


if __name__ == "__main__":
    asyncio.run(main())
