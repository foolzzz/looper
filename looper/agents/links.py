"""Links to the dashboard in the messages Looper sends to a chat: a task's end, a schedule's runs and alerts.

A chat client opens only an absolute URL, so a link starts with the
dashboard's address: the ops file's ``server.public_url`` when set (the
address people open the dashboard at, such as ``https://looper.example.com``
behind a proxy), else the server's own ``http://<host>:<port>``. The ops file
is read, and a malformed address refused, when ``looper serve`` starts
(``looper.ops_config``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# A server bound to every address is opened at the machine's own.
_ANY_ADDRESS = {"", "0.0.0.0", "::"}


class DashboardUrl(str):
    """The dashboard's address for links, and whether people outside this machine can open it: ``public`` is true
    only for the ops file's ``server.public_url``, never for the server's own address. Any plain ``str`` base
    (a test's, or none) is not public (``is_public``)."""

    public: bool

    def __new__(cls, value: str = "", *, public: bool = False) -> DashboardUrl:
        self = super().__new__(cls, value)
        self.public = public
        return self


def is_public(base: str) -> bool:
    """Whether a link starting with ``base`` opens for the people in a group chat."""
    return isinstance(base, DashboardUrl) and base.public


def dashboard_url(*, host: str, port: int, public_url: str = "") -> DashboardUrl:
    """The dashboard's address for links, without a trailing slash: ``public_url`` (checked by ``ops_config``),
    else the server's own."""
    if public_url:
        return DashboardUrl(public_url.rstrip("/"), public=True)
    shown = "127.0.0.1" if host in _ANY_ADDRESS else host
    return DashboardUrl(f"http://{f'[{shown}]' if ':' in shown else shown}:{port}")


def task_link(base: str, task_id: str) -> str:
    """The task's page; relative when ``base`` is empty."""
    return f"{base}/#tasks/{task_id}"


def schedule_link(base: str, schedule: Mapping[str, Any]) -> str:
    """The schedule's page; relative when ``base`` is empty."""
    return f"{base}/#projects/{schedule['project_id']}/schedules/{schedule['id']}"


def run_link(base: str, schedule: Mapping[str, Any], run_id: str) -> str:
    """The schedule page expanded at this run."""
    return f"{schedule_link(base, schedule)}/runs/{run_id}"
