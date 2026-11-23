"""Typed startup failures: retry transport failures quickly, configuration failures slowly."""
from __future__ import annotations

from typing import Literal, TypedDict

import httpx


class ChannelStartState(TypedDict):
    state: Literal["starting", "running", "retrying", "failed"]
    last_error: str | None
    next_attempt_at: str | None


class ChannelStartError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def transient(error: Exception) -> bool:
    if isinstance(error, ChannelStartError):
        return error.transient
    if isinstance(error.__cause__, httpx.TransportError):
        return True
    if isinstance(error, (TimeoutError, ConnectionError, httpx.TransportError)):
        return True
    from telegram.error import BadRequest, NetworkError, RetryAfter

    # Preserve the transport's structured classification; never infer it from error prose.
    return (getattr(error, "error_type", None) in {"timeout", "network", "transport"}
            or getattr(error, "http_status", 0) in {429, 500, 502, 503, 504}
            or isinstance(error, (NetworkError, RetryAfter)) and not isinstance(error, BadRequest))
