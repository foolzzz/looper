"""Confirm a relayed ledger decision in the operator's own chat, using the shared card record and callback."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from ..db import card_actions as db_cards, connection, ledger
from . import card_actions, operator_identity
from .message import is_lark, parse_channel_id
from .ops import Call

logger = logging.getLogger(__name__)
CARD_TTL = timedelta(days=7)


def _waiting(item: Mapping[str, Any]) -> bool:
    return (item["kind"] == "decision" and item["source"] is None and item["status"] == "open"
            and item["assurance"] == "relayed")


async def offer(call: Call, items: Sequence[Mapping[str, Any]]) -> None:
    """Append a confirmation card for each relayed decision read in the operator's direct chat; off is a no-op."""
    turn = call.turn
    if (turn.group or turn.client_id is not None or not turn.sender or not turn.chat
            or not card_actions.has_buttons(turn.channel_id) or not any(_waiting(item) for item in items)):
        return
    try:
        if (not await card_actions.enabled() or call.manager_id != "manager"
                or turn.sender != await operator_identity.operator_sender_id(turn.channel_id)):
            return
        pool = connection.get_pool()
        for item in items:
            if not _waiting(item):
                continue
            async with pool.acquire() as conn, conn.transaction():
                # Serialize the card's content with a confirmation or amendment of this same decision.
                row = await conn.fetchrow("SELECT * FROM ledger_items WHERE id = $1 FOR UPDATE", item["number"])
                if row is None or row["status"] != "open" or row["confirmed_event_id"] is not None:
                    continue
                token = card_actions.new_token()
                text = f"已记入账本 {item['id']}（转述，待本人核实）：{row['title']}"
                card = card_actions.Card(text, (card_actions.Button("确认", card_actions.value(token, "confirm"), "primary"),))
                await db_cards.create(token=token, kind="ledger", subject=item["id"],
                    digest=ledger.confirmation_digest(row), payload={"card": card.to_dict()}, manager_id=call.manager_id,
                    channel_id=turn.channel_id, chat=turn.chat, allowed=[turn.sender], ttl=CARD_TTL, pool=conn)
            turn.blocks.append(text)
            turn.cards.append(card.to_dict())
    except Exception:
        logger.warning("could not attach a ledger confirmation card; it is confirmed by typing", exc_info=True)


async def authorize(row: dict[str, Any], event: card_actions.Press, choice: str) -> card_actions.Answer | None:
    """Recheck the operator's current identity within the shared 1.5-second authorization deadline."""
    if not await ledger.enabled():
        return card_actions.Answer("账本已关闭", "warning")
    if event.presser != await operator_identity.operator_sender_id(event.channel_id):
        return card_actions.Answer("只有你本人能确认账本", "error")
    return None


async def press(row: dict[str, Any], event: card_actions.Press, choice: str) -> card_actions.Answer:
    card = card_actions.Card.from_dict(row["payload"]["card"])
    parsed = parse_channel_id(event.channel_id)
    channel = parsed.channel_type if parsed is not None else ""
    via = "lark" if is_lark(channel) else "telegram"
    actor = ledger.Actor("user", via, actor_id=f"{via}:{event.presser}", manager_id=row["manager_id"],
                         channel_id=event.channel_id, chat=event.chat, message_id=event.message_id)
    try:
        item = await ledger.apply("confirm", actor, item_id=ledger.parse_id(row["subject"]), card_token=row["token"])
    except ledger.LedgerRefused as exc:
        await db_cards.claim(row["token"], by=event.presser, via="button", choice=choice, outcome="stale")
        return card_actions.Answer(str(exc), "warning", card=card.settled(str(exc)))
    line = f"{item['id']} 已确认"
    return card_actions.Answer(line, "success", card=card.settled(f"✅ {line}"))


def install() -> None:
    card_actions.install("ledger", press, authorize=authorize)
