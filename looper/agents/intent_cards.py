"""Intent push buttons, sharing the typed commands' business transaction and card action record."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from typing import Any

from ..db import card_actions as store, connection, intents as db_intents
from . import card_actions as cards, leads_model, operator_identity
from .message import AgentAddress, MessageEnvelope, MessageType, manager_address, parse_channel_id


class Choice(StrEnum):
    BUILD = "build"
    REJECT = "reject"


class StaleCard(RuntimeError):
    def __init__(self) -> None:
        super().__init__(cards.STALE)


async def _digest(row: Mapping[str, Any], conn: Any) -> str:
    from . import intents

    view = await intents.intent_view(row, db=conn)
    project = await conn.fetchrow("SELECT work_dir, default_workflow, task_workspaces FROM projects WHERE id=$1",
                                  row["project_id"]) if row["project_id"] else None
    sources = (await db_intents.source_rows([row["id"]], db=conn)).get(row["id"], [])[:5]
    return cards.digest([[row.get(key) for key in ("title", "summary", "type_key", "type_version", "project_id",
                                                 "workflow_key", "flagged")],
                         [view[key] for key in ("type", "project", "effective_workflow", "candidates")],
                         dict(project) if project else None, sources])


async def make(row: Mapping[str, Any], channel_id: str, target: str, text: str, *, conn: Any) -> cards.Card | None:
    """The unchanged push text with buttons; off or without a button adapter, no writes and no card."""
    if not cards.has_buttons(channel_id) or not await cards.enabled(conn):
        return None
    token = cards.new_token()
    card = cards.Card(text, (cards.Button("建", cards.value(token, Choice.BUILD), "primary"),
                             cards.Button("驳回", cards.value(token, Choice.REJECT), "danger")))
    await store.create(token=token, kind="intent", subject=str(row["id"]), digest=await _digest(row, conn),
                       payload={"card": card.to_dict(), "target_chat": target, "schedule_id": row["schedule_id"]},
                       manager_id="manager", channel_id=channel_id, chat=target, allowed=[target],
                       ttl=timedelta(hours=24), pool=conn)
    return card


async def final_card(intent_id: int, channel_id: str, target: str, message_id: str, text: str, *,
                     conn: Any) -> cards.Card | None:
    """The terminal update stays a card only when this message was originally sent as one."""
    if not await cards.enabled(conn):
        return None
    posted = await conn.fetchval("SELECT true FROM card_actions WHERE kind='intent' AND subject=$1 AND message_id=$2 "
                                  "AND channel_id=$3 AND payload->>'target_chat'=$4",
                                  str(intent_id), message_id, channel_id, target)
    return cards.Card(text) if posted else None


async def authorize(row: dict[str, Any], event: cards.Press, choice: str) -> cards.Answer | None:
    """Recheck current operator and subscription target within the shared authorization deadline."""
    from . import intent_push

    if not await leads_model.intents_enabled():
        return cards.Answer("意图识别已关闭。", "warning")
    if event.presser != await operator_identity.operator_sender_id(event.channel_id):
        return cards.Answer(cards.NOT_ALLOWED["confirmation"], "error")
    target = await intent_push.target_for(row["payload"]["schedule_id"])
    if target != (event.channel_id, row["payload"]["target_chat"]):
        return cards.Answer(cards.STALE, "warning")
    return None


async def claim(conn: Any, intent: Mapping[str, Any], event: cards.Press) -> None:
    """Claim after locking the business row, in its transaction; failed builds roll the claim back too."""
    token, _, choice = event.value.partition(":")
    row = await store.get(token, pool=conn)
    if (row is None or row["kind"] != "intent" or row["subject"] != str(intent["id"])
            or intent["state"] != "pending" or row["digest"] != await _digest(intent, conn)):
        raise StaleCard()
    if not await store.claim(token, by=event.presser, via="button", choice=choice, outcome="running", pool=conn):
        raise db_intents.IntentRefused(cards.HANDLED)


async def resolve(conn: Any, intent_id: int, choice: Choice, event: cards.Press | None) -> None:
    if event is None and not await cards.enabled(conn):
        return
    await store.resolve_subject("intent", str(intent_id), by=event.presser if event else None,
                                via="button" if event else "typed", choice=choice, outcome="ok", pool=conn)
    if event is not None:
        await store.finish(event.value.partition(":")[0], "ok", pool=conn)


async def press(row: dict[str, Any], event: cards.Press, choice: str, *, platform: Any) -> cards.Answer:
    from . import intent_routing

    card = cards.Card.from_dict(row["payload"]["card"])
    iid = int(row["subject"])
    current = await connection.get_pool().fetchrow("SELECT * FROM intents WHERE id=$1", iid)
    if current is None or current["state"] != "pending":
        await store.claim(row["token"], by=event.presser, via="button", choice=choice, outcome="stale")
        return cards.Answer(cards.HANDLED, card=card.settled(cards.HANDLED))
    channel = parse_channel_id(event.channel_id).channel_type
    envelope = MessageEnvelope.create(source=AgentAddress("channel", event.channel_id, channel),
        target=manager_address("manager"), message_type=MessageType.USER_MESSAGE,
        payload={"text": "", "channel_context": {"chat_id": event.chat, "sender_id": event.presser,
                                                   "message_id": event.message_id, "chat_type": "p2p"}})
    try:
        text = await intent_routing._execute(envelope, platform=platform, number=iid,
            label=leads_model.format_intent_id(iid), build=choice == Choice.BUILD, argument=None,
            channel=channel, context=envelope.payload["channel_context"], sender=event.presser, group=False,
            card_press=event)
    except StaleCard:
        await store.claim(row["token"], by=event.presser, via="button", choice=choice, outcome="stale")
        return cards.Answer(cards.STALE, "warning", card=card.settled(cards.STALE))
    saved = await store.get(row["token"])
    if saved and saved["handled_at"] is not None:
        return cards.Answer(text, "success", card=card.settled(text))
    return cards.Answer(text, "warning", card=card)


def install(platform: Any) -> None:
    async def handle(row: dict[str, Any], event: cards.Press, choice: str) -> cards.Answer:
        return await press(row, event, choice, platform=platform)

    cards.install("intent", handle, authorize=authorize)
