"""Alerts for the direct task Manager session's sandbox boundary (RFC 21, 25.10).

A readback that finds the CLI running wider than Looper launched it, with another MCP set, or that cannot be read at
all is an invariant violation: critical, sent every time to the operator's direct chat (``operator_dm_target``), with
no claim or window. With no target or no sender (``chat_scope_alerts.configure``) the text is only logged. The text
carries the task id, the reason and the item name, never a setting's value.
"""
from __future__ import annotations

import logging

from . import chat_scope_alerts

logger = logging.getLogger(__name__)


async def invariant_violation(task_id: str, reason: str, item: str) -> None:
    """Critical. Sent every time."""
    try:
        await chat_scope_alerts._deliver(
            f"严重：任务 {task_id} 的任务管家直接会话沙箱读回没有通过（{reason}，{item}），已停止该会话。")
    except Exception:
        logger.warning("cannot send a task Manager sandbox invariant alert", exc_info=True)
