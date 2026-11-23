"""One direct message a day to the operator while skill proposals wait for them.

It names each pending proposal's project, skill, action and reason (clipped) and links the project's Skills tab.
It never carries a skill's description or content, and nothing in the channel confirms: a reply is an ordinary
message. The day is claimed first (``system_settings``), so a restart or a second process does not send twice.
It goes to the chat the user gates use (``gate_service.dashboard_target``) through the sender ``configure`` gave;
with neither, nothing is sent and nothing is claimed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from ..db import skill_proposals as store
from ..db import system_settings
from ..db.connection import get_pool
from . import turn_metrics

logger = logging.getLogger(__name__)

SENT_KEY = "skill_proposal_notice_sent"
WHY_CHARS = 80
LISTED = 10

Sender = Callable[..., Awaitable[tuple[bool, str | None]]]
_sender: Sender | None = None
_dashboard_url = ""


def configure(sender: Sender | None, *, dashboard_url: str = "") -> None:
    """Send notices through ``sender`` (channel id, chat, text); None: send none."""
    global _sender, _dashboard_url
    _sender, _dashboard_url = sender, dashboard_url


def _clip(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= WHY_CHARS else one[: WHY_CHARS - 1] + "…"


def message(rows: list[dict[str, str]], base: str) -> str:
    """The notice: names, motives and the pages to open; no skill text beyond the name."""
    lines = [f"有 {len(rows)} 个 skill 提议等你确认（确认只能在项目页做，这里不能回复确认）："]
    for row in rows[:LISTED]:
        lines.append(f"- {row['project_name']} / {row['name']}（{row['action']}）：{_clip(row['why'])}")
        lines.append(f"  {base}/#projects/{row['project_id']}/skills")
    if len(rows) > LISTED:
        lines.append(f"……还有 {len(rows) - LISTED} 个。")
    return "\n".join(lines)


async def notify_pending(*, now: datetime | None = None) -> bool:
    """Send today's notice if proposals are pending and today's has not gone out; True when one was sent."""
    from .gate_service import dashboard_target

    if _sender is None:
        return False
    async with get_pool().acquire() as conn:
        rows = await store.pending_summary(conn)
    if not rows:
        return False
    chat = await dashboard_target()
    if chat is None:
        return False
    today = (now or datetime.now()).astimezone().date().isoformat()
    if not await system_settings.claim(SENT_KEY, today):
        return False
    # Counted with the advisor's alerts: a new source needs a schema change this note does not justify.
    proactive = turn_metrics.proactive_kwargs(turn_metrics.ProactiveSource.ADVISOR_ALERT, None)
    sent, error = await _sender(
        chat.channel_id, chat.target.id, message(rows, _dashboard_url),
        **({**proactive, "group": chat.target.group} if proactive else {}))
    if not sent:
        logger.warning("cannot send the skill proposal notice to %s: %s", chat.channel_id, error)
    return bool(sent)
