"""The operator's persisted task run tree."""

import logging

import asyncpg

from ...db.connection import PoolNotInitializedError
from .. import run_tree
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT
from ..ops import ErrorCode, OpError

logger = logging.getLogger(__name__)

_NULL_NUMBER = {"type": ["number", "null"]}
_NULL_BOOL = {"type": ["boolean", "null"]}
_COST = _object({"subscription_usd": _NULL_NUMBER, "api_usd": _NULL_NUMBER}, closed=True)
RUN_TREE_NODE = _object({
    "id": _INT, "kind": _STR, "parent_record_id": {"type": ["integer", "null"]},
    "spawner": _object({"role": _NULL_STR, "seat": _NULL_STR}, closed=True), "reason": _STR,
    "model": _NULL_STR, "status": _STR, "started_at": _STR, "ended_at": _NULL_STR,
    "own_cost": _COST, "subtree_cost": _COST,
    "result_summary": _NULL_STR, "result_verified": _NULL_BOOL,
    **dict.fromkeys(("failure_class", "failure_cause", "state", "delivery", "cancel_reason"), _NULL_STR),
    "parent_spend_known": _NULL_BOOL,
    "wait_intervals": _array(_object({"started_at": _STR, "ended_at": _NULL_STR}, closed=True)),
}, closed=True)
RUN_TREE = _object({"task_id": _STR, "nodes": _array(RUN_TREE_NODE)}, closed=True)
SCHEMAS = {"RunTreeNode": RUN_TREE_NODE, "RunTree": RUN_TREE}
RESULTS = {}
OPERATIONS = (
    Operation("runs.tree", "POST", "/api/v1/runs/tree", "Read a task's run tree, costs and waits.",
        "Read a task's run tree, costs and waits. Includes every recorded run and task-scoped parent edges; "
        "unpriced costs remain null, split into subscription equivalents and API spend.",
        _object({"task_id": {"type": "string", "minLength": 1}}, closed=True), RUN_TREE,
        risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads only one task's persisted Looper records
)


async def run(server, operation, arguments, principal, headers):
    try:
        return await run_tree.read(arguments["task_id"])
    except (PoolNotInitializedError, asyncpg.PostgresConnectionError, asyncpg.InterfaceError, OSError, TimeoutError):
        raise OpError(ErrorCode.UNAVAILABLE, "run tree database unavailable") from None
    except Exception:
        logger.exception("Failed to read run tree for task %s", arguments["task_id"])
        raise OpError(ErrorCode.INTERNAL, "run tree read failed") from None
