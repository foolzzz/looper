"""Links to the dashboard in the messages Looper sends to a chat: a task's end, a schedule's runs and alerts.

A chat client opens only an absolute URL, so a link starts with the
dashboard's address: ``LOOPER_PUBLIC_URL`` when set (the address people open
the dashboard at, such as ``https://looper.example.com`` behind a proxy),
else the server's own ``http://<host>:<port>``. ``looper serve`` reads it at
start and does not start when it is malformed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .. import env

PUBLIC_URL_ENV = "LOOPER_PUBLIC_URL"
# A server bound to every address is opened at the machine's own.
_ANY_ADDRESS = {"", "0.0.0.0", "::"}


def dashboard_url(*, host: str, port: int) -> str:
    """The dashboard's address for links, without a trailing slash; raises ``ValueError`` for a malformed
    ``LOOPER_PUBLIC_URL``."""
    raw = (env.get(PUBLIC_URL_ENV) or "").strip()
    if not raw:
        shown = "127.0.0.1" if host in _ANY_ADDRESS else host
        return f"http://{f'[{shown}]' if ':' in shown else shown}:{port}"
    try:
        parts = urlsplit(raw)
        valid = (parts.scheme in ("http", "https") and bool(parts.hostname) and parts.port != 0
                 and not (parts.username or parts.password or parts.query or parts.fragment))
    except ValueError:  # a port that is not a number up to 65535
        valid = False
    if not valid:
        raise ValueError(f"{PUBLIC_URL_ENV} must be the dashboard's http:// or https:// address, without a user, "
                         f"query or fragment, such as https://looper.example.com; got {raw[:200]!r}")
    return raw.rstrip("/")


def task_link(base: str, task_id: str) -> str:
    """The task's page; relative when ``base`` is empty."""
    return f"{base}/#tasks/{task_id}"


def schedule_link(base: str, schedule: Mapping[str, Any]) -> str:
    """The schedule's page; relative when ``base`` is empty."""
    return f"{base}/#projects/{schedule['project_id']}/schedules/{schedule['id']}"
