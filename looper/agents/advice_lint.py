"""The code checks of an advice request before it is sent (the advisor RFC, section 9.3).

``lint`` reads every text of a request that passed the format check and returns each hit by category, field and
position (``Hit``), never what it matched: hits are what the Planner is told to fix (``describe``) and what the
consult records. The categories:

- ``secret``: common key and token formats, long random strings, and the values registered as secrets
  (``log_redaction``);
- ``binding``: a real name or place a binding stands for, written out, however short;
- ``local_path``: an absolute path (two or more segments, any root), a home or Windows or network path, or one of
  the local directories the caller names;
- ``url``: a URI of any scheme (``https://``, ``data:``, ``mailto:`` …), or one of the internal domains the caller
  names, or a host under one;
- ``identity``: an e-mail, IPv4 or IPv6 address, a long hexadecimal id or UUID, or one of the ids the caller names
  (the task's, its steps', its runs');
- ``raw_log``: a timestamped line, a Python, Java, JavaScript or Go stack trace, or a log excerpt of more than
  ``LOG_LINES_MAX`` lines;
- ``dangling``: a reference to something the request does not contain ("see above", "the log"), or a file name that
  no artifact of the request shows;
- ``verbatim``: more than ``VERBATIM_MAX`` of the request copied from the local material, counted over every
  ``SHINGLE``-character window of the request.

Every check but ``dangling`` and ``verbatim`` reads each text as written and as views of it (``_views``): NFKC
(full-width and compatibility forms as plain ones) with format characters such as zero-width ones removed and
spaces collapsed, case-folded for the literal values; with ``\\u``, ``\\x``, ``%XX`` and ``&#…;`` escapes decoded; and
each run of Base64 decoded once. A hit in a view is mapped back to the characters of the text it came from. The
field limits keep every run short enough to decode.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .advice import AdviceRequest, Binding

LOG_LINES_MAX = 20
VERBATIM_MAX = 0.30
SHINGLE = 12
_BASE64_RUNS_MAX = 64  # per text: decoded once each

_SECRETS = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    # A long run of base64 or URL-safe characters with upper and lower case letters and digits: what a key or token
    # looks like (a long identifier in code rarely mixes all three).
    r"|(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*[A-Z])"
    r"[A-Za-z0-9+/_-]{40,}={0,2}")
_PATHS = re.compile(
    # any absolute path of two or more segments, whatever its root and case; a home path; a Windows drive or
    # network path
    r"(?<![\w.~:/\\-])/(?:[\w.@%+-]+/)+[\w.@%+-]*"
    r"|(?<![\w.~/-])~[\w.-]*/[^\s\"'`)]*"
    r"|(?<![\w])[A-Za-z]:\\[^\s\"'`)]*|(?<![\w\\])\\\\[\w.-]+\\[^\s\"'`)]*")
_URLS = re.compile(r"\b[a-z][a-z0-9+.-]{0,31}://[^\s\"'`)]+|\bwww\.[^\s\"'`)]+"
                   r"|\b(?:data|mailto|urn|javascript|blob|tel|sms|magnet|cid|mid|news|sips?|xmpp|about):"
                   r"[^\s\"'`)]+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_IP = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
# What may be an IPv6 address; ``ipaddress`` decides.
_IP6 = re.compile(r"(?<![\w:.])(?=[0-9A-Fa-f:.]*[0-9A-Fa-f])[0-9A-Fa-f]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(?![\w:])")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX_ID = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_TIMESTAMP = re.compile(r"^\s*\[?(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\d{2}:\d{2}:\d{2}[.,]\d+)", re.MULTILINE)
_STACK = re.compile(
    r"Traceback \(most recent call last\)|File \"[^\"]+\", line \d+"  # Python
    r"|^\s*at [\w$.<>/:\[\] -]+\([^)\n]*:\d+(?::\d+)?\)"  # Java, JavaScript: indented or not
    r"|^\s*(?:Exception in thread|Caused by:)"
    r"|\bgoroutine \d+ \[[^\]\n]+\]:|^\s*[\w./-]+\.go:\d+(?: \+0x[0-9a-f]+)?\s*$",  # Go
    re.MULTILINE)
_DANGLING = re.compile(
    r"\b(?:see|as|mentioned|shown|described|quoted) above\b|\bthe above\b|\bsee (?:the )?logs?\b|\bin the logs?\b"
    r"|\bthe attached\b|见上|如上所述|上面(?:提到|说的|的日志|的代码)|见日志|看日志|日志里|前面提到", re.IGNORECASE)
_FILE_NAME = re.compile(r"(?<![\w/.-])[\w-]+(?:/[\w.-]+)*\.(?:py|pyi|ts|tsx|js|jsx|go|rs|java|kt|rb|php|c|cc|cpp|h|hpp"
                        r"|cs|swift|sql|yaml|yml|toml|ini|cfg|json|md|sh|txt|log|csv)\b")
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})|\\x([0-9a-fA-F]{2})|%([0-9a-fA-F]{2})"
                     r"|&#x([0-9a-fA-F]{1,6});|&#(\d{1,7});")
_BASE64 = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{8,}={0,2}")


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
    "url": "is a URL or an internal host name; describe what is there instead",
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


@dataclass(frozen=True)
class _View:
    """A form of one text the checks read: ``text`` for the patterns, ``folded`` for the literal values, and for
    each character of ``text`` (and of ``folded``) the characters ``start`` to ``end`` of the field it came from."""

    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]
    folded: str
    folded_starts: tuple[int, ...]
    folded_ends: tuple[int, ...]

    def span(self, start: int, end: int, *, folded: bool = False) -> tuple[int, int]:
        starts, ends = (self.folded_starts, self.folded_ends) if folded else (self.starts, self.ends)
        return starts[start], ends[end - 1]


def _normalized(pieces: Iterable[tuple[str, int, int]]) -> _View:
    """``pieces`` (characters with the field span each came from) as NFKC without format characters and with spaces
    collapsed, and that case-folded, each character with its span."""
    text, starts, ends = [], [], []
    folded, folded_starts, folded_ends = [], [], []
    for char, start, end in pieces:
        if unicodedata.category(char) == "Cf":
            continue  # zero-width and other format characters
        if char.isspace():
            if text and text[-1] == " ":
                ends[-1], folded_ends[-1] = end, end
                continue
            char = " "
        for plain in unicodedata.normalize("NFKC", char):
            text.append(plain), starts.append(start), ends.append(end)
            for low in plain.casefold():
                folded.append(low), folded_starts.append(start), folded_ends.append(end)
    return _View("".join(text), tuple(starts), tuple(ends), "".join(folded), tuple(folded_starts), tuple(folded_ends))


def _unescaped(text: str) -> list[tuple[str, int, int]]:
    """``text`` with its escapes decoded, each character with the span it came from."""
    pieces, at = [], 0
    for found in _ESCAPE.finditer(text):
        pieces += [(char, at + i, at + i + 1) for i, char in enumerate(text[at:found.start()])]
        hexa = next((g for g in found.groups()[:5] if g is not None), None)
        code = int(hexa, 16) if hexa is not None else int(found.group(6))
        if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF:
            pieces.append((chr(code), found.start(), found.end()))
        at = found.end()
    return pieces + [(char, at + i, at + i + 1) for i, char in enumerate(text[at:])]


def _decoded_runs(text: str) -> list[_View]:
    """Each run of Base64 in ``text`` that decodes to text, as a view whose every character maps to the whole run."""
    views = []
    for found in list(_BASE64.finditer(text))[:_BASE64_RUNS_MAX]:
        run = found.group(0)
        for decode in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                plain = decode(run + "=" * (-len(run) % 4)).decode("utf-8")
            except (binascii.Error, ValueError):
                continue
            if plain and all(char.isprintable() or char.isspace() for char in plain):
                views.append(_normalized((char, found.start(), found.end()) for char in plain))
                break
    return views


def _views(text: str) -> list[_View]:
    """The views of ``text`` the checks read (the module's notes); the first is the text as written."""
    plain = _View(text, tuple(range(len(text))), tuple(range(1, len(text) + 1)), text, tuple(range(len(text))),
                  tuple(range(1, len(text) + 1)))
    views = [plain, _normalized((char, i, i + 1) for i, char in enumerate(text))]
    if _ESCAPE.search(text):
        views.append(_normalized(_unescaped(text)))
    return views + _decoded_runs(text)


def _found(pattern: re.Pattern[str], views: Sequence[_View]) -> list[tuple[int, int]]:
    return [view.span(m.start(), m.end()) for view in views for m in pattern.finditer(view.text) if m.end() > m.start()]


def _ip6(views: Sequence[_View]) -> list[tuple[int, int]]:
    spans = []
    for view in views:
        for m in _IP6.finditer(view.text):
            try:
                ipaddress.IPv6Address(m.group(0))
            except ValueError:
                continue
            spans.append(view.span(m.start(), m.end()))
    return spans


def _literal(needles: Iterable[str], views: Sequence[_View]) -> list[tuple[int, int]]:
    """Where any of ``needles`` is written in a view, case-folded and normalized as the views are; a needle of any
    length (a two-character name is still a name)."""
    spans = []
    for needle in needles:
        probe = _normalized((char, 0, 0) for char in needle.strip()).folded.strip()
        if not probe:
            continue
        for view in views:
            start = view.folded.find(probe)
            while start != -1:
                spans.append(view.span(start, start + len(probe), folded=True))
                start = view.folded.find(probe, start + 1)
    return spans


def _domains(domains: Iterable[str], views: Sequence[_View]) -> list[tuple[int, int]]:
    """Where an internal domain, or a host under one, is written."""
    spans = []
    for domain in domains:
        name = domain.strip().strip(".").lower()
        if not name:
            continue
        pattern = re.compile(rf"(?<![\w.-])(?:[\w-]+\.)*{re.escape(name)}(?![\w-])", re.IGNORECASE)
        spans += [view.span(m.start(), m.end()) for view in views for m in pattern.finditer(view.text)]
    return spans


def _windows(text: str) -> list[str]:
    flat = " ".join(text.split()).lower()
    return [flat[i:i + SHINGLE] for i in range(0, max(0, len(flat) - SHINGLE + 1))]


def lint(request: AdviceRequest, *, bindings: Sequence[Binding] = (), material: str = "",
         local_paths: Sequence[str] = (), ids: Sequence[str] = (), domains: Sequence[str] = (),
         secrets: Sequence[str] | None = None) -> list[Hit]:
    """Every hit of the code checks in ``request``: ``bindings`` its aliases, ``material`` the local material it was
    written from, ``local_paths`` directories of this machine, ``ids`` the task's and its runs' ids, ``domains`` the
    project's internal domains, ``secrets`` the values registered as secrets (``log_redaction.registered()`` when
    None)."""
    from .log_redaction import registered

    secret_values = registered() if secrets is None else tuple(secrets)
    shown = " ".join(a.get("content", "") for a in request.fields.get("artifacts", ()))
    hits: list[Hit] = []
    for field, text in request.strings():
        taken: list[tuple[int, int]] = []

        def add(category: str, spans: Iterable[tuple[int, int]]) -> None:
            for start, end in sorted(set(spans)):
                if not any(s < end and start < e for s, e in taken):  # one category per span
                    taken.append((start, end))
                    hits.append(Hit(category, field, start, end))

        views = _views(text)
        add("secret", _found(_SECRETS, views) + _literal(secret_values, views))
        add("binding", _literal((b.refers_to for b in bindings), views))
        add("local_path", _found(_PATHS, views) + _literal(local_paths, views))
        add("url", _found(_URLS, views) + _domains(domains, views))
        add("identity", _found(_EMAIL, views) + _found(_IP, views) + _ip6(views) + _found(_UUID, views)
            + _found(_HEX_ID, views) + _literal(ids, views))
        log_spans = _found(_TIMESTAMP, views) + _found(_STACK, views)
        if field.startswith("artifacts[") and field.endswith(".content") and text.count("\n") >= LOG_LINES_MAX:
            log_spans.append((0, len(text)))
        add("raw_log", log_spans)
        dangling = _found(_DANGLING, views[:1])
        if not field.startswith("artifacts["):
            dangling += [(m.start(), m.end()) for m in _FILE_NAME.finditer(text) if m.group(0) not in shown]
        add("dangling", dangling)
    if material.strip():
        # Every window of the request counts, each time it occurs; only the material's are a set to look them up in.
        written, known = _windows(" ".join(text for _, text in request.strings())), set(_windows(material))
        if written and sum(window in known for window in written) / len(written) > VERBATIM_MAX:
            hits.append(Hit("verbatim", "*", 0, 0))
    return hits
