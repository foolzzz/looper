"""Why a run failed, as a type (the engine reliability RFC, section 4; record mode only records it).

A failed run gets one ``FailureCause``, and each cause belongs to one ``FailureClass``:

- ``quota``: its account's usage window is used up, or it was rate-limited (wait for the reset);
- ``transient``: a connection, an overloaded provider, a slow local start (try again later);
- ``environment``: a missing executable or directory, a login, TLS, the profile (someone must fix it);
- ``judgment``: a timeout, a full context, the spend cap, the run's own report, or anything not known.

The class is read from typed fields wherever the runtime gives them (Claude Code's ``api_error`` and
``rate_limit_event``, Codex app-server's ``codexErrorInfo``, the API runtime's error kind, the exceptions Looper raises
when it cannot start a run). Text is matched only where a runtime has no such field (an older Claude Code, Codex
exec); such a failure says ``signal: text``. What cannot be told is ``unknown``, a judgment: what happens today.

``ClaudeRunRecorder`` gathers each run's ``Evidence`` from every event it accounts, before the record's own keep
filter, and on close asks ``recorded``: nothing while ``engine.failure_typing`` is ``off`` (the default), else the
run's ``Failure``, written to ``llm_records.failure_class`` and ``failure_cause`` and ``metadata.failure``. A run that
Looper stopped (``interrupted``) has none. Recording never fails a run.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from .runtimes import RuntimeAdapter

logger = logging.getLogger(__name__)

SETTING_KEY = "engine.failure_typing"


class FailureTyping(StrEnum):
    """What ``engine.failure_typing`` asks for."""

    OFF = "off"  # nothing is classified (no row: the default)
    RECORD = "record"  # failed runs are classified and recorded; nothing else changes
    ACT = "act"  # record and handle failures with bounded recovery and operator-controlled holds


class FailureClass(StrEnum):
    QUOTA = "quota"
    TRANSIENT = "transient"
    ENVIRONMENT = "environment"
    JUDGMENT = "judgment"


class FailureCause(StrEnum):
    USAGE_LIMIT = "usage_limit"  # a usage window is used up (a subscription's weekly limit, usageLimitExceeded)
    RATE_LIMIT = "rate_limit"  # throttled (HTTP 429, rateLimitExceeded) without a used-up window
    CONNECTION_FAILED = "connection_failed"  # DNS, a connection, a stream that broke off
    PROVIDER_OVERLOADED = "provider_overloaded"  # 5xx, 529, serverOverloaded, a model at capacity
    START_TIMEOUT = "start_timeout"  # Looper's own process host did not come up in time
    RUNTIME_MISSING = "runtime_missing"  # the executable or the working directory is not there
    LOGIN = "login"  # not logged in, HTTP 401/403
    TLS = "tls"  # a certificate the run cannot verify
    PROFILE = "profile"  # the runtime cannot run this launch (its configuration)
    TIMEOUT = "timeout"  # it ran over its time limit
    CONTEXT = "context"  # its prompt is over the model's context window
    BUDGET = "budget"  # it reached its spend cap
    REPORT = "report"  # its own report says it failed, breaks the contract or does not match the files
    UNKNOWN = "unknown"  # none of the above can be told


CAUSE_CLASS: Mapping[FailureCause, FailureClass] = {
    FailureCause.USAGE_LIMIT: FailureClass.QUOTA,
    FailureCause.RATE_LIMIT: FailureClass.QUOTA,
    FailureCause.CONNECTION_FAILED: FailureClass.TRANSIENT,
    FailureCause.PROVIDER_OVERLOADED: FailureClass.TRANSIENT,
    FailureCause.START_TIMEOUT: FailureClass.TRANSIENT,
    FailureCause.RUNTIME_MISSING: FailureClass.ENVIRONMENT,
    FailureCause.LOGIN: FailureClass.ENVIRONMENT,
    FailureCause.TLS: FailureClass.ENVIRONMENT,
    FailureCause.PROFILE: FailureClass.ENVIRONMENT,
    FailureCause.TIMEOUT: FailureClass.JUDGMENT,
    FailureCause.CONTEXT: FailureClass.JUDGMENT,
    FailureCause.BUDGET: FailureClass.JUDGMENT,
    FailureCause.REPORT: FailureClass.JUDGMENT,
    FailureCause.UNKNOWN: FailureClass.JUDGMENT,
}

# How a Worker's report or a Reviewer's verdict read (``worker.RunOutcome``) when the run itself worked but its own
# report says the step failed: a judgment. A Reviewer's ``not_passed`` is a verdict, not a failure of the run.
REPORT_OUTCOMES = frozenset({"reported_failed", "contract", "files_mismatch"})


class Signal(StrEnum):
    TYPED = "typed"  # from a typed field or exception
    TEXT = "text"  # from a runtime's error text, where it has no typed field


@dataclass(frozen=True)
class Failure:
    """A failed run's cause, what told it, and, for a used-up window, when the window resets."""

    cause: FailureCause
    signal: Signal = Signal.TYPED
    resets_at: datetime | None = None

    @property
    def cls(self) -> FailureClass:
        return CAUSE_CLASS[self.cause]

    def metadata(self) -> dict[str, Any]:
        """What ``metadata.failure`` keeps beside the two columns."""
        found: dict[str, Any] = {"signal": self.signal.value}
        if self.resets_at is not None:
            found["resets_at"] = self.resets_at.isoformat()
        return found


def _epoch(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


_NETWORK_CODES = frozenset({"ENOTFOUND", "ECONNRESET", "EAI_AGAIN", "ECONNREFUSED"})


def _network_code(event: Mapping[str, Any]) -> str | None:
    """Read exact codes only in a model API error envelope, never tool output or prose."""
    api_error = event.get("api_error")
    codes = [event.get("api_error_code")]
    if isinstance(api_error, Mapping):
        codes.append(api_error.get("code"))
    if event.get("error_source") == "model_api":
        codes.extend(event.get(key) for key in ("code", "error_code", "error_kind", "exit_reason"))
    return next((code for code in codes if isinstance(code, str) and code in _NETWORK_CODES), None)


class Evidence:
    """What a run's stream said about how it may fail, taken from each event as the recorder accounts it.

    Only the few facts the classifiers read are kept (no text beyond Codex's last error messages, bounded)."""

    _MESSAGES = 5

    def __init__(self) -> None:
        self.rejected: dict[str, Any] | None = None  # Claude Code: the last rate limit info whose status is rejected
        self.assistant_error: str | None = None  # Claude Code: a synthetic assistant message's error kind
        self.assistant_api_error: str | None = None
        self.assistant_status: int | None = None
        self.api_retries = 0  # Claude Code: its own retries of a failed request (system api_retry)
        self.codex_error_info: Any = None  # Codex app-server: the final error's codexErrorInfo
        self.codex_messages: list[str] = []  # Codex: the latest error messages, oldest first
        self.network_error_code: str | None = None  # structured model API errors only

    def observe(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "assistant" or (kind == "system" and event.get("subtype") == "api_error"):
            # A response or a different API error supersedes the failed request, even after an internal retry.
            self.network_error_code = (_network_code(event)
                                       if isinstance(event.get("error"), str) or kind == "system" else None)
        elif kind == "result":
            code = _network_code(event)
            # A generic failure result can wrap the last API error; a specific result replaces it.
            if (code is not None or not _failed(event)
                    or event.get("subtype") not in (None, "error_during_execution")
                    or any(event.get(key) is not None for key in
                           ("api_error", "api_error_code", "api_error_type", "api_error_status",
                            "error_source", "error_kind", "codex_error_info"))):
                self.network_error_code = code
        elif kind == "tool_result" or (kind == "user" and isinstance(event.get("message"), Mapping)
                and isinstance(content := event["message"].get("content"), list)
                and any(isinstance(block, Mapping) and block.get("type") == "tool_result" for block in content)):
            self.network_error_code = None
        if kind == "rate_limit_event":
            self._rate_limit(event.get("rate_limit_info"))
        elif kind == "assistant" and isinstance(event.get("error"), str):
            self.assistant_error = event["error"]
            if isinstance(event.get("api_error"), str):
                self.assistant_api_error = event["api_error"]
            if type(event.get("api_error_status")) is int:
                self.assistant_status = event["api_error_status"]
            params = event.get("api_error_params")
            if isinstance(params, Mapping):
                self._rate_limit(params.get("rate_limit_info"))
        elif kind == "system" and event.get("subtype") == "api_retry":
            self.api_retries += 1
        elif kind == "system" and event.get("subtype") == "codex_error":
            if isinstance(event.get("message"), str):
                self.codex_messages = [*self.codex_messages, event["message"]][-self._MESSAGES:]
            if event.get("codex_error_info") is not None and event.get("will_retry") is not True:
                self.codex_error_info = event["codex_error_info"]
        elif kind == "result" and event.get("codex_error_info") is not None:
            self.codex_error_info = event["codex_error_info"]

    def _rate_limit(self, info: Any) -> None:
        if isinstance(info, Mapping) and info.get("status") == "rejected":
            self.rejected = dict(info)

    @property
    def resets_at(self) -> datetime | None:
        return _epoch((self.rejected or {}).get("resetsAt"))


# --- Text, only where a runtime has no typed field (each rule has a recorded sample in the tests) ---

_CLAUDE_TEXT: tuple[tuple[re.Pattern[str], FailureCause], ...] = (
    (re.compile(r"You've hit your (?:\w+ )?limit", re.I), FailureCause.USAGE_LIMIT),
    (re.compile(r"Can't reach the API server", re.I), FailureCause.CONNECTION_FAILED),
    (re.compile(r"Prompt is too long", re.I), FailureCause.CONTEXT),
    (re.compile(r"unable to get local issuer certificate|self[- ]signed certificate", re.I), FailureCause.TLS),
)
# The messages codex-cli 0.169.0 carries for these errors (its error type's Display texts).
_CODEX_TEXT: tuple[tuple[re.Pattern[str], FailureCause], ...] = (
    (re.compile(r"You've hit your usage limit|Quota exceeded\. Check your plan", re.I), FailureCause.USAGE_LIMIT),
    (re.compile(r"exceeded retry limit, last status: 429", re.I), FailureCause.RATE_LIMIT),
    (re.compile(r"Selected model is at capacity|exceeded retry limit, last status: 5\d\d", re.I),
     FailureCause.PROVIDER_OVERLOADED),
    (re.compile(r"stream disconnected before completion|workspace routing discovery timed out|"
                r"error sending request", re.I), FailureCause.CONNECTION_FAILED),
    (re.compile(r"ran out of room in the model's context window", re.I), FailureCause.CONTEXT),
    (re.compile(r"unable to get local issuer certificate|self[- ]signed certificate", re.I), FailureCause.TLS),
)


def _text(rules: tuple[tuple[re.Pattern[str], FailureCause], ...], texts: list[str]) -> FailureCause | None:
    for pattern, cause in rules:
        if any(pattern.search(text) for text in texts):
            return cause
    return None


def _failed(result: Mapping[str, Any] | None) -> bool:
    return bool(result) and (result.get("is_error") is True or result.get("subtype") not in (None, "success"))


def _status_cause(status: Any) -> FailureCause | None:
    if type(status) is not int:
        return None
    if status == 429:
        return FailureCause.RATE_LIMIT
    if status in (401, 403):
        return FailureCause.LOGIN
    if status >= 500:
        return FailureCause.PROVIDER_OVERLOADED
    return None


# --- Each runtime's classifier (its adapter's ``failure_signal``) ---

def claude_code(result: Mapping[str, Any] | None, evidence: Evidence) -> Failure | None:
    """A Claude Code run: its result's ``api_error``, ``api_error_status`` and ``api_error_type``, its stream's
    rejected ``rate_limit_event`` and synthetic assistant error; text only for a result without those fields."""
    if result is not None and not _failed(result):
        return None
    result = result or {}
    api_error = result.get("api_error") or evidence.assistant_api_error
    status = result.get("api_error_status") if type(result.get("api_error_status")) is int else evidence.assistant_status
    quota = (api_error == "usage_limit_reached" or status == 429 or result.get("api_error_type") == "rate_limit_error"
             or evidence.assistant_error == "rate_limit")
    if quota or (evidence.rejected is not None and result.get("is_error") is True):
        used_up = api_error == "usage_limit_reached" or evidence.rejected is not None
        return Failure(FailureCause.USAGE_LIMIT if used_up else FailureCause.RATE_LIMIT, resets_at=evidence.resets_at)
    if api_error == "connection_failed":
        return Failure(FailureCause.CONNECTION_FAILED)
    if (cause := _status_cause(status)) is not None:
        return Failure(cause)
    if result.get("api_error_type") in ("overloaded_error", "api_error"):
        return Failure(FailureCause.PROVIDER_OVERLOADED)
    if result.get("api_error_type") in ("authentication_error", "permission_error"):
        return Failure(FailureCause.LOGIN)
    texts = [str(result.get("result") or ""), *(str(error) for error in result.get("errors") or [])]
    if (cause := _text(_CLAUDE_TEXT, texts)) is not None:
        return Failure(cause, Signal.TEXT)
    return None


_CODEX_INFO: Mapping[str, FailureCause] = {
    "usageLimitExceeded": FailureCause.USAGE_LIMIT,
    "rateLimitExceeded": FailureCause.RATE_LIMIT,
    "serverOverloaded": FailureCause.PROVIDER_OVERLOADED,
    "internalServerError": FailureCause.PROVIDER_OVERLOADED,
    "unauthorized": FailureCause.LOGIN,
    "contextWindowExceeded": FailureCause.CONTEXT,
    "sessionBudgetExceeded": FailureCause.BUDGET,
}
_CODEX_CONNECTION = ("httpConnectionFailed", "responseStreamConnectionFailed", "responseStreamDisconnected")


def _codex_info(info: Any) -> FailureCause | None:
    if isinstance(info, str):
        return _CODEX_INFO.get(info)
    if isinstance(info, Mapping):
        for name in _CODEX_CONNECTION:
            if isinstance(info.get(name), Mapping):
                return _status_cause(info[name].get("httpStatusCode")) or FailureCause.CONNECTION_FAILED
    return None


def codex(result: Mapping[str, Any] | None, evidence: Evidence) -> Failure | None:
    """A Codex run: the app-server's ``codexErrorInfo`` (kept on its error events and failed result); for exec,
    which reports only messages, its error texts."""
    if result is not None and not _failed(result):
        return None
    info = (result or {}).get("codex_error_info", evidence.codex_error_info)
    if (cause := _codex_info(info)) is not None:
        return Failure(cause)
    texts = [str(error) for error in (result or {}).get("errors") or []] + evidence.codex_messages[::-1]
    if (cause := _text(_CODEX_TEXT, texts)) is not None:
        return Failure(cause, Signal.TEXT)
    return None


_API_KINDS: Mapping[str, FailureCause] = {
    "rate_limited": FailureCause.RATE_LIMIT,
    "auth": FailureCause.LOGIN,
    "context_overflow": FailureCause.CONTEXT,
    "not_found": FailureCause.PROFILE,
}


def api(result: Mapping[str, Any] | None, evidence: Evidence) -> Failure | None:
    """An API runtime run: the error kind its loop typed (``ErrorKind``), with the HTTP status."""
    if not _failed(result):
        return None
    assert result is not None
    kind = result.get("error_kind")
    if kind == "unavailable":
        return Failure(FailureCause.CONNECTION_FAILED if result.get("error_status") is None
                       else FailureCause.PROVIDER_OVERLOADED)
    return Failure(_API_KINDS[kind]) if kind in _API_KINDS else None


# --- Looper's own reasons ---

def start_failure(exc: BaseException) -> Failure:
    """A run Looper could not start, from the exception that said so."""
    from .runtimes import RuntimeUnsupported
    from .runtimes.channel import RunStartTimeout

    if isinstance(exc, RunStartTimeout):
        return Failure(FailureCause.START_TIMEOUT)
    if isinstance(exc, RuntimeUnsupported):
        return Failure(FailureCause.PROFILE)
    cause = exc if isinstance(exc, OSError) else exc.__cause__
    if isinstance(cause, (FileNotFoundError, NotADirectoryError, PermissionError)):
        return Failure(FailureCause.RUNTIME_MISSING)
    return Failure(FailureCause.UNKNOWN)


TIMED_OUT = Failure(FailureCause.TIMEOUT)


def classify(runtime: RuntimeAdapter | None, result: Mapping[str, Any] | None, evidence: Evidence, *,
             outcome: str | None = None, given: Failure | None = None) -> Failure:
    """A failed run's ``Failure``: ``given`` (what Looper itself knows: it could not start, it timed out), else its
    runtime's signal, else its own report, else its spend cap, else ``unknown``."""
    if given is not None:
        return given
    if runtime is not None:
        try:
            if (found := runtime.failure_signal(dict(result) if result is not None else None, evidence)) is not None:
                return found
        except Exception:
            logger.exception("could not classify a %s run's failure", runtime.type)
    if outcome in REPORT_OUTCOMES:
        return Failure(FailureCause.REPORT)
    if runtime is not None and result is not None:
        try:
            if runtime.budget_error(dict(result)) is not None:
                return Failure(FailureCause.BUDGET)
        except Exception:
            logger.exception("could not read a %s run's spend cap", runtime.type)
    return Failure(FailureCause.UNKNOWN)


def network_unavailable(runtime: RuntimeAdapter | None, result: Mapping[str, Any] | None, evidence: Evidence, *,
                        given: Failure | None = None) -> bool:
    """Whether a failed run has a structured model connection failure, independent of failure-typing mode.

    Runtime classifiers already distinguish transport errors from quota, auth and HTTP failures. Their text
    fallbacks never qualify. Exact network codes qualify only in dedicated API fields or an explicitly marked
    model API envelope; a tool's connection failure must not pause the task.
    """
    if given is not None:
        return given.signal is Signal.TYPED and given.cause is FailureCause.CONNECTION_FAILED
    if result is not None and not _failed(result):
        return False
    found = classify(runtime, result, evidence)
    if found.signal is Signal.TYPED and found.cause is not FailureCause.UNKNOWN:
        return found.cause is FailureCause.CONNECTION_FAILED
    return _network_code(result or {}) is not None or evidence.network_error_code is not None


async def typing_mode(*, pool: asyncpg.Pool | None = None) -> FailureTyping:
    """What ``engine.failure_typing`` is set to; ``off`` without a row, with a value it does not know (logged), or
    when it cannot be read."""
    from ..db import system_settings

    try:
        stored = (await system_settings.get_settings([SETTING_KEY], pool=pool)).get(SETTING_KEY)
    except Exception:
        logger.warning("could not read %s; failures are not classified", SETTING_KEY, exc_info=True)
        return FailureTyping.OFF
    if stored is None:
        return FailureTyping.OFF
    try:
        return FailureTyping(stored)
    except ValueError:
        logger.warning("%s has an unknown value %r; failures are not classified", SETTING_KEY, stored)
        return FailureTyping.OFF


async def recorded(runtime: RuntimeAdapter | None, result: Mapping[str, Any] | None, evidence: Evidence, *,
                   outcome: str | None = None, given: Failure | None = None) -> Failure | None:
    """The ``Failure`` to record for a run that failed; None while failure typing is off, or when classifying it
    went wrong (logged: recording never fails a run)."""
    if await typing_mode() is FailureTyping.OFF:
        return None
    try:
        return classify(runtime, result, evidence, outcome=outcome, given=given)
    except Exception:
        logger.exception("could not classify a run's failure")
        return None


def check_sql() -> str:
    """``llm_records_failure_check``: both columns NULL, or a cause with its own class (schema.sql writes the same;
    a NULL beside a value would make the row comparison NULL, which a check lets through, so both are tested)."""
    pairs = ", ".join(f"('{cls.value}', '{cause.value}')" for cause, cls in CAUSE_CLASS.items())
    return ("(failure_class IS NULL AND failure_cause IS NULL) OR (failure_class IS NOT NULL AND failure_cause IS NOT "
            f"NULL AND (failure_class, failure_cause) IN ({pairs}))")
