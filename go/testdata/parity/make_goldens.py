#!/usr/bin/env python3
"""Write status.json, projects.json, and overview.json from the Python handlers.

Usage: python make_goldens.py --db-url postgresql://postgres:postgres@127.0.0.1:54329/loopgo_...

Every host the driver would dial, including a comma-separated fallback, must be
127.0.0.1 or localhost on port 54329. The database name must start with
loopgo_. The script drops and recreates schema public, applies
looper/db/schema.sql and seed.sql, then calls the real handlers in-process.
It does not call configured_dsn and does not read .env.

The overview clock is local noon on 2026-06-15, the same day the Go parity
test freezes. seed.sql rows at 2026-06-15T12:00:00Z count as today. Rows in
2020 do not.

Normalization, the same rule the Go parity test applies before comparing:

- Walk every JSON value.
- A string matching an ISO-8601 datetime, YYYY-MM-DDTHH:MM:SS with an optional
  fraction and a Z or ±HH:MM suffix, becomes "<time>".
- The value of a key named uptime, pid, pids, or version becomes "<volatile>"
  and is not walked. implementation and db are written as the handler returned
  them.
- Object key order is not significant. Numbers compare by value, so 0 and 0.0
  match. The files below are written with sorted keys for review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import urllib.parse
from datetime import date
from pathlib import Path

_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_VOLATILE = {"uptime", "pid", "pids", "version"}
_REFUSAL = "refusing database URL: want 127.0.0.1:54329 and a loopgo_ database name"
# Local noon on this day is the parity clock. Keep it in step with parity_test.go.
_PARITY_DAY = date(2026, 6, 15)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]  # go/testdata/parity -> worktree root


def normalize(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _VOLATILE:
                out[key] = "<volatile>"
                continue
            out[key] = normalize(item)
        return out
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, str) and _TIME.fullmatch(value):
        return "<time>"
    return value


def check_db_url(dsn: str) -> None:
    """Refuse a URL whose database name does not start with loopgo_, or that dials any other host.

    asyncpg tries every comma-separated host. A query host is used only when the
    URL has no host, which is the driver's rule. A service= parameter can load
    more hosts from a file, so it is refused. A missing port is refused rather
    than taken from PGPORT.
    """
    try:
        hosts, name = _db_targets(dsn)
    except Exception:
        raise SystemExit(_REFUSAL) from None
    if not name.startswith("loopgo_") or not hosts:
        raise SystemExit(_REFUSAL)
    for host, port in hosts:
        if host not in {"127.0.0.1", "localhost"} or port != 54329:
            raise SystemExit(_REFUSAL)


def _db_targets(dsn: str) -> tuple[list[tuple[str, int]], str]:
    parsed = urllib.parse.urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("scheme")
    query: dict[str, str] = {}
    if parsed.query:
        for key, vals in urllib.parse.parse_qs(parsed.query, strict_parsing=True).items():
            query[key] = vals[-1]
    if "service" in query:
        raise ValueError("service")
    hostspec = ""
    if parsed.netloc:
        hostspec = parsed.netloc.partition("@")[2] if "@" in parsed.netloc else parsed.netloc
    name = urllib.parse.unquote(parsed.path[1:]) if parsed.path.startswith("/") else urllib.parse.unquote(parsed.path)
    if not name:
        name = query.get("dbname") or query.get("database") or ""
    if hostspec:
        hosts = _host_ports(hostspec, None)
    elif query.get("host"):
        hosts = _host_ports(query["host"], _port_list(query.get("port")))
    else:
        raise ValueError("host")
    return hosts, name


def _port_list(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    return [int(part) for part in raw.split(",")]


def _host_ports(hostspec: str, ports: list[int] | None) -> list[tuple[str, int]]:
    specs = hostspec.split(",") if "," in hostspec else [hostspec]
    if ports is not None:
        if len(ports) == 1:
            ports = ports * len(specs)
        elif len(ports) != len(specs):
            raise ValueError("ports")
    found: list[tuple[str, int]] = []
    for index, spec in enumerate(specs):
        host, port = _one_host(spec)
        if port is None:
            if ports is None:
                raise ValueError("port")
            port = ports[index]
        found.append((host, port))
    return found


def _one_host(spec: str) -> tuple[str, int | None]:
    if not spec:
        raise ValueError("host")
    if spec[0] == "/":
        raise ValueError("socket")
    if spec[0] == "[":
        match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", spec)
        if match is None:
            raise ValueError("ipv6")
        host = urllib.parse.unquote(match.group(1))
        port = int(urllib.parse.unquote(match.group(2))) if match.group(2) else None
        return host, port
    host, sep, port_text = spec.partition(":")
    host = urllib.parse.unquote(host)
    if sep == "":
        return host, None
    return host, int(urllib.parse.unquote(port_text))


def response_json(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
    if not status_line.endswith("200 OK"):
        raise SystemExit(f"handler returned {status_line}")
    return json.loads(body)


async def apply_sql(dsn: str) -> None:
    import asyncpg

    schema = (REPO / "looper" / "db" / "schema.sql").read_text(encoding="utf-8")
    seed = (HERE / "seed.sql").read_text(encoding="utf-8")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception:
        raise SystemExit("database connection failed") from None
    try:
        await conn.execute("DROP SCHEMA public CASCADE")
        await conn.execute("CREATE SCHEMA public")
        await conn.execute(schema)
        await conn.execute(seed)
    except Exception:
        raise SystemExit("schema or seed failed") from None
    finally:
        await conn.close()


def _freeze_today() -> None:
    """Make overview.local_midnight the local midnight of _PARITY_DAY."""
    import looper.agents.overview as overview

    class _Day(date):
        @classmethod
        def today(cls) -> date:
            return _PARITY_DAY

    overview.date = _Day


async def capture(dsn: str) -> dict[str, object]:
    import looper.db.connection as dbconn
    from looper.agents.bus import MessageBus
    from looper.agents.channels.web import WebChannelAdapter
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.supervisor import AgentSupervisor
    from looper.agents.tiers.task import TaskRouter

    # A stray configured_dsn() must not open .env or the default loopx database.
    dbconn._load_env = lambda: None
    _freeze_today()
    await dbconn.init_pool(dsn)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = AgentHTTPServer(
                host="127.0.0.1",
                port=0,
                web_adapter=WebChannelAdapter(),
                task_router=TaskRouter(runtime_root=root),
                supervisor=AgentSupervisor(MessageBus(root), root),
                worker_pool=None,
                manager=None,
                self_checks=[],
                runtime_root=root,
            )

            class Capture:
                def __init__(self) -> None:
                    self.buf = bytearray()

                def write(self, data: bytes) -> None:
                    self.buf.extend(data)

                async def drain(self) -> None:
                    return None

            out = {}
            for name, method in (
                ("status", server._handle_status),
                ("projects", server._handle_list_projects),
                ("overview", server._handle_overview),
            ):
                cap = Capture()
                await method(cap)
                out[name] = response_json(bytes(cap.buf))
            return out
    finally:
        await dbconn.close_pool()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", required=True)
    args = parser.parse_args()
    check_db_url(args.db_url)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    asyncio.run(apply_sql(args.db_url))
    bodies = asyncio.run(capture(args.db_url))
    for name in ("status", "projects", "overview"):
        payload = normalize(bodies[name])
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (HERE / f"{name}.json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
