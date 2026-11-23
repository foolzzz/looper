"""Buttons on chat cards: a Manager's confirmation and a user gate answered with one press (assistant card actions
RFC, ``docs/architecture/rfcs/assistant-card-actions-v0.zh-CN.md``).

Off until the system setting ``cards.actions.enabled`` is ``true``; off, nothing here runs and no card is sent.

- **A card** (``Card``) is code's text and its buttons. A button carries only ``<token>:<choice>``: the token names
  the card's row (``db.card_actions``), which holds what the buttons act on, a digest of the parameters they are
  bound to, who may press them and until when. The choices are the buttons' own keys (``confirm``, ``cancel``, an
  option, ``end``), never parameters.
- **A press** (``Press``) comes from the channel that sent the card (Lark's ``card.action.trigger`` on the bot
  token's long connection, a Telegram callback query). ``press`` answers it in code: someone not allowed is told
  ``NOT_ALLOWED``; a card already handled, expired or bound to parameters that changed since is told it was handled
  (``HANDLED``, ``STALE``); else the row is claimed once (``db.card_actions.claim``) and its kind's handler acts.
  ``respond`` gives the channel an answer within its time (a toast and the card as it is now) and updates the card
  again when the action ends later.
- **Kinds.** ``gate``: an option of the gate's open questions, or ending the discussion, goes into the gate as the
  user's message (``gate_service.post_message``), like ``/gate N``. ``confirmation``: the Manager's waiting
  high-risk call (``ops.service.ManagerOps.press``), which the confirmation typed in the chat resolves too.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ..db import card_actions as db_cards
from ..db import system_settings
from ..db import user_gates as db_user_gates
from .message import manager_address, parse_channel_id
from .user_gates import GateState, MessageKind

logger = logging.getLogger(__name__)

ENABLED_KEY = "cards.actions.enabled"
# The channel types whose adapter sends buttons and receives their presses; any other shows the card's text.
BUTTON_CHANNELS = frozenset({"lark_bot", "telegram"})
# How long a channel waits for a press's answer (Lark gives 3 s); past it the channel is told it was received and
# the card is updated when the action ends.
ANSWER_SECONDS = 2.5
# What the checks a press needs before it may act (the switch, its card, where it came from, who pressed) may take,
# from when the channel got it; past it the press is refused and nothing is claimed, however the checks end.
AUTHORIZE_SECONDS = 1.5
GATE_CARD_TTL = timedelta(days=7)
# At most this many open questions, and options each, get buttons; the rest are answered by replying.
GATE_ITEMS, GATE_OPTIONS = 3, 5
OPTION_LABEL_CHARS = 30

HANDLED = "已处理"
STALE = "已失效，请看最新的消息"
NOT_ALLOWED = {"confirmation": "只有发起人能确认", "gate": "只有发起人能回答"}
ELSEWHERE = "这张卡片只能在发出它的会话里操作"
TURNED_OFF = "按钮已关闭，请打字回复"
RECEIVED = "已收到，正在处理"
UNCHECKED = "没能及时核对这次点击，没有处理，请再点一次"


@dataclass(frozen=True)
class Button:
    label: str
    value: str  # "<token>:<choice>", all a button carries
    style: str = "default"  # primary | default | danger


@dataclass(frozen=True)
class Card:
    """Code's text and its buttons; ``mention`` is the open_id a Lark group card @-mentions."""

    text: str
    buttons: tuple[Button, ...] = ()
    mention: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "buttons": [{"label": b.label, "value": b.value, "style": b.style}
                                               for b in self.buttons], "mention": self.mention}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Card:
        return cls(str(data.get("text") or ""), tuple(Button(str(b["label"]), str(b["value"]), str(b.get("style")
                                                                                                    or "default"))
                                                       for b in data.get("buttons") or ()), data.get("mention"))

    @property
    def token(self) -> str:
        """The token its buttons carry ('' without buttons)."""
        return self.buttons[0].value.partition(":")[0] if self.buttons else ""

    def settled(self, line: str) -> Card:
        """The card once handled: its text, what became of it, no buttons."""
        return Card(f"{self.text}\n\n{line}", (), self.mention)


@dataclass(frozen=True)
class Press:
    """Someone pressed a button: the channel (bot) it came through, who, the button's value, and the chat and the
    channel message the button was in."""

    channel_id: str
    presser: str
    value: str
    chat: str = ""
    message_id: str = ""
    # Whether ``chat`` is a direct chat: asked only for a card sent to a user whose message was not noted.
    direct: Callable[[], Awaitable[bool]] | None = field(default=None, compare=False, repr=False)


@dataclass
class Answer:
    """What the presser is told at once (a toast, ``level`` info/success/warning/error) and the card as it is now;
    ``later`` is the card once the action that goes on ends."""

    toast: str
    level: str = "info"
    card: Card | None = None
    later: Awaitable[Card] | None = field(default=None, repr=False)


Handler = Callable[[dict[str, Any], Press, str], Awaitable[Answer]]
Authorizer = Callable[[dict[str, Any], Press, str], Awaitable[Answer | None]]
_handlers: dict[str, Handler] = {}
_authorizers: dict[str, Authorizer] = {}


def install(kind: str, handler: Handler, *, authorize: Authorizer | None = None) -> None:
    """Who acts on a press of a ``kind`` card (``serve`` installs them through the Manager fleet)."""
    _handlers[kind] = handler
    if authorize is None:
        _authorizers.pop(kind, None)
    else:
        _authorizers[kind] = authorize


def value(token: str, choice: str) -> str:
    return f"{token}:{choice}"


def new_token() -> str:
    return secrets.token_urlsafe(16)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def enabled(db: Any = None) -> bool:
    """Whether card buttons are on (``cards.actions.enabled`` is ``true``; no row is off)."""
    return (await system_settings.get_settings([ENABLED_KEY], pool=db)).get(ENABLED_KEY) == "true"


async def pending_confirmations(managers: Any = None) -> list[dict[str, Any]] | None:
    """Pending Manager operations for readers such as Needs-you. With cards on, their durable rows are the
    source; memory supplements only confirmations that could not be persisted. With cards off, read memory as
    before. None means neither a durable source nor a running fleet is available."""
    durable = await enabled()
    if not durable and managers is None:
        return None
    waiting = []
    if durable:
        for row in await db_cards.open_confirmations():
            payload = row["payload"]
            turn = payload.get("turn") or {}
            waiting.append({"manager_id": row["manager_id"] or "manager", "confirmation_id": row["subject"],
                            "tool": payload["tool"], "impact": payload.get("impact", ""),
                            "channel": turn.get("channel"), "channel_id": row["channel_id"], "chat": row["chat"],
                            "waited_seconds": int(row["age"]), "expires_in_seconds": max(0, int(row["remaining"]))})
    if managers is not None:
        for agent in managers.agents():
            ops = managers.ops(agent.agent_id)
            if ops is not None:
                rows = (ops.waiting_confirmations(unpersisted_only=True) if durable else ops.waiting_confirmations())
                waiting.extend({**row, "manager_id": agent.agent_id} for row in rows)
    return waiting


def has_buttons(channel_id: str | None) -> bool:
    parsed = parse_channel_id(channel_id)
    return parsed is not None and parsed.channel_type in BUTTON_CHANNELS


async def press(event: Press, *, check_until: float | None = None) -> Answer:
    """The answer to a press, written by code (module docstring). Its checks end by ``check_until`` (the event
    loop's clock; by default ``AUTHORIZE_SECONDS`` from now) or the press is refused (``UNCHECKED``), the checks
    cancelled and nothing claimed; only a press that passed them goes on to its handler."""
    loop = asyncio.get_running_loop()
    check_until = loop.time() + AUTHORIZE_SECONDS if check_until is None else check_until
    try:
        checked = await asyncio.wait_for(_authorize(event), max(0.0, check_until - loop.time()))
    except TimeoutError:
        logger.warning("%s: the checks of a card press took too long; it was refused", event.channel_id)
        return Answer(UNCHECKED, "error")
    except Exception:
        logger.exception("%s: the checks of a card press failed; it was refused", event.channel_id)
        return Answer(UNCHECKED, "error")
    if isinstance(checked, Answer):
        return checked
    handler, row, choice = checked
    return await handler(row, event, choice)


async def _authorize(event: Press) -> Answer | tuple[Handler, dict[str, Any], str]:
    """The checks of a press: its answer when it may not act, else its handler, card row and choice."""
    token, _, choice = event.value.partition(":")
    if not token or not choice:
        return Answer(HANDLED)
    if not await enabled():
        return Answer(TURNED_OFF, "warning")
    row = await db_cards.get(token)
    if row is None or row["channel_id"] != event.channel_id:
        return Answer(HANDLED)
    card = Card.from_dict(row["payload"].get("card") or {})
    if choice not in {button.value.partition(":")[2] for button in card.buttons}:
        return Answer(HANDLED)
    if not await _where_sent(row, event):
        return Answer(ELSEWHERE, "error")
    if row["allowed"] and event.presser not in row["allowed"]:
        return Answer(NOT_ALLOWED.get(row["kind"], NOT_ALLOWED["confirmation"]), "error")
    if row["handled_at"] is not None or row["expired"]:
        return Answer(HANDLED, card=card.settled(HANDLED))
    handler = _handlers.get(row["kind"])
    if handler is None:
        logger.warning("no handler for a %s card's press", row["kind"])
        return Answer(HANDLED)
    if authorize := _authorizers.get(row["kind"]):
        refused = await authorize(row, event, choice)
        if refused is not None:
            return refused
    return handler, row, choice


async def _where_sent(row: Mapping[str, Any], event: Press) -> bool:
    """Whether the press came from the card as it was sent: from the chat it is in, and from the channel message it
    became once that is noted (``bind``), so a forwarded copy does nothing; a message id is unique only within its
    chat on Telegram, so both are compared. A Lark card sent to a user (their open_id) whose chat was not noted (the
    service stopped right after sending it) is pressed from a direct chat by that user: that chat is theirs."""
    if row.get("message_id") and event.message_id != row["message_id"]:
        return False
    if event.chat and event.chat == row.get("chat"):
        return True
    return bool(event.presser) and row.get("chat") == event.presser and event.direct is not None \
        and await event.direct()


async def bind(card: Card, message_id: str | None, chat: str | None = None) -> None:
    """Note the channel message ``card`` was sent as and the chat it is in (a Lark user's direct chat, for a card
    sent to their open_id); the channels call it after sending. A failure only logs: the card is then matched by
    the chat it was sent to."""
    if not card.token or not message_id:
        return
    try:
        await db_cards.bind(card.token, message_id, chat)
    except Exception:
        logger.warning("could not note the message of a card", exc_info=True)


async def respond(event: Press, update: Callable[[Card], Awaitable[None]], *,
                  seconds: float | None = None, started: float | None = None) -> Answer:
    """``press`` answered within ``seconds``, with the card as it is when the action ends in that time too. A press
    that takes longer is told ``RECEIVED`` and ``update`` gets its card when it is answered; an action that goes on
    longer comes back as the answer's ``later``, which the channel ``follow``s once it showed the answer."""
    seconds = ANSWER_SECONDS if seconds is None else seconds
    loop = asyncio.get_running_loop()
    started = loop.time() if started is None else started  # when the channel got the press
    deadline = started + seconds
    # Checked within AUTHORIZE_SECONDS of ``started``, before the answer is due: what goes on past the answer is
    # only the action of a press that passed its checks.
    task = asyncio.create_task(press(event, check_until=started + AUTHORIZE_SECONDS))
    done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - loop.time()))
    if not done:
        _keep(_finish_late(task, update))
        return Answer(RECEIVED)
    try:
        answer = task.result()
    except Exception:
        logger.exception("a card press failed")
        return Answer("出错了，没有处理", "error")
    if answer.later is None:
        return answer
    later = asyncio.ensure_future(answer.later)
    done, _ = await asyncio.wait({later}, timeout=max(0.0, deadline - loop.time()))
    if not done:
        return Answer(answer.toast, answer.level, card=answer.card, later=later)
    try:
        return Answer(answer.toast, answer.level, card=later.result())
    except Exception:
        logger.exception("a card's action failed")
        return Answer(answer.toast, answer.level, card=answer.card)


def follow(answer: Answer, update: Callable[[Card], Awaitable[None]]) -> None:
    """Update the card once the action that goes on after ``answer`` ends (``respond``)."""
    if answer.later is not None:
        _keep(_update_later(answer.later, update))


_kept: set[asyncio.Task[Any]] = set()


def _keep(work: Awaitable[Any]) -> None:
    task = asyncio.ensure_future(work)
    _kept.add(task)
    task.add_done_callback(_kept.discard)


async def _update_later(later: Awaitable[Card], update: Callable[[Card], Awaitable[None]]) -> None:
    try:
        await update(await later)
    except Exception:
        logger.warning("could not update a card after its action ended", exc_info=True)


async def _finish_late(task: asyncio.Task[Answer], update: Callable[[Card], Awaitable[None]]) -> None:
    try:
        answer = await task
    except Exception:
        logger.exception("a card press failed")
        return
    if answer.card is not None:
        await _update_later(_ready(answer.card), update)
    if answer.later is not None:
        await _update_later(answer.later, update)


async def _ready(card: Card) -> Card:
    return card


# --- Gates ---


def _gate_digest(gate: Mapping[str, Any]) -> str:
    """What a gate card's buttons are bound to: the topic and the open questions with their options."""
    return digest([gate.get("topic"), [[item.get("id"), item.get("question"), item.get("options")]
                                       for item in gate.get("agenda") or [] if item.get("status") in (None, "open")]])


def _clip(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else f"{flat[:limit - 1]}…"


async def gate_card(row: Mapping[str, Any], channel_id: str, target_id: str, *, mention: str | None, group: bool,
                    text: str) -> Card | None:
    """The card a gate's message is posted as (``gate_courier``): its text with a button per option of its open
    questions and one that ends the discussion; None when the message gets none (cards off, a channel without
    buttons, a message that does not ask for an answer, a gate that no longer waits)."""
    try:
        kind = MessageKind(row["kind"])
    except ValueError:
        return None
    if (kind not in (MessageKind.OPENED, MessageKind.UPDATED, MessageKind.REMINDER, MessageKind.RESUMED)
            or not GateState(row["state"]).waiting or not has_buttons(channel_id) or not await enabled()):
        return None
    token = new_token()
    answers: dict[str, dict[str, Any]] = {}
    buttons: list[Button] = []
    items = [item for item in row.get("agenda") or [] if item.get("status") in (None, "open") and item.get("options")]
    for number, item in enumerate(items[:GATE_ITEMS], 1):
        for index, option in enumerate((item.get("options") or [])[:GATE_OPTIONS], 1):
            choice = f"{number}.{index}"
            label = _clip(option, OPTION_LABEL_CHARS)
            buttons.append(Button(f"{number}. {label}" if len(items) > 1 else label, value(token, choice)))
            answers[choice] = {"agenda_item": item.get("id"), "option": option,
                               "text": f"{_clip(item.get('question'), 200)}：{option}"}
    buttons.append(Button("按现在的结论结束", value(token, "end"), "default"))
    if len(buttons) > 1:
        buttons[0] = Button(buttons[0].label, buttons[0].value, "primary")
    card = Card(text, tuple(buttons), mention if group else None)
    # Only the requester may answer: a direct chat's user, or the group's requester; a group without one (Telegram)
    # admits the senders the bot admits there. A Telegram group's id is negative, a person's chat is their user id.
    group = group or target_id.startswith("-")
    allowed = [mention] if mention else ([] if group else [target_id])
    await db_cards.create(token=token, kind="gate", subject=row["step_id"], digest=_gate_digest(row),
                          payload={"task_id": row["task_id"], "label": row["step_key"], "answers": answers,
                                   "group": group, "card": card.to_dict()},
                          manager_id=None, channel_id=channel_id, chat=target_id, allowed=allowed, ttl=GATE_CARD_TTL)
    return card


async def press_gate(row: dict[str, Any], event: Press, choice: str, *, bus: Any,
                     has_planner: Callable[[str], bool]) -> Answer:
    """An option, or ending the discussion, into the gate as the presser's message (``gate_commands`` does the same
    for ``/gate N``)."""
    from .gate_service import HTTP_400, post_message, receipt, sender_is_operator, task_name
    from .projects import RequestError

    payload, token = row["payload"], row["token"]
    card = Card.from_dict(payload["card"])
    task_id, label = payload["task_id"], payload["label"]
    gate = await db_user_gates.get(task_id, label)
    if gate is None or not GateState(gate["state"]).waiting:
        await db_cards.claim(token, by=event.presser, via="button", choice=choice, outcome="stale")
        return Answer("这个关卡已经不在等你了", "warning", card=card.settled("这个关卡已经不在等你了。"))
    if _gate_digest(gate) != row["digest"]:
        await db_cards.claim(token, by=event.presser, via="button", choice=choice, outcome="stale")
        return Answer(STALE, "warning", card=card.settled("问题已经更新，这张卡片已失效。"))
    end = choice == "end"
    data = {"text": ""} if end else payload["answers"].get(choice)
    if data is None:
        return Answer(HANDLED)
    if not await db_cards.claim(token, by=event.presser, via="button", choice=choice, outcome="answered"):
        return Answer(HANDLED, card=card.settled(HANDLED))
    parsed = parse_channel_id(row["channel_id"])
    channel = parsed.channel_type if parsed is not None else ""
    group = bool(payload.get("group"))
    origin = {"author": event.presser if group else None, "channel": channel, "channel_id": row["channel_id"],
              "chat": row["chat"], "external_id": None}
    try:
        posted = await post_message(task_id, label, data, has_planner=has_planner(task_id), bus=bus,
                                    source=manager_address(row.get("manager_id") or "manager"),
                                    via=f"button:{channel}", end=end, origin=origin,
                                    from_operator=await sender_is_operator(row["channel_id"], channel, group=group,
                                                                           sender=event.presser))
    except RequestError as exc:
        await db_cards.finish(token, "error")
        if exc.status == HTTP_400:
            return Answer(f"没能交给规划者：{exc}", "error", card=card.settled(f"❌ 没能交给规划者：{exc}"))
        return Answer("这个关卡已经不在等你了", "warning", card=card.settled("这个关卡已经不在等你了。"))
    line = receipt(await task_name(task_id), label, end=end, kept=posted["delivered"] == "kept")
    chosen = "" if end else f"你选了：{data['option']}。"
    return Answer(line, "success", card=card.settled(f"✅ {chosen}{line}"))


# --- How the channels show a card ---


def lark_card(card: Card) -> dict[str, Any]:
    """The card as Lark's card JSON 2.0: its text as Markdown, then its buttons in a row."""
    text = f"<at id={card.mention}></at> {card.text}" if card.mention else card.text
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": text}]
    if card.buttons:
        styles = {"primary": "primary_filled", "danger": "danger", "default": "default"}
        elements.append({"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "small", "columns": [
            {"tag": "column", "width": "auto", "elements": [{
                "tag": "button", "text": {"tag": "plain_text", "content": button.label},
                "type": styles.get(button.style, "default"),
                "behaviors": [{"type": "callback", "value": {"looper": button.value}}]}]}
            for button in card.buttons]})
    return {"schema": "2.0", "config": {"update_multi": True}, "body": {"elements": elements}}


def lark_answer(answer: Answer) -> dict[str, Any]:
    """The answer as a Lark card callback's response: a toast, and the card as it is now."""
    response: dict[str, Any] = {"toast": {"type": answer.level, "content": answer.toast}}
    if answer.card is not None:
        response["card"] = {"type": "raw", "data": lark_card(answer.card)}
    return response


def telegram_markup(card: Card) -> Any:
    """The card's buttons as a Telegram inline keyboard, three to a row; None without buttons."""
    if not card.buttons:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keys = [InlineKeyboardButton(button.label, callback_data=button.value) for button in card.buttons]
    return InlineKeyboardMarkup([keys[i:i + 3] for i in range(0, len(keys), 3)])


def shown(text: str, cards: Sequence[Mapping[str, Any]] | None) -> tuple[str, list[Card]]:
    """A reply's text without the cards it carries, and those cards: a card goes as a message of its own only when
    its text is in the reply as it is (a reply a group check replaced carries none)."""
    found: list[Card] = []
    for raw in cards or ():
        card = Card.from_dict(raw)
        if card.text and card.text in text:
            joined = "\n\n" + card.text  # as ``tiers.manager._with_blocks`` adds a block
            text = text.replace(joined if joined in text else card.text, "", 1)
            found.append(card)
    return (text.strip() if found else text), found
