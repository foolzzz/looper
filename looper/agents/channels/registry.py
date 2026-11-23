"""Channel registry: manages adapter lifecycle.

Outbound messages are addressed to a channel id (``channel-<type>`` or ``channel-<type>-<n>``) and each
adapter delivers what arrives on its own bus queue; the registry does not route
them a second time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import turn_metrics
from ..turn_metrics import ProactiveRef
from ..bus import MessageBus
from ..message import parse_channel_id
from ..task_notice import NotifyTarget
from ..ops.leads import for_group
from ..log_redaction import redact
from .base import ChannelAdapter, PostOutcome, PostReceipt
from .chat_routing import RouteDecision, RoutingSnapshot, load_snapshot
from .lark_quote import QuoteRead, QuoteReader, QuoteRef, QuoteUnavailable
from .startup import ChannelStartState, transient

logger = logging.getLogger(__name__)


class ChannelRegistry:
    """Manages channel adapter lifecycle."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._channels: dict[str, ChannelAdapter] = {}
        self._routing: dict[str, RoutingSnapshot] = {}
        self._mutation_locks: dict[str, asyncio.Lock] = {}
        self._starts: dict[str, ChannelStartState] = {}
        self._retries: dict[str, asyncio.Task[None]] = {}
        self._start_lock = asyncio.Lock()
        self._stopping = False
        self.down_checks: list[dict[str, str]] = []
        self._deliveries: dict[str, list[tuple[datetime, bool]]] = {}

    _retry_wait = staticmethod(asyncio.sleep)
    _delivery_clock = staticmethod(lambda: datetime.now(timezone.utc))

    def record_delivery(self, channel_id: str, success: bool) -> None:
        """Observe an actual send result without changing the send's return value or logging."""
        self._deliveries.setdefault(channel_id, []).append((self._delivery_clock(), success))

    def delivery_events(self, channel_id: str) -> list[tuple[datetime, bool]]:
        return list(self._deliveries.get(channel_id, ()))

    def acknowledge_deliveries(self, channel_id: str, count: int) -> None:
        del self._deliveries.setdefault(channel_id, [])[:count]

    def start_state(self, channel_id: str) -> ChannelStartState | None:
        state = self._starts.get(channel_id)
        return dict(state) if state is not None else None

    def start_checks(self) -> list[dict[str, str]]:
        return [{"id": f"channel:{channel_id}", "label": f"Channel {channel_id}",
                 "status": "fail" if state["state"] == "failed" else "warn",
                 "detail": f"{state['state']}: {state['last_error'] or ''}; next attempt: {state['next_attempt_at']}",
                 "fix": "Check Settings → Channels and the channel's credentials or transport."}
                for channel_id, state in self._starts.items() if state["state"] != "running"] + self.down_checks \
            + self.catchup_checks()

    def catchup_checks(self) -> list[dict[str, str]]:
        checks = []
        for channel in self.list_channels():
            for window in channel.get("catchup", {}).get("windows", []):
                state, chat, start = window["state"], window["chat_id"], window["start"]
                times = [datetime.fromtimestamp(window[key], timezone.utc).isoformat() for key in ("start", "end")]
                checks.append({
                    "id": f"lark_catchup:{channel['channel_id']}:{chat}:{start}:{state}",
                    "label": f"飞书消息补收{'失败' if state == 'dropped' else '等待重试'}",
                    "status": "fail" if state == "dropped" else "warn",
                    "detail": f"{chat or '聊天发现'}: {times[0]} – {times[1]}; {state}; "
                              f"attempts={window['attempts']}; {window['reason'] or ''}",
                    "fix": "检查聊天并把遗漏的消息转发给 Looper。" if state == "dropped"
                           else "服务会自动重试这个窗口。",
                })
        return checks

    async def start_bot(self, channel_id: str, start: Callable[[bool], Awaitable[bool]]) -> None:
        """Try once on startup; own one retry loop per failed enabled channel until shutdown/disable.

        The callback reloads saved settings on retries and returns False if the row is gone or disabled.
        All attempts share one lock so two recovering rows cannot claim the same bot.
        """
        if self._stopping or channel_id in self._retries:
            return

        async def attempt(retry: bool, delay: float) -> float | None:
            async with self._start_lock:
                self._starts[channel_id] = {"state": "starting", "last_error": None, "next_attempt_at": None}
                try:
                    if not await start(retry):
                        self._starts.pop(channel_id, None)
                        return None
                except Exception as exc:
                    delay = delay if transient(exc) else 600
                    self._starts[channel_id] = {
                        "state": "retrying" if transient(exc) else "failed", "last_error": redact(str(exc)),
                        "next_attempt_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()}
                    logger.warning("%s start failed; retry in %s s: %s", channel_id, delay, exc)
                    return delay
                self._starts[channel_id] = {"state": "running", "last_error": None, "next_attempt_at": None}
                return None

        delay = await attempt(False, 30)
        if delay is None:
            return

        async def retry() -> None:
            nonlocal delay
            try:
                while delay is not None and not self._stopping:
                    await self._retry_wait(delay)
                    delay = await attempt(True, min(delay * 2, 600))
            finally:
                self._retries.pop(channel_id, None)

        self._retries[channel_id] = asyncio.create_task(retry(), name=f"channel-start:{channel_id}")

    async def cancel_start(self, channel_id: str) -> None:
        task = self._retries.pop(channel_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A disable can race a retry that just finished registering an adapter.
            await self.unregister(channel_id)
        self._starts.pop(channel_id, None)

    def mutation_lock(self, channel_id: str) -> asyncio.Lock:
        return self._mutation_locks.setdefault(channel_id, asyncio.Lock())

    def bindings_changed(self, channel_id: str, snapshot: RoutingSnapshot) -> None:
        """Total, synchronous publication: no I/O, callbacks or adapter dependency."""
        self._routing[channel_id] = snapshot

    def resolve_chat(self, channel_id: str, chat_id: str) -> RouteDecision:
        return self._routing[channel_id].resolve(chat_id)

    async def register(self, adapter: ChannelAdapter) -> None:
        if parse_channel_id(adapter.channel_id) is not None:
            async with self.mutation_lock(adapter.channel_id):
                try:
                    snapshot = await load_snapshot(adapter.channel_id)
                except Exception:
                    logger.exception("%s not started: cannot load chat routing", adapter.channel_id)
                    raise
                self.bindings_changed(adapter.channel_id, snapshot)
                adapter._resolve_chat = self.resolve_chat
        self._channels[adapter.channel_id] = adapter
        adapter.delivery_observer = lambda success: self.record_delivery(adapter.channel_id, success)
        await adapter.start(self._bus)
        logger.info("channel %s (%s) registered", adapter.channel_id, adapter.channel_type)

    async def unregister(self, channel_id: str) -> None:
        adapter = self._channels.pop(channel_id, None)
        if adapter:
            await adapter.stop()
            logger.info("channel %s unregistered", channel_id)

    def prepare_stop_all(self) -> None:
        self._stopping = True
        for task in self._retries.values():
            task.cancel()
        for adapter in self._channels.values():
            adapter.prepare_stop()

    async def stop_all(self) -> None:
        self._stopping = True
        for task in self._retries.values():
            task.cancel()
        await asyncio.gather(*self._retries.values(), return_exceptions=True)
        self._retries.clear()
        for channel_id in list(self._channels):
            await self.unregister(channel_id)

    def get(self, channel_id: str) -> ChannelAdapter | None:
        return self._channels.get(channel_id)

    @staticmethod
    def _not_running(channel_id: str) -> str:
        parsed = parse_channel_id(channel_id)
        if parsed is not None and parsed.builtin:
            return f"the {parsed.channel_type} channel is not running: enable it in Settings → Channels"
        return f"the bot {channel_id} is not running: enable it in Settings → Channels"

    async def notify(self, channel_id: str, chat: str, text: str, *,
                     idempotency_key: str | None = None, proactive: ProactiveRef | None = None,
                     group: bool = False) -> tuple[bool, str | None]:
        """Send through the running channel, retaining the event key across caller retries when supplied."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=chat,
                                              group=group, external_id=None,
                                              outcome=PostOutcome.REJECTED.value, dedupe_key=idempotency_key)
            return False, self._not_running(channel_id)
        try:
            receipt = await adapter.notify(chat, text, **({"idempotency_key": idempotency_key}
                                                        if idempotency_key is not None else {}))
        except Exception:
            self.record_delivery(channel_id, False)
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=chat, group=group,
                                              external_id=None, outcome=PostOutcome.UNKNOWN.value, dedupe_key=idempotency_key)
            raise
        self.record_delivery(channel_id, receipt.success)
        if proactive is not None:
            turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=chat,
                                          group=group,
                                          external_id=receipt.external_id, outcome=receipt.result.value, dedupe_key=idempotency_key)
        return receipt.success, receipt.error

    async def post(self, channel_id: str, target: NotifyTarget, text: str, *,
                   reply_to: str | None = None, idempotency_key: str | None = None,
                   proactive: ProactiveRef | None = None) -> PostReceipt:
        """Post ``text`` to ``target`` through the running channel ``channel_id`` (``ChannelAdapter.post``)."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=target.id, group=target.group,
                                              external_id=None, outcome=PostOutcome.REJECTED.value,
                                              dedupe_key=idempotency_key)
            return PostReceipt(False, error=self._not_running(channel_id), outcome=PostOutcome.REJECTED)
        if target.group:
            text = for_group(text)
        try:
            receipt = await adapter.post(target, text, reply_to=reply_to, idempotency_key=idempotency_key)
        except Exception:
            self.record_delivery(channel_id, False)
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=target.id, group=target.group,
                                              external_id=None, outcome=PostOutcome.UNKNOWN.value,
                                              dedupe_key=idempotency_key)
            raise
        self.record_delivery(channel_id, receipt.success)
        if proactive is not None:
            turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=target.id, group=target.group,
                                          external_id=receipt.external_ids[0] if receipt.external_ids else None,
                                          outcome=receipt.result.value, dedupe_key=idempotency_key)
        return receipt

    async def post_card(self, channel_id: str, target: NotifyTarget, card: Any, *,
                        reply_to: str | None = None, idempotency_key: str | None = None,
                        proactive: ProactiveRef | None = None) -> PostReceipt:
        """Post ``card`` (``card_actions.Card``) to ``target`` like ``post`` (``ChannelAdapter.post_card``)."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=target.id, group=target.group,
                                              external_id=None, outcome=PostOutcome.REJECTED.value,
                                              dedupe_key=idempotency_key)
            return PostReceipt(False, error=self._not_running(channel_id), outcome=PostOutcome.REJECTED)
        if target.group and (checked := for_group(card.text)) != card.text:
            card = replace(card, text=checked, buttons=())
        try:
            receipt = await adapter.post_card(target, card, reply_to=reply_to, idempotency_key=idempotency_key)
        except Exception:
            if proactive is not None:
                turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=target.id, group=target.group,
                                              external_id=None, outcome=PostOutcome.UNKNOWN.value,
                                              dedupe_key=idempotency_key)
            raise
        if proactive is not None:
            turn_metrics.record_proactive(proactive, channel_id=channel_id, chat=target.id, group=target.group,
                                          external_id=receipt.external_ids[0] if receipt.external_ids else None,
                                          outcome=receipt.result.value, dedupe_key=idempotency_key)
        return receipt

    async def edit_card(self, channel_id: str, target: NotifyTarget, external_id: str, card: Any) -> PostReceipt:
        """Update a posted card through its adapter, preserving its format and the delivery result."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            return PostReceipt(False, error=self._not_running(channel_id), outcome=PostOutcome.REJECTED)
        if target.group and (checked := for_group(card.text)) != card.text:
            card = replace(card, text=checked, buttons=())
        return await adapter.edit_card(target, external_id, card)

    async def edit(self, channel_id: str, target: NotifyTarget, external_id: str, text: str) -> PostReceipt:
        """Edit one message through its running adapter, preserving the platform's delivery result."""
        adapter = self._channels.get(channel_id)
        if adapter is None:
            return PostReceipt(False, error=self._not_running(channel_id), outcome=PostOutcome.REJECTED)
        return await adapter.edit(target, external_id, text)

    async def read_quote(self, ref: QuoteRef, *, after: int) -> QuoteRead:
        """What a received message quotes, read by the running channel that received it (``QuoteReader``); raises
        ``QuoteUnavailable`` when that channel is not running or reads no quotes."""
        adapter = self._channels.get(ref.channel_id)
        if not isinstance(adapter, QuoteReader):
            raise QuoteUnavailable(f"{ref.channel_id} is not running" if adapter is None
                                   else f"{ref.channel_id} reads no quotes")
        return await adapter.read_quote(ref, after=after)

    def list_channels(self) -> list[dict[str, Any]]:
        return [
            {
                "channel_id": adapter.channel_id,
                "channel_type": adapter.channel_type,
                **adapter.healthcheck(),
            }
            for adapter in self._channels.values()
        ]
