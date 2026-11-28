"""In-memory desktop driver for tests.

Scripts the next result of a message kind. It does not capture the screen,
post input, prompt for a permission, or start a process. A result other than
``ok`` does not record an applied action. The default screenshot body states
the redaction default (password controls on, no custom regions) and contains
no image bytes.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from .contract import ContractError, in_use_code, is_in_use

_BUSINESS = frozenset({"ok", "forbidden", "no_effect", "browser_app", "audit_unavailable"})


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.applied: list[dict] = []
        self.grants: list[tuple[str, dict, str]] = []
        self._queue: dict[str, list[tuple[str, dict]]] = {}
        self._lock = threading.Lock()

    def script(self, kind: str, code: str, body: Mapping | None = None) -> None:
        if code not in _BUSINESS and not is_in_use(code):
            raise ContractError("bad_request", "scripted code")
        with self._lock:
            self._queue.setdefault(kind, []).append((code, dict(body or {})))

    def handle(self, kind: str, body: Mapping, session_id: str) -> tuple[str, Mapping]:
        copied = dict(body)
        with self._lock:
            self.calls.append((kind, copied))
            queued = self._queue.get(kind) or []
            if queued:
                code, payload = queued.pop(0)
            else:
                code, payload = _default(kind, copied)
            if kind == "act" and code == "ok":
                self.applied.append(copied)
            if kind in {"grant", "revoke"} and code == "ok":
                self.grants.append((kind, copied, session_id))
        return code, payload


def scripted_in_use(session_id: str) -> str:
    return in_use_code(session_id)


def _default(kind: str, body: Mapping) -> tuple[str, dict]:
    del body
    if kind == "observe":
        return "ok", {"observation_id": "obs-1", "text": "window"}
    if kind == "screenshot":
        return "ok", {"redacted": True, "password_controls": True, "custom_regions": []}
    if kind == "health":
        return "ok", {"status": "ok"}
    if kind == "stop":
        return "ok", {"stopped": True}
    if kind in {"act", "grant", "revoke"}:
        return "ok", {}
    return "bad_request", {}
