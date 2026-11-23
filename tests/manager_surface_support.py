"""The default Manager's model-facing tools and system prompt as main has them with every feature off.

One fixture (``fixtures/manager_surfaces_off.json``) freezes each item on its own: each tool's listing and the order
of those tools, and each ``## `` section of the system prompt and the order of those sections. Another feature's added
tool or section changes none of them; a changed byte of an existing one, a missing one or a reordering fails. The
default-off tests that drive a real Manager turn compare against it, so one intended change is one edit.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "manager_surfaces_off.json").read_text())


def normalized(value: Any, root: Path) -> str:
    """``value`` as JSON, with this run's root, interpreter and content-named files written the same every run."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    text = text.replace(str(root), "<root>").replace(sys.executable, "<python>")
    return re.sub(r"/[0-9a-f]{64}\.(md|json)", r"/<sha>.\1", text)


def _digest(value: Any, root: Path) -> str:
    return hashlib.sha256(normalized(value, root).encode()).hexdigest()


def sections(text: str) -> list[tuple[str, str]]:
    """``text`` cut before each line starting ``## ``: (heading, section text), in order; the opening text's heading
    is ``""``."""
    parts: list[tuple[str, str]] = []
    heading, lines = "", []
    for line in text.split("\n"):
        if line.startswith("## "):
            if heading or lines:
                parts.append((heading, "\n".join(lines)))
            heading, lines = line, []
        lines.append(line)
    parts.append((heading, "\n".join(lines)))
    return parts


def _frozen(items: list[tuple[str, Any]], baseline: dict[str, Any], root: Path) -> dict[str, Any]:
    current = dict(items)
    assert len(current) == len(items), "two items share a name"
    return {"order": [name for name, _ in items if name in baseline["each"]],
            "each": {name: _digest(current[name], root) if name in current else None for name in baseline["each"]}}


def surfaces(tools: list[dict[str, Any]], system: str, root: Path, *, variant: str = "default") -> dict[str, Any]:
    """The current bytes of what ``BASELINE`` froze, from a turn's tool listing and system prompt; ``variant`` names
    the system prompt (``default``, or ``stable_context`` with the stable context switch on)."""
    return {"tools": _frozen([(tool["name"], tool) for tool in tools], BASELINE["tools"], root),
            "system": _frozen(sections(system), BASELINE["system"][variant], root)}


def assert_main_surfaces(tools: list[dict[str, Any]], system: str, root: Path, *, variant: str = "default") -> None:
    """The existing tools and system prompt sections are main's, byte for byte and in main's order."""
    assert surfaces(tools, system, root, variant=variant) == {
        "tools": BASELINE["tools"], "system": BASELINE["system"][variant]}
