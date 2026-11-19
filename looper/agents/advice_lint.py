"""The code checks of an advice request before it is sent (the advisor RFC, section 9.3).

``lint`` reads every text of a request that passed the format check and returns each hit by category, field and
position (``Hit``), never what it matched: hits are what the Planner is told to fix (``describe``) and what the
consult records. The categories:

- ``secret``: common key and token formats, long random strings, and the values registered as secrets
  (``log_redaction``);
- ``binding``: a real name or place a binding stands for, written out;
- ``local_path``: an absolute or home path, or one of the local directories the caller names;
- ``url``: any URL (no address is allowed yet);
- ``identity``: an e-mail or IP address, a long hexadecimal id or UUID, or one of the ids the caller names (the task's,
  its steps', its runs');
- ``raw_log``: a timestamped line, a stack trace, or a log excerpt of more than ``LOG_LINES_MAX`` lines;
- ``dangling``: a reference to something the request does not contain ("see above", "the log"), or a file name that
  no artifact of the request shows;
- ``verbatim``: more than ``VERBATIM_MAX`` of the request copied from the local material (by 12-character pieces).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .advice import AdviceRequest, Binding

LOG_LINES_MAX = 20
VERBATIM_MAX = 0.30
SHINGLE = 12

_SECRETS = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    # A long run of base64 or URL-safe characters with upper and lower case letters and digits: what a key or token
    # looks like (a long identifier in code rarely mixes all three).
    r"|(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*[A-Z])"
    r"[A-Za-z0-9+/_-]{40,}={0,2}")
_PATHS = re.compile(r"(?<![\w.~/-])(?:/Users/|/home/|/root/|/private/|/var/folders/|/tmp/|/opt/|/etc/|/srv/|~/"
                    r"|[A-Za-z]:\\)[^\s\"'`)]*")
_URLS = re.compile(r"\b(?:https?|ftp|ssh|git|file)://[^\s\"'`)]+|\bwww\.[^\s\"'`)]+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_IP = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX_ID = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_TIMESTAMP = re.compile(r"^\s*\[?(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\d{2}:\d{2}:\d{2}[.,]\d+)", re.MULTILINE)
_STACK = re.compile(r"Traceback \(most recent call last\)|^\s+at [\w$.<>]+\([^)]*:\d+\)|File \"[^\"]+\", line \d+",
                    re.MULTILINE)
_DANGLING = re.compile(
    r"\b(?:see|as|mentioned|shown|described|quoted) above\b|\bthe above\b|\bsee (?:the )?logs?\b|\bin the logs?\b"
    r"|\bthe attached\b|见上|如上所述|上面(?:提到|说的|的日志|的代码)|见日志|看日志|日志里|前面提到", re.IGNORECASE)
_FILE_NAME = re.compile(r"(?<![\w/.-])[\w-]+(?:/[\w.-]+)*\.(?:py|pyi|ts|tsx|js|jsx|go|rs|java|kt|rb|php|c|cc|cpp|h|hpp"
                        r"|cs|swift|sql|yaml|yml|toml|ini|cfg|json|md|sh|txt|log|csv)\b")


@dataclass(frozen=True)
class Hit:
    """What a check found: its category, the field, and where in that field (characters ``start`` to ``end``)."""

    category: str
    field: str
    start: int
    end: int

    def view(self) -> dict[str, object]:
        return {"stage": "code", "category": self.category, "field": self.field, "start": self.start, "end": self.end}


_HOW = {
    "secret": "looks like a key, token or registered secret; remove it",
    "binding": "names what one of your aliases stands for; use the alias",
    "local_path": "looks like a local path; name what the file does instead",
    "url": "is a URL; describe what is there instead",
    "identity": "looks like an address, a person or an id; replace it with a role name",
    "raw_log": "is a raw log or stack trace; keep the exception type and the few relevant lines, without "
               f"timestamps, at most {LOG_LINES_MAX} lines",
    "dangling": "refers to something the request does not contain; say it in the request, or show it in an artifact",
    "verbatim": f"copies more than {int(VERBATIM_MAX * 100)}% of the local material word for word; restate it in "
                "general terms",
}


def describe(hits: Iterable[Hit]) -> list[str]:
    """What to fix for each field and category hit, with positions and never what was matched."""
    grouped: dict[tuple[str, str], list[Hit]] = {}
    for hit in hits:
        grouped.setdefault((hit.field, hit.category), []).append(hit)
    lines = []
    for (field, category), found in grouped.items():
        where = ", ".join(f"{h.start}–{h.end}" for h in found[:5]) + (" …" if len(found) > 5 else "")
        what = "the request as a whole" if field == "*" else f"{field} (characters {where})"
        lines.append(f"{what} {_HOW[category]}")
    return lines


def _found(pattern: re.Pattern[str], text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in pattern.finditer(text)]


def _literal(needles: Iterable[str], text: str, *, ignore_case: bool = False) -> list[tuple[int, int]]:
    spans, haystack = [], text.lower() if ignore_case else text
    for needle in needles:
        needle = needle.strip()
        if len(needle) < 3:
            continue
        probe = needle.lower() if ignore_case else needle
        start = haystack.find(probe)
        while start != -1:
            spans.append((start, start + len(probe)))
            start = haystack.find(probe, start + 1)
    return spans


def _shingles(text: str) -> set[str]:
    flat = " ".join(text.split()).lower()
    return {flat[i:i + SHINGLE] for i in range(0, max(0, len(flat) - SHINGLE + 1))}


def lint(request: AdviceRequest, *, bindings: Sequence[Binding] = (), material: str = "",
         local_paths: Sequence[str] = (), ids: Sequence[str] = (), secrets: Sequence[str] | None = None) -> list[Hit]:
    """Every hit of the code checks in ``request``: ``bindings`` its aliases, ``material`` the local material it was
    written from, ``local_paths`` directories of this machine, ``ids`` the task's and its runs' ids, ``secrets`` the
    values registered as secrets (``log_redaction.registered()`` when None)."""
    from .log_redaction import registered

    secret_values = registered() if secrets is None else tuple(secrets)
    shown = " ".join(a.get("content", "") for a in request.fields.get("artifacts", ()))
    hits: list[Hit] = []
    for field, text in request.strings():
        taken: list[tuple[int, int]] = []

        def add(category: str, spans: Iterable[tuple[int, int]]) -> None:
            for start, end in spans:
                if not any(s < end and start < e for s, e in taken):  # one category per span
                    taken.append((start, end))
                    hits.append(Hit(category, field, start, end))

        add("secret", _found(_SECRETS, text) + _literal(secret_values, text))
        add("binding", _literal((b.refers_to for b in bindings), text, ignore_case=True))
        add("local_path", _found(_PATHS, text) + _literal(local_paths, text))
        add("url", _found(_URLS, text))
        add("identity", _found(_EMAIL, text) + _found(_IP, text) + _found(_UUID, text) + _found(_HEX_ID, text)
            + _literal(ids, text))
        log_spans = _found(_TIMESTAMP, text) + _found(_STACK, text)
        if field.startswith("artifacts[") and field.endswith(".content") and text.count("\n") >= LOG_LINES_MAX:
            log_spans.append((0, len(text)))
        add("raw_log", log_spans)
        dangling = _found(_DANGLING, text)
        if not field.startswith("artifacts["):
            dangling += [(m.start(), m.end()) for m in _FILE_NAME.finditer(text) if m.group(0) not in shown]
        add("dangling", dangling)
    if material.strip():
        written = _shingles(" ".join(text for _, text in request.strings()))
        if written and len(written & _shingles(material)) / len(written) > VERBATIM_MAX:
            hits.append(Hit("verbatim", "*", 0, 0))
    return hits
