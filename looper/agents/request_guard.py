"""Keep the local API to the dashboard and local clients.

The server runs agents that execute code: a task's Workers run Claude with
every permission. A web page open in the user's browser must therefore not be
able to drive it. Browsers send ``Origin`` on cross-site requests, so a request
whose Origin is not the server's own is refused. When the server listens on a
loopback address, the ``Host`` must name a loopback address too; that defeats
DNS rebinding, where an attacker's name resolves to 127.0.0.1 and the page
becomes "same origin". Clients that are not browsers (curl, the CLI) send no
Origin and are unaffected.
"""

from __future__ import annotations

import ipaddress
import urllib.parse

_LOOPBACK_NAMES = frozenset({"localhost"})


def _is_loopback(hostname: str) -> bool:
    if hostname in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _hostname(authority: str) -> str:
    """The host of a ``Host`` header value such as ``localhost:8767`` or ``[::1]:8767``."""
    return (urllib.parse.urlsplit(f"//{authority}").hostname or "").lower()


def refusal(headers: dict[str, str], *, bind_host: str) -> str | None:
    """Why a request must be refused, or ``None`` to serve it.

    ``headers`` are the request headers with lower-case names; ``bind_host``
    is the address the server listens on.
    """
    host = headers.get("host", "")
    if host and _is_loopback(bind_host) and not _is_loopback(_hostname(host)):
        return f"host {host!r} is not this server"
    origin = headers.get("origin")
    if origin is not None and urllib.parse.urlsplit(origin).netloc.lower() != host.lower():
        return f"cross-origin request from {origin!r} refused"
    return None
