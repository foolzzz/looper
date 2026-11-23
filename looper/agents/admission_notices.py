"""One operator alert per durable account-freeze event."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
import logging

from ..db import profile_registry, provider_limits, system_settings

logger = logging.getLogger(__name__)
Sender = Callable[..., Awaitable[tuple[bool, str | None]]]
_sender: Sender | None = None


def configure(sender: Sender | None) -> None:
    global _sender
    _sender = sender


async def freeze_alert(
    provider_id: str,
    event_id: int,
    *,
    until: datetime | None,
    until_known: bool,
    waiting: int,
) -> None:
    """Deduplicate successful sends; release unsuccessful claims for retry."""
    from .gate_service import dashboard_target

    key = f"admission_alert_sent:{event_id}"
    claimed = sent = False
    try:
        sender = _sender
        chat = await dashboard_target()
        if sender is None or chat is None:
            return
        claimed = await system_settings.claim(key, "sent")
        if not claimed:
            return
        account = await profile_registry.get_provider(provider_id)
        windows = await provider_limits.windows(provider_id)
        names = (
            ", ".join(w["window_name"] for w in windows if w["status"] == "rejected")
            or "unknown window"
        )
        reset = (
            (until - timedelta(seconds=60)).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            if until_known and until
            else "重置时间未知"
        )
        text = f"账号 {account['name'] if account else provider_id} 的额度已用完（{names}），{reset}；{waiting} 个运行等待继续。"
        logger.warning("admission freeze: %s", text)
        sent, error = await sender(
            chat.channel_id,
            chat.target.id,
            text,
            group=chat.target.group,
            idempotency_key=f"admission-freeze:{event_id}",
        )
        if not sent:
            logger.error("cannot send admission freeze alert: %s", error)
    except Exception:
        logger.error("cannot deliver admission freeze alert", exc_info=True)
    finally:
        if claimed and not sent:
            try:
                await system_settings.put_settings({key: None})
            except Exception:
                logger.error("cannot release admission freeze alert claim", exc_info=True)
