"""Request-local database timing, shared by HTTP dispatch and pooled connections."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class ApiTiming:
    db_ms: float = 0.0
    db_queries: int = 0
    awaiting: str | None = None


current_api_timing: ContextVar[ApiTiming | None] = ContextVar("api_timing", default=None)


def record_query(record: Any) -> None:
    # asyncpg schedules this callback with call_soon, copying the query's context.
    # Mutate the request's timing object so dispatch sees it after another loop turn.
    timing = current_api_timing.get()
    if timing is not None:
        timing.db_ms += record.elapsed * 1000
        timing.db_queries += 1
