"""Operator-owned native chat routing and metadata, without transcript access."""

from ...db import chat_bindings as storage
from ...db.connection import get_pool
from ..api_v1 import Operation, _array, _object, _BOOL, _NULL_STR, _STR
from ..channels import chat_routing
from ..ops import ErrorCode, OpError

_ID = {"type": "string", "minLength": 1}
_TIME = {"type": "string", "format": "date-time"}
_COUNT = {"type": "integer", "minimum": 1}
BINDING = _object({
    "channel_id": _ID, "chat_id": _ID, "manager_id": _ID, "created_at": _TIME,
    "channel_type": {"type": "string", "enum": ["telegram", "lark", "lark_bot"]},
    "bot_name": _STR, "chat_name": _NULL_STR, "manager_name": _STR,
    "manager_enabled": _BOOL, "manager_deleted": _BOOL,
}, closed=True)
UNBOUND_CHAT = _object({"chat_id": _ID, "name": _NULL_STR, "count": _COUNT, "last_at": _TIME}, closed=True)
SEEN_CHAT = _object({
    "chat_id": _ID, "name": _NULL_STR, "count": {**_COUNT, "type": ["integer", "null"]},
    "last_at": {**_TIME, "type": ["string", "null"]},
    "binding": {"anyOf": [BINDING, {"type": "null"}]},
    "effective_manager_id": _ID, "effective_manager_name": _STR,
    "effective_manager_enabled": _BOOL, "effective_manager_deleted": _BOOL,
}, closed=True)
SCHEMAS = {"Binding": BINDING, "SeenChat": SEEN_CHAT, "UnboundChat": UNBOUND_CHAT}


def _op(name, summary, request, result, *, risk="read"):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), summary, summary,
                     request, result, risk=risk, operator_only=True)


OPERATIONS = (
    _op("manager_chat_bindings.list", "List explicit native chat bindings.",
        _object({"channel_id": _ID, "manager_id": _ID}, [], closed=True),
        _object({"bindings": _array(BINDING)}, closed=True)),
    _op("manager_chat_bindings.put", "Bind or rebind a native chat to a Manager.",
        _object({"channel_id": _ID, "chat_id": _ID, "manager_id": _ID}, closed=True),
        _object({"binding": BINDING}, closed=True), risk="write"),
    _op("manager_chat_bindings.delete", "Remove an explicit native chat binding.",
        _object({"channel_id": _ID, "chat_id": _ID}, closed=True),
        _object({"channel_id": _ID, "chat_id": _ID, "deleted": _BOOL}, closed=True), risk="write"),
    _op("channel_chats.list", "List observed and explicitly bound native chats.",
        _object({"channel_id": _ID, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
                 "after": _ID}, ["channel_id"], closed=True),
        _object({"channel_id": _ID, "bot_manager_id": _ID, "bound_chats_only": _BOOL,
                 "chats": _array(SEEN_CHAT), "unbound_chats": {**_array(UNBOUND_CHAT), "maxItems": 20},
                 "next_cursor": _NULL_STR, "has_more": _BOOL}, closed=True)),
    _op("channel_chats.set_policy", "Set whether this bot admits only explicitly bound chats.",
        _object({"channel_id": _ID, "bound_chats_only": _BOOL}, closed=True),
        _object({"channel_id": _ID, "bound_chats_only": _BOOL}, closed=True), risk="write"),
)
RESULTS = {}

_BINDINGS = """
    SELECT b.channel_id, b.chat_id, b.manager_id, b.created_at, c.channel_type,
           c.display_name AS bot_name, s.name AS chat_name, m.name AS manager_name,
           (m.enabled AND m.deleted_at IS NULL) AS manager_enabled,
           (m.deleted_at IS NOT NULL) AS manager_deleted
    FROM manager_chat_bindings b JOIN channel_configs c ON c.channel_id = b.channel_id
    JOIN managers m ON m.id = b.manager_id
    LEFT JOIN channel_seen_chats s ON s.channel_id = b.channel_id AND s.chat_id = b.chat_id
"""


async def _bindings(conn, channel_id=None, manager_id=None, chat_ids=None):
    rows = await conn.fetch(_BINDINGS + """
        WHERE ($1::text IS NULL OR b.channel_id = $1) AND ($2::text IS NULL OR b.manager_id = $2)
          AND ($3::text[] IS NULL OR b.chat_id = ANY($3)) ORDER BY b.channel_id, b.chat_id
        """, channel_id, manager_id, chat_ids)
    return [{**dict(row), "created_at": row["created_at"].isoformat()} for row in rows]


async def _catalog(conn, arguments):
    channel_id = arguments["channel_id"]
    channel = await storage._channel(channel_id, conn)
    owner = await conn.fetchrow("SELECT name, enabled, deleted_at FROM managers WHERE id = $1", channel["manager_id"])
    limit = arguments.get("limit", 100)
    rows = await conn.fetch("""
        WITH identities AS (
            SELECT chat_id FROM channel_seen_chats WHERE channel_id = $1 UNION
            SELECT chat_id FROM manager_chat_bindings WHERE channel_id = $1)
        SELECT i.chat_id, s.name, s.count, s.last_at FROM identities i
        LEFT JOIN channel_seen_chats s ON s.channel_id = $1 AND s.chat_id = i.chat_id
        WHERE ($2::text IS NULL OR i.chat_id > $2) ORDER BY i.chat_id ASC LIMIT $3
        """, channel_id, arguments.get("after"), limit + 1)
    bindings = {row["chat_id"]: row for row in await _bindings(conn, channel_id, chat_ids=[r["chat_id"] for r in rows[:limit]])}
    chats = []
    for row in rows[:limit]:
        binding = bindings.get(row["chat_id"])
        chats.append({**dict(row), "last_at": row["last_at"].isoformat() if row["last_at"] else None,
            "binding": binding,
            "effective_manager_id": binding["manager_id"] if binding else channel["manager_id"],
            "effective_manager_name": binding["manager_name"] if binding else owner["name"],
            "effective_manager_enabled": binding["manager_enabled"] if binding else owner["enabled"] and owner["deleted_at"] is None,
            "effective_manager_deleted": binding["manager_deleted"] if binding else owner["deleted_at"] is not None})
    unbound = await conn.fetch("SELECT chat_id, name, count, last_at FROM channel_unbound_chats "
                               "WHERE channel_id = $1 ORDER BY last_at DESC, chat_id ASC LIMIT 20", channel_id)
    return {"channel_id": channel_id, "bot_manager_id": channel["manager_id"],
            "bound_chats_only": channel["bound_chats_only"], "chats": chats,
            "unbound_chats": [{**dict(row), "last_at": row["last_at"].isoformat()} for row in unbound],
            "has_more": len(rows) > limit, "next_cursor": chats[-1]["chat_id"] if len(rows) > limit else None}


async def run(server, operation, arguments, principal, headers):
    if operation.risk == "write" and server._channels is None:
        raise OpError(ErrorCode.UNAVAILABLE, "live channel routing is not available")
    try:
        # Reuse native identity validation at the service boundary, before any write.
        if "channel_id" in arguments:
            storage._kind(arguments["channel_id"])
        if "manager_id" in arguments:
            storage._manager_id(arguments["manager_id"])
        for field in ("chat_id", "after"):
            if field in arguments:
                storage._chat_id(arguments["channel_id"], arguments[field])
        name = operation.operation_id
        if name == "manager_chat_bindings.put":
            bindings = []
            async def readback(conn):
                bindings.extend(await _bindings(conn, arguments["channel_id"], chat_ids=[arguments["chat_id"]]))
            await chat_routing.bind_chat(**arguments, registry=server._channels, readback=readback)
            return {"binding": bindings[0]}
        if name == "manager_chat_bindings.delete":
            deleted = await chat_routing.unbind_chat(**arguments, registry=server._channels)
            return {**arguments, "deleted": deleted}
        if name == "channel_chats.set_policy":
            await chat_routing.set_chat_policy(arguments["channel_id"], arguments["bound_chats_only"], registry=server._channels)
            return dict(arguments)
        async with get_pool().acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            if name == "channel_chats.list":
                return await _catalog(conn, arguments)
            if "channel_id" in arguments:
                await storage._channel(arguments["channel_id"], conn)
            if "manager_id" in arguments and not await conn.fetchval(
                    "SELECT deleted_at IS NULL FROM managers WHERE id = $1", arguments["manager_id"]):
                raise LookupError("Manager not found")
            return {"bindings": await _bindings(conn, **arguments)}
    except LookupError:
        raise OpError(ErrorCode.NOT_FOUND, "channel or Manager not found") from None
    except ValueError:
        raise OpError(ErrorCode.INVALID, "invalid native chat routing request") from None
