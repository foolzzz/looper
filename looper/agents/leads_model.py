"""The shared vocabulary of message leads and intents (message-leads-intents RFC, section 6): their kinds and states,
how their ids are shown and read, and the system settings that turn them on.

Every part of the feature imports these names; none defines its own copy. A lead's ``expired`` state is never stored:
``shown_state`` derives it, here only.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

LEADS_ENABLED_KEY = "leads.enabled"
LEADS_VALID_DAYS_KEY = "leads.valid_days"
INTENTS_ENABLED_KEY = "intents.enabled"
INTENTS_GROUP_READ_KEY = "intents.group_read.enabled"
INTENTS_RULES_ENABLED_KEY = "intents.rules.enabled"
RULE_INTENT_TYPE = "mr_review"
RULE_CLASSIFICATION_AVAILABLE = False
GROUP_INTENT_TYPES = ("dev_task", "bug_triage")  # what a group request may be (RFC section 11.1)
LEADS_SEARCH_MODE_KEY = "leads.search_mode"
DEFAULT_VALID_DAYS = 90
VALID_DAYS_RANGE = (1, 365)

# What every surface that shows leads says first (the CLI, the Manager tool, the dashboard).
NOTICE = "来自飞书消息，未经确认；只当线索，不当权威。"


class IntentVia(StrEnum):
    SUBSCRIPTION = "subscription"
    FOLLOWED_GROUP = "followed_group"


class OutputKind(StrEnum):
    """What a subscription makes of its window (``subscriptions.output_kind``)."""

    PUSH = "push"
    LEADS = "leads"
    INTENTS = "intents"
    TRIAGE = "triage"


class SearchMode(StrEnum):
    """How a search matches its words (``leads.search_mode``; assistant search RFC, section 4): every word as written
    (``words``, the default), or any word by its Chinese character pairs, its English word starts and, with pg_trgm,
    its likeness, the leads that match more of the words first (``fulltext``)."""

    WORDS = "words"
    FULLTEXT = "fulltext"


class LeadKind(StrEnum):
    DECISION = "decision"
    COMMITMENT = "commitment"
    REQUEST = "request"
    STATUS = "status"
    ISSUE = "issue"
    FACT = "fact"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LeadState(StrEnum):
    """A lead's stored state."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    PROMOTED = "promoted"
    INACCURATE = "inaccurate"
    DROPPED = "dropped"  # the second model's check found its sources do not support it (``leads_check``)


# The states a lead is shown in and searched by: the stored ones and the derived ``expired``. The default search
# (``active``) never shows a dropped lead; ``all`` shows it after the rest.
EXPIRED = "expired"
SHOWN_LEAD_STATES = ("active", EXPIRED, "superseded", "promoted", "inaccurate", "dropped")
SEARCH_STATES = (*SHOWN_LEAD_STATES, "all")


class IntentState(StrEnum):
    PENDING = "pending"
    TASKED = "tasked"
    REJECTED = "rejected"
    EXPIRED = "expired"
    MERGED = "merged"
    DROPPED = "dropped"  # the check found its sources do not support it: handled by the system, never pushed


class IntentPriority(StrEnum):
    URGENT = "urgent"  # pushed at once, marked urgent
    NORMAL = "normal"  # pushed at once
    QUIET = "quiet"  # not pushed: the Intents page only


BUILTIN_INTENT_TYPES = ("dev_task", "bug_triage", "alert_triage")
# The only type an intent whose sources are all alert-bot messages may have.
ALERT_TYPE = "alert_triage"

# Kind labels as the CLI and the pushes write them.
LEAD_KIND_ZH = {"decision": "决定", "commitment": "承诺", "request": "请求", "status": "进展", "issue": "问题",
                "fact": "事实"}
CONFIDENCE_ZH = {"high": "高", "medium": "中", "low": "低"}

_LEAD_ID = re.compile(r"(?:lead-)?(\d{1,18})", re.IGNORECASE)
_INTENT_ID = re.compile(r"(?:I-)?(\d{1,18})", re.IGNORECASE)


def format_lead_id(number: int) -> str:
    return f"lead-{number}"


def parse_lead_id(text: str) -> int:
    """``lead-123`` or ``123``; ``ValueError`` for anything else."""
    found = _LEAD_ID.fullmatch(text.strip())
    if found is None:
        raise ValueError(f"a lead is named lead-<number>, not {text!r}")
    return int(found.group(1))


def format_intent_id(number: int) -> str:
    return f"I-{number:04d}"


def parse_intent_id(text: str) -> int:
    """``I-0042``, ``I-42``, ``i-42`` or ``42``; ``ValueError`` for anything else."""
    found = _INTENT_ID.fullmatch(text.strip())
    if found is None:
        raise ValueError(f"an intent is named I-<number>, not {text!r}")
    return int(found.group(1))


def shown_state(state: str, valid_until: datetime, now: datetime) -> str:
    """The state a lead is shown in: ``expired`` for an active lead past ``valid_until``, else its stored state."""
    return EXPIRED if state == LeadState.ACTIVE and valid_until <= now else state


async def _setting(key: str, db: Any = None) -> str | None:
    from ..db import system_settings

    return (await system_settings.get_settings([key], pool=db)).get(key)


async def leads_enabled(db: Any = None) -> bool:
    """Whether leads are on (``leads.enabled`` is ``true``; no row is off)."""
    return await _setting(LEADS_ENABLED_KEY, db) == "true"


async def intents_enabled(db: Any = None) -> bool:
    """Whether intents are on (``intents.enabled`` is ``true``; no row is off)."""
    return await _setting(INTENTS_ENABLED_KEY, db) == "true"


async def rules_enabled(db: Any = None) -> bool:
    """Read only the opt-in rule switch; absent and malformed values are off."""
    return await _setting(INTENTS_RULES_ENABLED_KEY, db) == "true"


async def rule_types_enabled(db: Any = None) -> bool:
    """Residual metadata is usable only with its classifier and intent reading installed."""
    return RULE_CLASSIFICATION_AVAILABLE and await rules_enabled(db) and await intents_enabled(db)


def intent_type_visible(row: Any, *, rules_on: bool) -> bool:
    """A user-defined namesake remains visible when the built-in rule type is hidden."""
    return not (row["builtin"] and row["key"] == RULE_INTENT_TYPE) or rules_on


async def group_read_enabled(db: Any = None) -> bool:
    """Whether intents read their followed groups (``intents.group_read.enabled`` is ``true``; no row is off)."""
    return await _setting(INTENTS_GROUP_READ_KEY, db) == "true"


async def search_mode(db: Any = None) -> SearchMode:
    """How searches match (``leads.search_mode``; no row, or a value it does not know, is ``words``)."""
    raw = await _setting(LEADS_SEARCH_MODE_KEY, db)
    try:
        return SearchMode(raw) if raw is not None else SearchMode.WORDS
    except ValueError:
        return SearchMode.WORDS


async def valid_days(db: Any = None) -> int:
    """How long a new lead stays valid (``leads.valid_days``, default 90; a stored value out of range is the default)."""
    raw = await _setting(LEADS_VALID_DAYS_KEY, db)
    try:
        days = int(raw) if raw is not None else DEFAULT_VALID_DAYS
    except ValueError:
        return DEFAULT_VALID_DAYS
    return days if VALID_DAYS_RANGE[0] <= days <= VALID_DAYS_RANGE[1] else DEFAULT_VALID_DAYS
