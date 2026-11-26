"""Operator-owned scope policy; these operations are never Manager tools."""

import json
import logging

from ...db import chat_bindings as storage
from ...db.connection import get_pool
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _BOOL, _STR
from ..channels.allowlist import sender_ids
from ..ops import ErrorCode, OpError
from .chat_controls import _manager

logger = logging.getLogger(__name__)
_ID = {"type": "string", "minLength": 1}
_VERSION = {"type": "integer", "minimum": 1}
_ITEMS = _array({"type": "object"})
_ADDRESS = {"channel_id": _ID, "chat_id": _ID}
STOP_NOW = {"type": "boolean", "default": False,
            "description": "Interrupt a running turn of a session this change retires."}
_OVERRIDE = _object({"principal_id": _ID, "principal_kind": {"type": "string", "enum": ["person", "bot"]},
                     "sender_ids": _array(_STR), "scope_id": _ID,
                     "created_at": {"type": "string", "format": "date-time"}}, closed=True)
RESULTS = {}


def _op(name, summary, request, result, *, write=False):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), summary, summary, request, result,
                     risk="high" if write else "read", confirms=write, operator_only=True,
                     level=ActionLevel.INSIDE)  # Reads or stores Looper policy; sends nothing.


OPERATIONS = (
    _op("capability_sets.list", "List available capability sets.", _object({}, closed=True),
        _object({"sets": _array(_object({"id": _ID, "name": _STR, "builtin": _BOOL,
                                        "current_version": _VERSION, "items": _ITEMS}, closed=True))}, closed=True)),
    _op("capability_sets.get", "Read one capability set version.",
        _object({"id": _ID, "version": _VERSION}, ["id"], closed=True),
        _object({"id": _ID, "version": _VERSION, "items": _ITEMS}, closed=True)),
    _op("chat_scope_overrides.list", "List a chat's principal scope overrides.", _object(_ADDRESS, closed=True),
        _object({"overrides": _array(_OVERRIDE)}, closed=True)),
    _op("chat_scope_overrides.put", "Set one person or bot's chat scope override.",
        _object({**_ADDRESS, "principal_kind": _OVERRIDE["properties"]["principal_kind"],
                 "sender_ids": {"type": "array", "items": _ID, "minItems": 1, "maxItems": 2},
                 "scope_id": _ID, "stop_now": STOP_NOW},
                [*_ADDRESS, "principal_kind", "sender_ids", "scope_id"], closed=True), _OVERRIDE, write=True),
    _op("chat_scope_overrides.delete", "Remove one principal's chat scope override.",
        _object({**_ADDRESS, "principal_id": _ID, "stop_now": STOP_NOW},
                [*_ADDRESS, "principal_id"], closed=True), _object({"deleted": _BOOL}, closed=True), write=True),
)


async def reconcile(server, manager_id, arguments):
    """A post-commit failure cannot undo accepted policy; the next message retries."""
    try:
        agent = _manager(server, manager_id)
    except OpError as exc:
        if exc.code in (ErrorCode.NOT_FOUND, ErrorCode.UNAVAILABLE):
            return
        raise
    try:
        await agent.scope_config_changed(arguments["channel_id"], arguments["chat_id"],
                                         stop_now=arguments.get("stop_now", False))
    except Exception:
        logger.exception("could not reconcile committed chat scope policy for %s", manager_id)


def scope_impact(chat, scope, stop_now, *, legacy=False, principal=None):
    target = f"the group default of {chat}" if principal is None else f"the override for {principal} in {chat}"
    ending = "end now, interrupting a running turn" if stop_now else "finish their current turn and end"
    text = f"Set {target} to {scope}. Sessions of this chat that no longer match {ending}."
    if legacy:
        text += (" This chat used legacy: everyone admitted could run commands with the operator's system account. "
                 "Its Manager's system prompt may contain prose capability rules; scopes are enforced in code now, "
                 "so you can remove those rules from the Manager definition.")
    return text


async def _binding(conn, arguments, *, lock=False):
    storage._chat_id(arguments["channel_id"], arguments["chat_id"])
    row = await conn.fetchrow("SELECT manager_id, default_scope FROM manager_chat_bindings "
                              "WHERE channel_id = $1 AND chat_id = $2" + (" FOR UPDATE" if lock else ""),
                              arguments["channel_id"], arguments["chat_id"])
    if row is None:
        raise OpError(ErrorCode.NOT_FOUND, "chat is not bound")
    return row


async def _overrides(conn, arguments):
    rows = await conn.fetch("SELECT principal_id, principal_kind, scope_id, min(created_at) AS created_at, "
        "array_agg(sender_id ORDER BY (sender_id = principal_id) DESC, sender_id) AS sender_ids "
        "FROM binding_principal_scopes WHERE channel_id = $1 AND chat_id = $2 "
        "GROUP BY principal_id, principal_kind, scope_id ORDER BY principal_id",
        arguments["channel_id"], arguments["chat_id"])
    return [{**dict(row), "created_at": row["created_at"].isoformat()} for row in rows]


async def impact(server, operation, arguments, principal, headers):
    if operation.risk != "high":
        return None
    try:
        async with get_pool().acquire() as conn:
            binding = await _binding(conn, arguments)
        return scope_impact(arguments["chat_id"], arguments.get("scope_id", "the group default"),
                            arguments.get("stop_now", False), legacy=binding["default_scope"] == "legacy",
                            principal=arguments.get("principal_id") or arguments.get("sender_ids", [""])[0])
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None


async def run(server, operation, arguments, principal, headers):
    name = operation.operation_id
    try:
        async with get_pool().acquire() as conn:
            if name.startswith("capability_sets."):
                rows = await conn.fetch("SELECT s.id, s.name, s.builtin, v.version AS current_version, v.items "
                    "FROM capability_sets s JOIN capability_set_versions v ON v.set_id = s.id "
                    "AND v.version = coalesce($2::integer, s.current_version) WHERE s.deleted_at IS NULL "
                    "AND ($1::text IS NULL OR s.id = $1) ORDER BY s.id", arguments.get("id"), arguments.get("version"))
                sets = [{**dict(row), "items": json.loads(row["items"]) if isinstance(row["items"], str)
                         else row["items"]} for row in rows]
                if name == "capability_sets.list":
                    return {"sets": sets}
                if not sets:
                    raise OpError(ErrorCode.NOT_FOUND, "capability set or version not found")
                row = sets[0]
                return {"id": row["id"], "version": row["current_version"], "items": row["items"]}
            async with conn.transaction():
                binding = await _binding(conn, arguments, lock=operation.risk == "high")
                if name == "chat_scope_overrides.list":
                    return {"overrides": await _overrides(conn, arguments)}
                address = arguments["channel_id"], arguments["chat_id"]
                if name == "chat_scope_overrides.put":
                    ids = sender_ids(storage._kind(arguments["channel_id"]), arguments["sender_ids"])
                    if len(ids) != len(arguments["sender_ids"]) or not 1 <= len(ids) <= (
                            1 if arguments["principal_kind"] == "person" else 2):
                        raise ValueError("person takes exactly one id; bot takes one or two distinct ids")
                    await storage.validate_default_scope(conn, arguments["scope_id"])
                    held = await conn.fetchval("SELECT principal_id FROM binding_principal_scopes "
                        "WHERE channel_id = $1 AND chat_id = $2 AND sender_id = ANY($3::text[]) "
                        "AND principal_id <> $4 LIMIT 1", *address, list(ids), ids[0])
                    if held is not None:
                        raise OpError(ErrorCode.CONFLICT, f"sender id already belongs to principal {held}")
                    await conn.execute("DELETE FROM binding_principal_scopes "
                        "WHERE channel_id = $1 AND chat_id = $2 AND principal_id = $3", *address, ids[0])
                    await conn.executemany("INSERT INTO binding_principal_scopes "
                        "(channel_id, chat_id, sender_id, principal_id, principal_kind, scope_id) "
                        "VALUES ($1, $2, $3, $4, $5, $6)",
                        [(*address, sender, ids[0], arguments["principal_kind"], arguments["scope_id"]) for sender in ids])
                    result = next(row for row in await _overrides(conn, arguments) if row["principal_id"] == ids[0])
                else:
                    removed = await conn.fetch("DELETE FROM binding_principal_scopes WHERE channel_id = $1 "
                        "AND chat_id = $2 AND principal_id = $3 RETURNING sender_id", *address, arguments["principal_id"])
                    result = {"deleted": bool(removed)}
        await reconcile(server, binding["manager_id"], arguments)
        return result
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
