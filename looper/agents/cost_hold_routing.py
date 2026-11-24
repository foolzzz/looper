"""Only exact operator replies decide spend holds; no model sees a hold command."""

from __future__ import annotations

import re

from ..db import cost_holds as store
from . import cost_holds, gate_routing, operator_identity
from .intent_routing import RouteResult
from .message import is_lark

_CONTINUE = re.compile(r"继续(?: C-([1-9][0-9]{0,18})(?: ([0-9]+(?:\.[0-9]+)?))?)?")
_STOP = re.compile(r"停(?: C-([1-9][0-9]{0,18}))?")


async def route(envelope, *, platform):
    if not cost_holds.needs_service():
        return None
    text = " ".join(str(envelope.payload.get('text') or '').split())
    continuing = _CONTINUE.fullmatch(text)
    match = continuing or _STOP.fullmatch(text)
    if match is None:
        return None
    channel = envelope.source.channel or ''
    context = envelope.payload.get('channel_context') or {}
    group = gate_routing._group(channel, context) if is_lark(channel) or channel == 'telegram' else False
    sender = context.get('sender_id') or context.get('user_id')
    client = context.get('client') or {}
    operator = await operator_identity.operator_sender_id(envelope.source.agent_id) if channel != 'web' else None
    if not operator_identity.is_operator(channel=channel, group=group, sender=str(sender) if sender else None,
            client_id=client.get('id'), operator_id=operator):
        return None
    explicit = match.group(1)
    if explicit:
        number = int(explicit)
        if number > 9223372036854775807:
            return RouteResult('没有这条花费等待。')
        # A bare 「继续 C-<id>」 decides only an unanswered hold. Budget holds keep today's fall-through.
        if continuing and continuing.group(2) is None:
            row = await store.get(number)
            if row is None or row.get('state') != 'pending' or row.get('reason') != 'unanswered':
                return None
    else:
        pending = await store.list_pending()
        if not pending:
            return None
        if len(pending) != 1:
            return RouteResult('有多条花费等待，请回复「继续 C-<id> <新上限美元>」或「停 C-<id>」。')
        number = pending[0]['id']
    amount = float(continuing.group(2)) if continuing and continuing.group(2) else None
    via = 'dashboard' if channel == 'web' else 'lark' if is_lark(channel) else 'telegram'

    async def execute():
        try:
            await cost_holds.decide(number, 'continue' if continuing else 'stop', amount, via=via, platform=platform)
        except ValueError as exc:
            return str(exc)
        return f'C-{number} 已继续。' if continuing else f'C-{number} 已停下。'

    return RouteResult(action=execute)


async def before_intent(manager, envelope):
    """Keep both exact-reply routers inside the Manager's existing cancellable preparation step."""
    if cost_holds.needs_service() and manager.ops is not None:
        result = await route(envelope, platform=manager.ops.platform)
        if result is not None:
            return result
    return await manager._route_intent(envelope)
