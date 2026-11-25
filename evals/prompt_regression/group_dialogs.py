"""Group dialog transport, ported from #1710; only synthetic native CLI traffic."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import Literal
from urllib.parse import urlsplit
import uuid

from looper import ops_config
from looper.agents.message import channel_thread_id
from looper.agents.session_routing import session_digest

from .fake_lark import APP, BOT, BOT_SENDER, CHAT, OPERATOR, OTHER
from .runner import Api, EvalError, INTERRUPTED, RESERVED_PORT, _confirmed

SESSION = f"group:channel-lark:{CHAT}"
SENDERS = {"operator": OPERATOR, "member": OTHER, "bot": BOT_SENDER}


def _read(database_url, query, *args):
    import asyncpg

    async def read():
        conn = await asyncpg.connect(database_url, timeout=10, command_timeout=30)
        try:
            return [dict(row) for row in await conn.fetch(query, *args)]
        finally:
            await conn.close()

    return asyncio.run(read())


def enable_channel(database_url: str) -> None:
    """Enable the stand-in before serve reads its channels; no events are sent yet."""
    import asyncpg
    from looper.db.connection import ensure_schema
    from looper.agents.serve import seed_channel_configs

    async def seed():
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            await ensure_schema(pool)
            await seed_channel_configs(pool)
            await pool.execute("UPDATE channel_configs SET enabled=true, config=$1::jsonb "
                               "WHERE channel_id='channel-lark'", json.dumps({
                                   "profile": "eval", "allowed_open_ids": list(SENDERS.values()),
                                   "operator_sender_id": OPERATOR, "allowlist_app_id": APP}))
        finally:
            await pool.close()

    asyncio.run(seed())


def _isolated(api: Api, root: Path) -> None:
    port = urlsplit(api.base_url).port
    stored = ops_config.read_file(root / "runtime" / ops_config.FILE, required=True).get("port")
    if port is None or port == RESERVED_PORT or stored != port:
        raise EvalError("group dialogs require an ops file pinned to their isolated server port")


def seed_server(database_url: str, *, manager: str, routing: Literal["per_chat", "scoped"],
                api: Api, root: Path) -> None:
    """Bind the synthetic group after the dialog's seed has created its scenario Manager.

    Channel startup is seeded separately by enable_channel, before serve starts. API operations
    require the running server and its operator credential, so routing and binding happen here.
    """
    _isolated(api, root)
    if routing == "per_chat":
        _confirmed(api, "/api/v1/scenario_session_routing/update", {
            "manager_id": manager, "session_routing": routing})
    elif routing == "scoped":
        # Until scoped is selectable, the switch-enabling card replaces this with the operation.
        rows = _read(database_url, "UPDATE managers SET session_routing = 'scoped' WHERE id=$1 RETURNING id", manager)
        if not rows:
            raise EvalError("the group dialog's scenario Manager does not exist")
    else:
        raise EvalError(f"unsupported group routing: {routing}")
    _confirmed(api, "/api/v1/manager_chat_bindings/put", {
        "channel_id": "channel-lark", "chat_id": CHAT, "manager_id": manager})


def prepare(api: Api, root: Path, dialog, database_url: str) -> None:
    if not dialog.manager or not dialog.routing:
        raise EvalError("group dialogs need a scenario Manager and an explicit routing mode")
    seed_server(database_url, manager=f"manager-{dialog.manager}", routing=dialog.routing, api=api, root=root)


def ask(api: Api, root: Path, dialog, text: str, *, sender: Literal["operator", "member", "bot"],
        database_url: str, timeout: float, check_budget=None) -> tuple[str, str | None]:
    from .dialogs import ReplyTimeout, _check_manager_run, _newest_run

    _isolated(api, root.parent)
    agent = f"manager-{dialog.manager}"
    thread = channel_thread_id("channel-lark", CHAT, agent, "manager")
    since = _newest_run(api, agent)
    before = _read(database_url, "SELECT COALESCE(max(id), 0) AS id FROM chat_messages WHERE session_id=$1", thread)[0]["id"]
    # Read the actual column once available; do not infer provenance from the routing policy.
    provenance = bool(_read(database_url, "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                            "AND table_name='chat_messages' AND column_name='manager_session'"))
    columns = "content, delivery_failure, " + ("manager_session" if provenance else "NULL::text AS manager_session")
    message_id = "om_eval" + uuid.uuid4().hex
    event = {"type": "im.message.receive_v1", "chat_id": CHAT, "chat_type": "group", "message_id": message_id,
             "message_type": "text", "sender_type": "app" if sender == "bot" else "user",
             "sender_id": SENDERS[sender], "content": text,
             "mentions": [{"id": BOT, "key": "@_user_1", "name": "Eval helper"}]}
    with (root / "events.jsonl").open("a") as out:
        out.write(json.dumps(event) + "\n")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if INTERRUPTED.is_set():
            raise EvalError("the run was interrupted")
        if check_budget:
            check_budget()
        rows = _read(database_url, f"SELECT {columns} FROM chat_messages WHERE id>$1 AND role='assistant' "
                     "AND session_id=$2 ORDER BY id LIMIT 1", before, thread)
        if rows:
            row = rows[0]
            if row["delivery_failure"]:
                raise EvalError(f"the group reply was not delivered: {row['delivery_failure']}")
            delivered = [json.loads(line) for line in (root / "delivered.jsonl").read_text().splitlines()]
            if not any(d["method"] == "+messages-reply" and d["target"] == message_id
                       and d["text"] == row["content"] for d in delivered):
                raise EvalError("the group reply did not pass through the native CLI stand-in")
            _check_manager_run(api, since, agent_id=agent)
            return row["content"], row["manager_session"]
        time.sleep(0.1)
    raise ReplyTimeout(f"no group reply within {timeout:g} s")


def smoke_check(look) -> str | None:
    if look.reply != f"Received in {CHAT}.":
        return "the reply did not name only this synthetic chat"
    rows = _read(look.database_url, "SELECT session_key FROM manager_sessions WHERE manager_id=$1 ORDER BY session_key",
                 look.manager_id)
    if [r["session_key"] for r in rows] != [SESSION]:
        return "the group did not use its single per-chat session key"
    record = look.api.post("/api/v1/runs/list", {"agent_id": look.manager_id, "limit": 1})["result"]["records"][0]
    expected = look.runtime_root / ".local" / "manager-sessions" / session_digest(look.manager_id, SESSION) / "work"
    if (record.get("metadata") or {}).get("cwd") != str(expected):
        return "the answering run did not execute in this group's session"
    return None


def seed_smoke(api: Api, project_id: str) -> None:
    api.post("/api/v1/managers/create", {"key": "group-smoke", "name": "Eval group desk", "tools": "all",
        "system_prompt": "Answer the messages addressed to you in this group."})
