"""Dashboard task-event lists stay equal to the server's closed enumerations.

The dashboard drops a task event whose reason or method is not in its list. These lists are the
dashboard's copy of the OperatorChatTaskEvent schema, including the refused-reason enum.
"""

import re
from pathlib import Path

from looper.agents.api_v1 import document


_DASHBOARD = Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "work-task-conversation.ts"
_NAMES = (
    "COMMANDS",
    "QUEUED_REASONS",
    "SWITCH_REASONS",
    "FRESH_REASONS",
    "STOP_METHODS",
    "SUMMARY_MODES",
    "SANDBOX_CATEGORIES",
    "SANDBOX_CASES",
    "SANDBOX_CLIENTS",
    "REFUSED",
)
_ARRAY = re.compile(
    r"^const (?P<name>[A-Z][A-Z0-9_]*) = \[(?P<body>.*?)\] as const;$",
    re.MULTILINE | re.DOTALL,
)
_FIELDS = {
    "COMMANDS": ("command", "command"),
    "QUEUED_REASONS": ("queued", "reason"),
    "SWITCH_REASONS": ("model_switch_rejected", "reason"),
    "FRESH_REASONS": ("session_fresh", "reason"),
    "STOP_METHODS": ("stopped", "method"),
    "SUMMARY_MODES": ("turn_summary", "mode"),
    "SANDBOX_CATEGORIES": ("sandbox_denial", "category"),
    "SANDBOX_CASES": ("sandbox_denial", "case"),
    "SANDBOX_CLIENTS": ("sandbox_denial", "client"),
    "REFUSED": ("refused", "reason"),
}


def _quoted(name: str, body: str) -> list[str]:
    values: list[str] = []
    rest = body.strip()
    if not rest:
        raise AssertionError(f"{name}: array is empty")
    while rest:
        if len(rest) < 2 or rest[0] != '"':
            raise AssertionError(f"{name}: expected a double-quoted string, got {rest[:40]!r}")
        end = rest.find('"', 1)
        if end < 0 or "\\" in rest[1:end]:
            raise AssertionError(f"{name}: expected a plain double-quoted string, got {rest[:40]!r}")
        values.append(rest[1:end])
        rest = rest[end + 1:].lstrip()
        if rest.startswith(","):
            rest = rest[1:].lstrip()
            if not rest:
                raise AssertionError(f"{name}: trailing comma")
        elif rest:
            raise AssertionError(f"{name}: expected a comma, got {rest[:40]!r}")
    return values


def _dashboard_lists() -> dict[str, list[str]]:
    if not _DASHBOARD.is_file():
        raise AssertionError("web/src/lib/work-task-conversation.ts was not found")
    found: dict[str, list[str]] = {}
    for match in _ARRAY.finditer(_DASHBOARD.read_text(encoding="utf-8")):
        name = match.group("name")
        if name in found:
            raise AssertionError(f"{name}: declared more than once")
        found[name] = _quoted(name, match.group("body"))
    missing = [name for name in _NAMES if name not in found]
    if missing:
        raise AssertionError(
            "web/src/lib/work-task-conversation.ts is missing const arrays: " + ", ".join(missing))
    return found


def _server_lists() -> dict[str, list[str]]:
    schema = document()["components"]["schemas"]["OperatorChatTaskEvent"]
    variants = schema.get("oneOf")
    if not isinstance(variants, list) or not variants:
        raise AssertionError("OperatorChatTaskEvent has no oneOf")
    lists: dict[str, list[str]] = {}
    for name, (kind, field) in _FIELDS.items():
        matches = [
            variant for variant in variants
            if variant.get("properties", {}).get("kind", {}).get("const") == kind
            and isinstance(variant.get("properties", {}).get(field, {}).get("enum"), list)
        ]
        if len(matches) != 1:
            raise AssertionError(f"{kind}.{field}: expected one enum, found {len(matches)}")
        # A nullable field lists None next to its values; the dashboard keeps null apart from the list.
        enum = [item for item in matches[0]["properties"][field]["enum"] if item is not None]
        if not enum or any(not isinstance(item, str) for item in enum):
            raise AssertionError(f"{kind}.{field}: enum is not a list of strings")
        lists[name] = list(enum)
    return lists


def test_dashboard_task_event_lists_match_the_server():
    """Each dashboard array equals the server enum for that task-event field, in the same order."""
    dashboard = _dashboard_lists()
    server = _server_lists()
    drifted = [
        f"{name}: dashboard={dashboard[name]!r} server={server[name]!r}"
        for name in _NAMES
        if dashboard[name] != server[name]
    ]
    assert not drifted, "task-event lists drifted:\n" + "\n".join(drifted)
