#!/usr/bin/env python3
"""Write the parity goldens from the Python handlers.

Usage: python make_goldens.py --db-url postgresql://postgres:postgres@127.0.0.1:54329/loopgo_...

Every host the driver would dial, including a comma-separated fallback, must be
127.0.0.1 or localhost on port 54329. The database name must start with
loopgo_. The script drops and recreates schema public, applies
looper/db/schema.sql and seed.sql, then calls the real handlers in-process.
It does not call configured_dsn and does not read .env.

The overview clock and the schedule clock are local noon on 2026-06-15, the
same day the Go parity test freezes. seed.sql rows at 2026-06-15T12:00:00Z
count as today. Rows in 2020 do not. A schedule run at 2026-06-14T09:00:00Z
is before that local midnight everywhere from UTC-12 through UTC+14.

Normalization, the same rule the Go parity test applies before comparing:

- Walk every JSON value.
- The value of a key in _VOLATILE_TIMES (since) becomes "<time>" and is not
  walked. Those are generated when the request runs. Seeded timestamps stay
  as the handler returned them. _VOLATILE_TIMES is volatileTimes in
  parity_test.go.
- The value of a key named uptime, pid, pids, or default_timezone becomes
  "<volatile>" and is not walked. default_timezone is this machine's IANA
  zone. A string version (a build id) becomes "<volatile>" too. A
  numeric version, such as a requirement or role version, is written as the
  handler returned it. implementation and db are written as the handler
  returned them.
- Knowledge fixture paths become "<knowledge-root>". The harness first checks
  index_chars against the original index, then compares its length after this
  path replacement as well.
- Object key order is not significant. Numbers compare by value, so 0 and 0.0
  match. The files below are written with sorted keys for review.

--raw-only writes raw/<name>.http for /api/status and every response whose
status is not 200, plus error-not-found, error-bad-host, error-cross-origin,
and no-content. Those files are the exact _json_response / _no_content bytes.
Sorted JSON files are left untouched. Blank lines and # comments in
raw/<name>.paths name JSON Pointers the Go compare may replace; this script
does not write path files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
from datetime import date
from pathlib import Path

_VOLATILE = {"uptime", "pid", "pids", "default_timezone"}
# Request-time timestamps only. Same set as volatileTimes in parity_test.go.
_VOLATILE_TIMES = {"since"}
_READS = (
    ("status", 200),
    ("projects", 200),
    ("overview", 200),
)
# The default list, each group, one project, one limit, and two bad parameters.
# tasks-before is the next page after tasks-limit (that page's next_before).
# status filters the page and not the counts. schedule_id filters both.
# tasks-combined is empty: waiting_user and the nightly schedule do not
# overlap, while counts still follow the schedule. Dropping either predicate,
# or applying status to the counts, changes that body.
_TASKS = (
    ("tasks", {}, 200),
    ("tasks-group-needs_you", {"group": "needs_you"}, 200),
    ("tasks-group-in_progress", {"group": "in_progress"}, 200),
    ("tasks-group-ready", {"group": "ready"}, 200),
    ("tasks-group-attention", {"group": "attention"}, 200),
    ("tasks-group-completed", {"group": "completed"}, 200),
    ("tasks-group-cancelled", {"group": "cancelled"}, 200),
    ("tasks-group-other", {"group": "other"}, 200),
    ("tasks-project", {"project_id": "proj-beta00002"}, 200),
    ("tasks-limit", {"limit": "2"}, 200),
    ("tasks-bad-group", {"group": "nope"}, 400),
    ("tasks-bad-limit", {"limit": "0"}, 400),
    ("tasks-before", {"limit": "2", "before": "task-asame0001"}, 200),
    ("tasks-status", {"status": "running"}, 200),
    ("tasks-schedule", {"schedule_id": "sched-nightly01"}, 200),
    ("tasks-combined", {
        "project_id": "proj-alpha0001",
        "status": "waiting_user",
        "schedule_id": "sched-nightly01",
    }, 200),
)
# One rich task, one task with nothing attached, and an id the database does
# not have. The missing id is the handler's 404, not the router's.
_DETAILS = (
    ("task-rich", "task-rich00001", 200),
    ("task-min", "task-min000001", 200),
    ("task-missing", "task-missing01", 404),
)
# usage-agent01 has a priced turn, an unpriced turn, and a running turn.
# turns=0 clamps to 1, the same body as turns=1. "many" is the handler's 400.
# "\x1c2" is the query turns=%1C2. str.isspace() is true for U+001C and
# int() raises ValueError, so the handler answers 400.
_USAGE = (
    ("usage", "usage-agent01", {}, 200),
    ("usage-turns", "usage-agent01", {"turns": "1"}, 200),
    ("usage-turns-zero", "usage-agent01", {"turns": "0"}, 200),
    ("usage-bad-turns", "usage-agent01", {"turns": "many"}, 400),
    ("usage-bad-turns-1c", "usage-agent01", {"turns": "\x1c2"}, 400),
)
# The list, each filter, a page, an unknown role, and the bad parameters.
# limit and offset are parsed outside the handler's try: a non-integer is
# the server's 500, which take() writes when the handler raises ValueError.
# A negative offset is a database error, and the handler answers 200 with
# an empty list. limit=0 is a real empty page. limit=%1C2 is U+001C then 2:
# int() rejects it, same 500 as any other non-integer.
_RECORDS = (
    ("llm-records", {}, 200),
    ("llm-records-agent", {"agent_id": "usage-agent01"}, 200),
    ("llm-records-task", {"task_id": "task-rich00001"}, 200),
    ("llm-records-role", {"role": "manager"}, 200),
    ("llm-records-page", {"limit": "1", "offset": "1"}, 200),
    ("llm-records-unknown-role", {"role": "nope"}, 200),
    ("llm-records-limit-zero", {"limit": "0"}, 200),
    ("llm-records-neg-offset", {"offset": "-1"}, 200),
    ("llm-records-bad-limit", {"limit": "many"}, 500),
    ("llm-records-bad-offset", {"offset": "1.5"}, 500),
    ("llm-records-bad-limit-1c", {"limit": "\x1c2"}, 500),
)
# 91001 is the priced usage turn. 91009 is not a row.
_RECORD = (
    ("llm-record", 91001, 200),
    ("llm-record-missing", 91009, 404),
)
# after=1 keeps seq 2. A missing record is an empty list, not 404.
# limit=-1 is a database error (500). "nope" is the handler's 400.
_EVENTS = (
    ("llm-record-events", 91001, {}, 200),
    ("llm-record-events-after", 91001, {"after": "1"}, 200),
    ("llm-record-events-bad", 91001, {"after": "nope"}, 400),
    ("llm-record-events-missing", 91009, {}, 200),
    ("llm-record-events-neg-limit", 91001, {"limit": "-1"}, 500),
)
# The Manager, then the newest 30 tasks and their step agents. No query.
_CHAT_TARGETS = (("chat-targets", 200),)
# Planner threads are excluded. sess-lark-1 is newer than sess-web-1, and its
# last message is longer than 200 characters. There is no query parameter.
_CHAT_SESSIONS = (("chat-sessions", 200),)
# The lark session's assistant message is the full 250 characters, with the
# delivery fields. An unknown session is an empty 200. A missing session_id
# is the handler's 400. A session id of spaces is queried.
_CHAT_MESSAGES = (
    ("chat-messages", {"session_id": "sess-lark-1"}, 200),
    ("chat-messages-web", {"session_id": "sess-web-1"}, 200),
    ("chat-messages-missing", {"session_id": "sess-missing"}, 200),
    ("chat-messages-required", {}, 400),
    ("chat-messages-spaces", {"session_id": " "}, 200),
)
# Five rows on task-rich00001. since is exclusive. limit=0 and a negative
# limit are empty 200s. A non-integer is parsed outside the try, so take()
# writes the request loop's 500. seq 5 is a tool row and stays in the list.
_ACTIVITY = (
    ("activity", "task-rich00001", {}, 200),
    ("activity-since", "task-rich00001", {"since": "1"}, 200),
    ("activity-limit-zero", "task-rich00001", {"limit": "0"}, 200),
    ("activity-neg-limit", "task-rich00001", {"limit": "-1"}, 200),
    ("activity-bad-since", "task-rich00001", {"since": "nope"}, 500),
    ("activity-bad-limit", "task-rich00001", {"limit": "nope"}, 500),
)
# Detail is the row, and the task id in the path is not part of the query,
# so task-min000001's seq 1 is the rich task's row. 99 is not a row.
_ACTIVITY_DETAIL = (
    ("activity-detail", "task-rich00001", 1, 200),
    ("activity-detail-other", "task-min000001", 1, 200),
    ("activity-missing", "task-rich00001", 99, 404),
)
# text, phase, error, and user_message. The tool row is left out.
# task-min000001 has no activity.
_CONVERSATION = (
    ("conversation", "task-rich00001", 200),
    ("conversation-empty", "task-min000001", 200),
)
# Role configs. A missing role is the handler's 404.
_ROLES = (("roles", 200),)
_ROLE = (
    ("role-planner", "planner", 200),
    ("role-missing", "missing", 404),
)
# Channel views add start_problem and has_bot_token. The bot token is not in
# the body. telegram-empty's allowlist is empty. web has no allowlist.
_CHANNELS = (("channel-configs", 200),)
_CHANNEL = (
    ("channel-lark", "lark", 200),
    ("channel-telegram-empty", "telegram-empty", 200),
    ("channel-web", "web", 200),
    ("channel-missing", "missing", 404),
)
# The saved dashboard chat is on the telegram allowlist, so the problem is null.
_GATE = (("gate-settings", 200),)
# Alpha has one template and one role item. Beta has neither. The missing
# project is the handler's 404, through _answer.
_TEMPLATES = (
    ("role-templates-alpha", "proj-alpha0001", 200),
    ("role-templates-beta", "proj-beta00002", 200),
    ("role-templates-missing", "proj-missing01", 404),
)
# Alpha's list is three profiles, so the project is restricted and choosable
# is that list. Beta's list is empty, so every Worker or Reviewer profile is
# choosable. A missing project is the handler's 404.
_ALLOWED = (
    ("allowed-profiles-alpha", "proj-alpha0001", 200),
    ("allowed-profiles-beta", "proj-beta00002", 200),
    ("allowed-profiles-missing", "proj-missing01", 404),
)
# 2010-01-01, 2010-W01-1, and 20100101 are all before the rich task, so those
# stats match the unfiltered alpha body. 2099 includes no task. 2010-W53-1
# is not a day. The missing project is 404.
_ROLE_STATS = (
    ("role-stats-alpha", "proj-alpha0001", None, 200),
    ("role-stats-since", "proj-alpha0001", "2010-01-01", 200),
    ("role-stats-future", "proj-alpha0001", "2099-01-01", 200),
    ("role-stats-week", "proj-alpha0001", "2010-W01-1", 200),
    ("role-stats-compact", "proj-alpha0001", "20100101", 200),
    ("role-stats-beta", "proj-beta00002", None, 200),
    ("role-stats-missing", "proj-missing01", None, 404),
    ("role-stats-bad-since", "proj-alpha0001", "nope", 400),
    ("role-stats-bad-week", "proj-alpha0001", "2010-W53-1", 400),
)
# A gate's discussion, oldest first. A missing task and a missing label share
# the "has no gate" 404. task-min000001 has no gate g1.
_GATE_MESSAGES = (
    ("gate-messages", "task-rich00001", "g1", 200),
    ("gate-messages-missing-gate", "task-rich00001", "nope", 404),
    ("gate-messages-missing-task", "task-missing01", "g1", 404),
    ("gate-messages-min", "task-min000001", "g1", 404),
)
# The task page's room. Filters narrow messages and leave the counts. limit=1
# keeps the newest message and sets more. A non-digit limit is 400. A
# superscript or circled digit passes str.isdigit, then int() raises
# ValueError and the request loop answers 500.
_ROOM = (
    ("room", "task-rich00001", {}, 200),
    ("room-min", "task-min000001", {}, 200),
    ("room-missing", "task-missing01", {}, 404),
    ("room-kind", "task-rich00001", {"kind": "question"}, 200),
    ("room-from", "task-rich00001", {"from": "backend"}, 200),
    ("room-unanswered", "task-rich00001", {"unanswered": "1"}, 200),
    ("room-limit", "task-rich00001", {"limit": "1"}, 200),
    ("room-after", "task-rich00001", {"after": "1"}, 200),
    ("room-bad-limit", "task-rich00001", {"limit": "nope"}, 400),
    ("room-limit-superscript", "task-rich00001", {"limit": "²"}, 500),
    ("room-limit-circled", "task-rich00001", {"limit": "②"}, 500),
    ("room-after-superscript", "task-rich00001", {"after": "²"}, 500),
    ("room-after-circled", "task-rich00001", {"after": "②"}, 500),
)
# The absent directory is not a git checkout and is not the seeded /work/alpha
# path. A golden for /work/alpha would follow this machine's repository.
# LOOPER_OPERATOR_NOTES_ROOT is a temp directory, so the notes are empty and
# ~/.claude is not read. project_id is checked before mode. A relative dir is
# refused before mode. base with in_place is refused before git.
_ABSENT = "/loopgo-parity-absent-workspace"
_WORKSPACES = (
    ("workspace-missing-project", {"project_id": "proj-missing01"}, 404),
    ("workspace-missing-mode", {"project_id": "proj-missing01", "mode": "nope"}, 404),
    ("workspace-no-dir", {}, 400),
    ("workspace-blank-project", {"project_id": "proj-blank0001"}, 400),
    ("workspace-relative", {"dir": "relative"}, 400),
    ("workspace-relative-mode", {"dir": "rel", "mode": "nope"}, 400),
    ("workspace-bad-mode", {"dir": _ABSENT, "mode": "nope"}, 400),
    ("workspace-base-inplace", {"dir": _ABSENT, "mode": "in_place", "base": "main"}, 400),
    ("workspace-absent", {"dir": _ABSENT}, 200),
    ("workspace-absent-asked", {"dir": _ABSENT, "mode": "worktree"}, 200),
    ("workspace-absent-base", {"dir": _ABSENT, "mode": "worktree", "base": "main"}, 200),
    ("workspace-absent-inplace", {"dir": _ABSENT, "mode": "in_place"}, 200),
    ("workspace-absent-project", {"project_id": "proj-alpha0001", "dir": _ABSENT}, 200),
)
# One seeded schedule on Alpha. Blank has none. limits.default_timezone is
# this machine's zone and is marked volatile.
_PROJECT_SCHEDULES = (
    ("schedules-project", "proj-alpha0001", 200),
    ("schedules-project-blank", "proj-blank0001", 200),
    ("schedules-project-missing", "proj-missing01", 404),
)
_ALL_SCHEDULES = (
    ("schedules", 200),
)
_SCHEDULE = (
    ("schedule", "sched-nightly01", 200),
    ("schedule-missing", "sched-missing01", 404),
)
# Newest run is run-skipbudgtdy. hide drops skipped_gate rows and still counts
# them in routine_skips. before=run-nope is 400 only after the schedule exists.
# ² passes str.isdigit and int() raises, so the request loop answers 500.
# 4,300 nines is a decimal integer outside 1..200 (400). 4,301 nines is the
# same 500 as ².
_RUNS = (
    ("schedule-runs", "sched-nightly01", {}, 200),
    ("schedule-runs-limit", "sched-nightly01", {"limit": "1"}, 200),
    ("schedule-runs-hide", "sched-nightly01", {"skips": "hide"}, 200),
    ("schedule-runs-before", "sched-nightly01", {"before": "run-skipbudgtdy"}, 200),
    ("schedule-runs-bad-limit", "sched-nightly01", {"limit": "0"}, 400),
    ("schedule-runs-bad-skips", "sched-nightly01", {"skips": "nope"}, 400),
    ("schedule-runs-bad-before", "sched-nightly01", {"before": "run-nope"}, 400),
    ("schedule-runs-missing", "sched-missing01", {}, 404),
    ("schedule-runs-limit-digits", "sched-nightly01", {"limit": "9" * 4300}, 400),
)
_RUNS_500 = (
    ("schedule-runs-limit-superscript", "sched-nightly01", {"limit": "²"}, 500),
    ("schedule-runs-limit-overflow", "sched-nightly01", {"limit": "9" * 4301}, 500),
)
_NOTIFICATIONS = (
    ("schedule-notifications", "sched-nightly01", {}, 200),
    ("schedule-notifications-limit", "sched-nightly01", {"limit": "1"}, 200),
    ("schedule-notifications-missing", "sched-missing01", {}, 404),
    ("schedule-notifications-bad-limit", "sched-nightly01", {"limit": "0"}, 400),
)
_NOTIFICATIONS_500 = (
    ("schedule-notifications-superscript", "sched-nightly01", {"limit": "²"}, 500),
)
# Global settings, three projects with different layers, and an unknown id.
# Alpha's Worker effective profile is the role-in-project row. The task
# override is not loaded. Blank's empty harness and null env differ from Beta.
_AGENT_SETTINGS = (
    ("agent-settings", None, 200),
    ("agent-settings-alpha", "proj-alpha0001", 200),
    ("agent-settings-beta", "proj-beta00002", 200),
    ("agent-settings-blank", "proj-blank0001", 200),
    ("agent-settings-missing", "proj-missing01", 404),
)
# GET /api/registry. The login homes stay empty. The fake codex says it is
# not logged in. The directories are unique. normalize rewrites them to the
# placeholders the goldens and the Go harness share.
_REGISTRY = (("registry", 200),)
_PARITY_PLACEHOLDER_BIN = "/tmp/loopgo-parity-bin"
_PARITY_PLACEHOLDERS = (
    ("bin", _PARITY_PLACEHOLDER_BIN),
    ("codex-home", "/tmp/loopgo-parity-codex-home"),
    ("grok-home", "/tmp/loopgo-parity-grok-home"),
    ("empty-home", "/tmp/loopgo-parity-empty-home"),
)
_parity_root: Path | None = None
_parity_dirs: dict[str, Path] = {}
_parity_rewrites: list[tuple[str, str]] = []
_REFUSAL = "refusing database URL: want 127.0.0.1:54329 and a loopgo_ database name"
# Local noon on this day is the parity clock. Keep it in step with parity_test.go.
_PARITY_DAY = date(2026, 6, 15)


# New project fixture is loaded after the legacy responses are captured.
_KNOWLEDGE = (
    ("knowledge", "proj-knowledge01", None, 200),
    ("knowledge-empty", "proj-knowledge02", None, 200),
    ("knowledge-missing", "proj-missing01", None, 404),
    ("document-text", "proj-knowledge01", "doc-text001", 200),
    ("document-upload", "proj-knowledge01", "doc-upload01", 200),
    ("document-unicode", "proj-knowledge01", "doc-unicode01", 200),
    ("document-live", "proj-knowledge01", "doc-live001", 200),
    ("document-file-missing", "proj-knowledge01", "doc-missing01", 200),
    ("document-binary", "proj-knowledge01", "doc-binary01", 200),
    ("document-invalid-utf8", "proj-knowledge01", "doc-invalid01", 200),
    ("document-large", "proj-knowledge01", "doc-large001", 200),
    ("document-symlink", "proj-knowledge01", "doc-symlink01", 200),
    ("document-parent", "proj-knowledge01", "doc-parent01", 200),
    ("document-directory", "proj-knowledge01", "doc-dir0001", 200),
    ("document-missing", "proj-knowledge01", "doc-absent01", 404),
    ("document-cross-project", "proj-knowledge02", "doc-text001", 404),
    ("document-project-missing", "proj-missing01", "doc-text001", 404),
)
_DRAFTS = (
    ("knowledge-drafts", "proj-knowledge01", 200),
    ("knowledge-drafts-empty", "proj-knowledge02", 200),
    ("knowledge-drafts-missing", "proj-missing01", 404),
)
_SKILLS = (
    ("project-skill", "proj-knowledge01", "skill-project001", 200),
    ("project-skill-file-missing", "proj-knowledge01", "skill-nofile001", 200),
    ("project-skill-raw-body", "proj-knowledge01", "skill-rawbody01", 200),
    ("project-skill-cross-project", "proj-knowledge02", "skill-project001", 404),
    ("project-skill-missing", "proj-knowledge01", "skill-absent01", 404),
    ("project-skill-other-origin", "proj-knowledge01", "skill-otherorigin01", 404),
    ("project-skill-project-missing", "proj-missing01", "skill-project001", 404),
)
_KNOWLEDGE_PLACEHOLDER = "<knowledge-root>"

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]  # go/testdata/parity -> worktree root


def normalize(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _VOLATILE:
                out[key] = "<volatile>"
                continue
            # A build id is a string. Requirement and role versions are numbers.
            if key == "version" and isinstance(item, str):
                out[key] = "<volatile>"
                continue
            if key in _VOLATILE_TIMES:
                out[key] = "<time>"
                continue
            out[key] = normalize(item)
        if "index" in out and "index_chars" in out:
            out["index_chars"] = len(out["index"])
        return out
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, str):
        return _rewrite_parity_text(value)
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


# One raw _json_response per name, in the same order as _names().
_RAW: list[bytes] = []


def response_json(raw: bytes, expect: int = 200):
    _RAW.append(raw)
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
    parts = status_line.split(" ", 2)
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if code != expect:
        raise SystemExit(f"handler returned {status_line}, want {expect}")
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
    """Make overview and schedule midnights the local midnight of _PARITY_DAY.

    The schedule clock is local noon that day, the same instant the Go harness
    stores on Server.Now and Store.Now.
    """
    import looper.agents.overview as overview
    import looper.agents.schedules as schedules
    from datetime import datetime

    class _Day(date):
        @classmethod
        def today(cls) -> date:
            return _PARITY_DAY

    overview.date = _Day
    noon = datetime(_PARITY_DAY.year, _PARITY_DAY.month, _PARITY_DAY.day, 12, 0, 0).astimezone()
    real_midnight = schedules.local_midnight

    def _midnight(now=None):
        return real_midnight(noon)

    schedules.local_midnight = _midnight


async def capture(dsn: str) -> dict[str, object]:
    import looper.db.connection as dbconn
    from looper.agents.bus import MessageBus
    from looper.agents.channels.web import WebChannelAdapter
    from looper.agents import agent_settings, projects, role_api, schedules
    from looper.agents.gate_service import gate_settings, thread as gate_thread
    from looper.agents.http_server import HTTP_200, HTTP_500, AgentHTTPServer, _json_response
    from looper.agents.supervisor import AgentSupervisor
    from looper.agents.tiers.task import TaskRouter

    # A stray configured_dsn() must not open .env or the default loopx database.
    dbconn._load_env = lambda: None
    _freeze_today()
    await dbconn.init_pool(dsn)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A non-empty root. An empty value is falsy and would read ~/.claude.
            os.environ["LOOPER_OPERATOR_NOTES_ROOT"] = str(root / "operator-notes")
            (root / "operator-notes").mkdir()
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

            async def take(call, expect: int):
                # list_llm_records parses limit and offset outside its try.
                # The request loop turns that ValueError into 500. This script
                # calls the handler directly, so it writes that body when the
                # handler wrote nothing.
                cap = Capture()
                try:
                    await call(cap)
                except ValueError:
                    if not cap.buf:
                        cap.write(_json_response(HTTP_500, {"error": "internal server error"}))
                return response_json(bytes(cap.buf), expect)

            out = {}
            handlers = {
                # No password session and no token cookie: can_sign_out stays false.
                "status": lambda writer: server._handle_status(writer, {}),
                "projects": server._handle_list_projects,
                "overview": server._handle_overview,
            }
            for name, expect in _READS:
                out[name] = await take(handlers[name], expect)
            for name, query, expect in _TASKS:
                out[name] = await take(
                    lambda cap, query=query: server._handle_list_tasks(query, cap), expect)
            for name, task_id, expect in _DETAILS:
                out[name] = await take(
                    lambda cap, task_id=task_id: server._handle_task_detail(task_id, cap), expect)
            for name, agent, query, expect in _USAGE:
                out[name] = await take(
                    lambda cap, agent=agent, query=query: server._handle_agent_usage(agent, query, cap),
                    expect)
            for name, query, expect in _RECORDS:
                out[name] = await take(
                    lambda cap, query=query: server._handle_list_llm_records(query, cap), expect)
            for name, record_id, expect in _RECORD:
                out[name] = await take(
                    lambda cap, record_id=record_id: server._handle_get_llm_record(record_id, cap),
                    expect)
            for name, record_id, query, expect in _EVENTS:
                out[name] = await take(
                    lambda cap, record_id=record_id, query=query: server._handle_llm_record_events(
                        record_id, query, cap),
                    expect)
            for name, expect in _CHAT_TARGETS:
                out[name] = await take(server._handle_chat_targets, expect)
            for name, expect in _CHAT_SESSIONS:
                out[name] = await take(server._handle_chat_sessions, expect)
            for name, query, expect in _CHAT_MESSAGES:
                out[name] = await take(
                    lambda cap, query=query: server._handle_chat_messages(query, cap), expect)
            for name, task_id, query, expect in _ACTIVITY:
                out[name] = await take(
                    lambda cap, task_id=task_id, query=query: server._handle_task_activity(
                        task_id, query, cap),
                    expect)
            for name, task_id, seq, expect in _ACTIVITY_DETAIL:
                out[name] = await take(
                    lambda cap, task_id=task_id, seq=seq: server._handle_task_activity_detail(
                        task_id, seq, cap),
                    expect)
            for name, task_id, expect in _CONVERSATION:
                out[name] = await take(
                    lambda cap, task_id=task_id: server._handle_task_conversation(task_id, cap),
                    expect)

            async def answered(call, expect: int):
                # Gate settings, templates, and role stats go through _answer,
                # so a RequestError is 400 or 404 rather than an empty buffer.
                cap = Capture()
                await server._answer(cap, HTTP_200, call)
                return response_json(bytes(cap.buf), expect)

            async def answered_room(call, expect: int):
                # int("²") raises ValueError inside _task_room. _answer does
                # not catch it. The request loop writes 500.
                cap = Capture()
                try:
                    await server._answer(cap, HTTP_200, call)
                except ValueError:
                    if not cap.buf:
                        cap.write(_json_response(HTTP_500, {"error": "internal server error"}))
                return response_json(bytes(cap.buf), expect)

            for name, expect in _ROLES:
                out[name] = await take(server._handle_list_role_configs, expect)
            for name, role, expect in _ROLE:
                out[name] = await take(
                    lambda cap, role=role: server._handle_get_role_config(role, cap), expect)
            for name, expect in _CHANNELS:
                out[name] = await take(server._handle_list_channel_configs, expect)
            for name, channel_id, expect in _CHANNEL:
                out[name] = await take(
                    lambda cap, channel_id=channel_id: server._handle_get_channel_config(channel_id, cap),
                    expect)
            for name, expect in _GATE:
                out[name] = await answered(gate_settings, expect)
            for name, project_id, expect in _TEMPLATES:
                out[name] = await answered(
                    lambda project_id=project_id: role_api.templates_view(project_id), expect)
            for name, project_id, expect in _ALLOWED:
                out[name] = await answered(
                    lambda project_id=project_id: projects.allowed_profiles_view(project_id), expect)
            for name, project_id, since, expect in _ROLE_STATS:
                out[name] = await answered(
                    lambda project_id=project_id, since=since: role_api.role_stats(project_id, since),
                    expect)
            for name, task_id, label, expect in _GATE_MESSAGES:
                out[name] = await answered(
                    lambda task_id=task_id, label=label: gate_thread(task_id, label), expect)
            for name, task_id, query, expect in _ROOM:
                out[name] = await answered_room(
                    lambda task_id=task_id, query=query: server._task_room(task_id, query), expect)
            for name, query, expect in _WORKSPACES:
                out[name] = await answered(
                    lambda query=query: projects.check_workspace(query), expect)
            for name, project_id, expect in _PROJECT_SCHEDULES:
                out[name] = await answered(
                    lambda project_id=project_id: schedules.list_schedules(project_id), expect)
            for name, expect in _ALL_SCHEDULES:
                out[name] = await answered(schedules.list_all_schedules, expect)
            for name, schedule_id, expect in _SCHEDULE:
                out[name] = await answered(
                    lambda schedule_id=schedule_id: schedules.get_schedule(schedule_id), expect)
            for name, schedule_id, query, expect in _RUNS:
                out[name] = await answered(
                    lambda schedule_id=schedule_id, query=query: schedules.list_runs(schedule_id, query),
                    expect)
            for name, schedule_id, query, expect in _RUNS_500:
                out[name] = await answered_room(
                    lambda schedule_id=schedule_id, query=query: schedules.list_runs(schedule_id, query),
                    expect)
            for name, schedule_id, query, expect in _NOTIFICATIONS:
                out[name] = await answered(
                    lambda schedule_id=schedule_id, query=query: schedules.list_notifications(
                        schedule_id, query),
                    expect)
            for name, schedule_id, query, expect in _NOTIFICATIONS_500:
                out[name] = await answered_room(
                    lambda schedule_id=schedule_id, query=query: schedules.list_notifications(
                        schedule_id, query),
                    expect)

            async def answered_settings(call, expect: int):
                cap = Capture()
                await server._handle_agent_settings(cap, call)
                return response_json(bytes(cap.buf), expect)

            for name, project_id, expect in _AGENT_SETTINGS:
                if project_id is None:
                    out[name] = await answered_settings(agent_settings.settings_view, expect)
                else:
                    out[name] = await answered_settings(
                        lambda project_id=project_id: agent_settings.project_settings_view(project_id),
                        expect)
            out.update(await _capture_registry(server, take))
            out.update(await _capture_knowledge(server, root))
            return out
    finally:
        await dbconn.close_pool()


async def _capture_knowledge(server, root: Path) -> dict[str, object]:
    from looper.agents import knowledge, project_skills
    from looper.agents.http_server import HTTP_200
    from looper.db.connection import get_pool

    # Every path and symlink target is below this owned fixture root. An escaped
    # symlink points to a harmless sibling, and must never yield its sentinel.
    root = root.resolve()
    project = root / "project"
    project.mkdir()
    (root / "empty").mkdir()
    (project / "subdir").mkdir()
    (project / "live.md").write_text("# Live\n最新内容 🌍\n", encoding="utf-8")
    (project / "binary.dat").write_bytes(b"text\x00binary")
    (project / "invalid.dat").write_bytes(b"\xffinvalid")
    (project / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    (root / "outside.md").write_text("OUTSIDE_SENTINEL", encoding="utf-8")
    (project / "escaped.md").symlink_to(root / "outside.md")
    sql = (HERE / "knowledge_fixture.sql").read_text(encoding="utf-8")
    await get_pool().execute(sql.replace("__KNOWLEDGE_ROOT__", str(root).replace("'", "''")))
    server._runtime_root = root
    _parity_rewrites.append((str(root), _KNOWLEDGE_PLACEHOLDER))

    class Capture:
        def __init__(self):
            self.buf = bytearray()
        def write(self, data):
            self.buf.extend(data)
        async def drain(self):
            pass

    async def take(call, expect, catalog=False):
        cap = Capture()
        if catalog:
            await server._handle_catalog(cap, HTTP_200, call)
        else:
            await server._handle_knowledge(cap, HTTP_200, lambda _: call())
        value = response_json(bytes(cap.buf), expect)
        if "index_chars" in value and value["index_chars"] != len(value["index"]):
            raise SystemExit("knowledge index_chars does not count the real index")
        return value

    out = {}
    for name, project_id, document_id, expect in _KNOWLEDGE:
        if document_id is None:
            call = lambda p=project_id: knowledge.knowledge_view(p, root)
        else:
            call = lambda p=project_id, d=document_id: knowledge.get_document(p, d, root)
        out[name] = await take(call, expect)
    for name, project_id, expect in _DRAFTS:
        out[name] = await take(lambda p=project_id: knowledge.drafts_view(p), expect)
    for name, project_id, skill_id, expect in _SKILLS:
        out[name] = await take(lambda p=project_id, s=skill_id: project_skills.get_skill(p, s), expect, True)
    return out


def _names() -> list[str]:
    return (
        [name for name, _ in _READS]
        + [name for name, _, _ in _TASKS]
        + [name for name, _, _ in _DETAILS]
        + [name for name, _, _, _ in _USAGE]
        + [name for name, _, _ in _RECORDS]
        + [name for name, _, _ in _RECORD]
        + [name for name, _, _, _ in _EVENTS]
        + [name for name, _ in _CHAT_TARGETS]
        + [name for name, _ in _CHAT_SESSIONS]
        + [name for name, _, _ in _CHAT_MESSAGES]
        + [name for name, _, _, _ in _ACTIVITY]
        + [name for name, _, _, _ in _ACTIVITY_DETAIL]
        + [name for name, _, _ in _CONVERSATION]
        + [name for name, _ in _ROLES]
        + [name for name, _, _ in _ROLE]
        + [name for name, _ in _CHANNELS]
        + [name for name, _, _ in _CHANNEL]
        + [name for name, _ in _GATE]
        + [name for name, _, _ in _TEMPLATES]
        + [name for name, _, _ in _ALLOWED]
        + [name for name, _, _, _ in _ROLE_STATS]
        + [name for name, _, _, _ in _GATE_MESSAGES]
        + [name for name, _, _, _ in _ROOM]
        + [name for name, _, _ in _WORKSPACES]
        + [name for name, _, _ in _PROJECT_SCHEDULES]
        + [name for name, _ in _ALL_SCHEDULES]
        + [name for name, _, _ in _SCHEDULE]
        + [name for name, _, _, _ in _RUNS]
        + [name for name, _, _, _ in _RUNS_500]
        + [name for name, _, _, _ in _NOTIFICATIONS]
        + [name for name, _, _, _ in _NOTIFICATIONS_500]
        + [name for name, _, _ in _AGENT_SETTINGS]
        + [name for name, _ in _REGISTRY]
        + [name for name, _, _, _ in _KNOWLEDGE]
        + [name for name, _, _ in _DRAFTS]
        + [name for name, _, _, _ in _SKILLS]
    )


def _rewrite_parity_text(text: str) -> str:
    """Put the unique parity directories back to the golden placeholders."""
    for actual, placeholder in _parity_rewrites:
        if actual:
            text = text.replace(actual, placeholder)
    return text


def _open_dir(path: Path) -> int:
    """A directory fd that does not follow a symlink at path."""
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _discard_parity_root() -> None:
    """Remove the unique login directory. A symlink is unlinked, not followed."""
    global _parity_root
    root = _parity_root
    _parity_root = None
    if root is None:
        return
    if root.is_symlink():
        root.unlink(missing_ok=True)
        return
    shutil.rmtree(root, ignore_errors=True)


def _prepare_registry_login() -> None:
    """Fake executables and empty login homes in a new directory.

    A fixed path under /tmp can already be a symlink. Writing the executable
    would follow it, and unlinking home/auth.json would delete the target.
    The directory is new. Writes use O_EXCL and O_NOFOLLOW. auth.json is
    removed by dir_fd, which unlinks that name. Does not open auth.json.
    """
    global _parity_root, _parity_dirs, _parity_rewrites
    _discard_parity_root()
    root = Path(tempfile.mkdtemp(prefix="loopgo-parity-login-"))
    if root.is_symlink() or not root.is_dir():
        raise SystemExit("parity login root is not a directory")
    _parity_root = root
    dirs: dict[str, Path] = {}
    rewrites: list[tuple[str, str]] = []
    parent = _open_dir(root)
    try:
        os.mkdir("bin", 0o755, dir_fd=parent)
    finally:
        os.close(parent)
    bin_dir = root / "bin"
    if bin_dir.is_symlink():
        raise SystemExit("parity bin is a symlink")
    dirs["bin"] = bin_dir
    rewrites.append((str(bin_dir), _PARITY_PLACEHOLDER_BIN))
    for name in ("claude", "codex", "omp", "grok"):
        if name == "codex":
            body = "#!/bin/sh\necho 'Not logged in' >&2\nexit 1\n"
        else:
            body = "#!/bin/sh\nexit 0\n"
        _write_exec(bin_dir, name, body)
    for dirname, placeholder in _PARITY_PLACEHOLDERS:
        if dirname == "bin":
            continue
        parent = _open_dir(root)
        try:
            os.mkdir(dirname, 0o755, dir_fd=parent)
        finally:
            os.close(parent)
        home = root / dirname
        if home.is_symlink():
            raise SystemExit(f"parity home is a symlink: {dirname}")
        _remove_auth(home)
        dirs[dirname] = home
        rewrites.append((str(home), placeholder))
    rewrites.sort(key=lambda item: len(item[0]), reverse=True)
    _parity_dirs = dirs
    _parity_rewrites = rewrites


def _write_exec(directory: Path, name: str, body: str) -> None:
    """Create name. O_EXCL fails when it exists, so a symlink is not opened."""
    dirfd = _open_dir(directory)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(name, flags, 0o755, dir_fd=dirfd)
        try:
            os.write(fd, body.encode("utf-8"))
        finally:
            os.close(fd)
    finally:
        os.close(dirfd)


def _remove_auth(home: Path) -> None:
    """Unlink auth.json in home. dir_fd does not follow a symlink home."""
    dirfd = _open_dir(home)
    try:
        try:
            os.unlink("auth.json", dir_fd=dirfd)
        except FileNotFoundError:
            return
    finally:
        os.close(dirfd)


async def _capture_registry(server, take) -> dict[str, object]:
    """GET /api/registry with the parity login environment, then restore it.

    The process environment is changed only for this call. CODEX_HOME and
    GROK_HOME are empty unique homes, so the probes do not use ~/.codex or
    ~/.grok. forget_probe runs on both sides, so this capture is not a cached
    probe. The bodies still contain the unique paths; normalize rewrites them.
    """
    from looper.agents import profile_registry
    from looper.agents.http_server import HTTP_200
    from looper.agents.runtimes import codex_login, grok_login
    from looper.db.connection import get_pool

    keys = ("PATH", "CODEX_HOME", "GROK_HOME", "HOME")
    saved = {key: os.environ.get(key) for key in keys}
    saved_bin = server._claude_bin
    _prepare_registry_login()
    bin_dir = _parity_dirs["bin"]
    status = await get_pool().execute(
        "UPDATE agent_runtimes SET executable = replace(executable, $1, $2) "
        "WHERE executable LIKE $1 || '/%'",
        _PARITY_PLACEHOLDER_BIN,
        str(bin_dir),
    )
    if status != "UPDATE 2":
        raise SystemExit(f"parity executables updated {status}")
    os.environ["PATH"] = str(bin_dir) + os.pathsep + (saved["PATH"] or "")
    os.environ["CODEX_HOME"] = str(_parity_dirs["codex-home"])
    os.environ["GROK_HOME"] = str(_parity_dirs["grok-home"])
    os.environ["HOME"] = str(_parity_dirs["empty-home"])
    server._claude_bin = str(bin_dir / "claude")
    codex_login.forget_probe()
    grok_login.forget_probe()
    try:
        out = {}
        for name, expect in _REGISTRY:
            out[name] = await take(
                lambda cap: server._handle_registry(
                    cap, HTTP_200,
                    lambda: profile_registry.registry_view(server_claude=server._claude_bin),
                ),
                expect,
            )
        return out
    finally:
        server._claude_bin = saved_bin
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        codex_login.forget_probe()
        grok_login.forget_probe()
        _discard_parity_root()


def _status_code(raw: bytes) -> int:
    line = raw.split(b"\r\n", 1)[0].decode("utf-8", "replace")
    parts = line.split(" ")
    if len(parts) < 2 or not parts[1].isdigit():
        raise SystemExit("bad status line")
    return int(parts[1])


def write_raw_goldens(names: list[str]) -> None:
    """Write exact Python response bytes. Does not rewrite sorted JSON."""
    if len(_RAW) != len(names):
        raise SystemExit(f"raw responses {len(_RAW)} != names {len(names)}")
    from looper.agents.http_server import HTTP_403, HTTP_404, _json_response, _no_content
    from looper.agents.request_guard import refusal

    raw_dir = HERE / "raw"
    raw_dir.mkdir(exist_ok=True)
    for name, raw in zip(names, _RAW):
        if name == "status" or _status_code(raw) != 200:
            (raw_dir / f"{name}.http").write_bytes(raw)
    (raw_dir / "error-not-found.http").write_bytes(
        _json_response(HTTP_404, {"error": "not found"}))
    host = refusal({"host": "evil.example:8767"}, bind_host="127.0.0.1")
    origin = refusal(
        {"host": "127.0.0.1:8767", "origin": "https://attacker.example"},
        bind_host="127.0.0.1",
    )
    if not host or not origin:
        raise SystemExit("refusal fixture was allowed")
    (raw_dir / "error-bad-host.http").write_bytes(_json_response(HTTP_403, {"error": host}))
    (raw_dir / "error-cross-origin.http").write_bytes(
        _json_response(HTTP_403, {"error": origin}))
    (raw_dir / "no-content.http").write_bytes(_no_content())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", required=True)
    parser.add_argument(
        "--raw-only",
        action="store_true",
        help="write raw status and non-200 responses; do not rewrite sorted JSON",
    )
    args = parser.parse_args()
    check_db_url(args.db_url)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    asyncio.run(apply_sql(args.db_url))
    bodies = asyncio.run(capture(args.db_url))
    names = _names()
    if args.raw_only:
        write_raw_goldens(names)
        return
    for name in names:
        payload = normalize(bodies[name])
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (HERE / f"{name}.json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
