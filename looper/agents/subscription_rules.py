"""A subscription's selection rules (message-stream RFC, section 7): what a window keeps, and what that says about it.

A subscription without rules selects with its one filter (``subscriptions`` columns), as before. With ``rules`` an
event is kept when any rule takes it, the first rule that does naming it (its ``name`` for counting, its ``label`` and
the chat's area for the tag ``lines_v2`` writes). A rule takes an event when every condition it names holds: chat
kind, the chat listed (or not excluded), sender kind, not sent by the source's own user (``exclude_self``), whether it
@-mentions the user (``mentions_me``), and the text matching ``text_match``.

``text_match`` is operator data (a regular expression kept in the local database). Python's ``re`` has no time limit, so
a pattern is bounded where it is saved (length, no repeated group inside a repeated group) and the text it runs on is
cut to ``MATCH_TEXT_CHARS``.

A rule that can select private content (a direct chat, ``mentions_me`` only, or a ``never_quiet`` fallback that
writes the messages themselves) makes the subscription private: it may push only to the operator's own direct chat.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any

MAX_RULES = 20
NAME_MAX_CHARS = 40
MATCH_MAX_CHARS = 500
MATCH_TEXT_CHARS = 1000
REGEX_TIMEOUT_SECONDS = 5.0  # what the matching of one window may take: the process doing it is killed after
MAX_UNBOUNDED = 2  # repeats that can run long in one pattern: with these, a match takes at most quadratic time
MAX_BOUNDED_REPEAT = 10  # a bounded repeat with a larger maximum counts as unbounded
MAX_VARIABLE = 8
MAX_BRANCHES = 8
MAX_AREAS = 200
CHAT_KINDS = ("p2p", "group", "topic", "unknown")
SENDER_KINDS = ("user", "app")
MENTIONS = ("any", "only", "exclude")
FALLBACK_PER_RULE = 30
FALLBACK_TEXT_CHARS = 200

_KEYS = ("name", "label", "chat_kinds", "include_chats", "exclude_chats", "sender_kinds", "exclude_self",
         "mentions_me", "text_match", "never_quiet", "areas")
def _walk(tree: Any, found: dict[str, int], inside_repeat: bool) -> str | None:
    """Count the repeats and branches of a parsed pattern and say why it is unsafe (None if it is not)."""
    from re import _constants as const  # type: ignore[attr-defined]

    repeats = (const.MAX_REPEAT, const.MIN_REPEAT, getattr(const, "POSSESSIVE_REPEAT", const.MAX_REPEAT))
    for op, args in tree:
        if op in repeats:
            low, high, body = args
            if inside_repeat:
                return "text_match repeats a group that already repeats or branches (such as (a+)+ or (a|aa)+): rewrite it"
            found["variable"] += low != high
            found["unbounded"] += high == const.MAXREPEAT or high > MAX_BOUNDED_REPEAT
            if (why := _walk(body, found, True)) is not None:
                return why
        elif op == const.BRANCH:
            if inside_repeat:
                return "text_match repeats a group that already repeats or branches (such as (a+)+ or (a|aa)+): rewrite it"
            found["branches"] += 1
            for alternative in args[1]:
                if (why := _walk(alternative, found, inside_repeat)) is not None:
                    return why
        elif op == const.SUBPATTERN:
            if (why := _walk(args[-1], found, inside_repeat)) is not None:
                return why
        elif op in (const.ASSERT, const.ASSERT_NOT):
            if (why := _walk(args[1], found, inside_repeat)) is not None:
                return why
        elif op == getattr(const, "ATOMIC_GROUP", None):
            if (why := _walk(args, found, inside_repeat)) is not None:
                return why
        elif op == const.GROUPREF_EXISTS:
            for branch in args[1:]:
                if branch is not None and (why := _walk(branch, found, inside_repeat)) is not None:
                    return why
    return None


def unsafe(pattern: str) -> str | None:
    """Why a pattern may backtrack without bound, or None. ``re`` cannot be interrupted in-process, so the shapes that
    make it take exponential time are refused where the pattern is saved, on the pattern Python itself parsed (so
    ``{,n}``, ``(?x)`` and comments are what they are, not what they look like): a repeat whose body holds a repeat or a
    branch, more than ``MAX_UNBOUNDED`` repeats that can run long (unbounded, or with a maximum over
    ``MAX_BOUNDED_REPEAT``), more than ``MAX_VARIABLE`` repeats of any kind (``a?a?a?...a{30}``), and more than
    ``MAX_BRANCHES`` alternations. Matching is also done in a process that is killed at a deadline (``Matcher``)."""
    from re import _parser  # type: ignore[attr-defined]

    try:
        tree = _parser.parse(pattern, re.IGNORECASE)
    except (re.error, RecursionError, OverflowError) as exc:
        return f"text_match is not a valid pattern: {exc}"
    found = {"variable": 0, "unbounded": 0, "branches": 0}
    if (why := _walk(tree, found, False)) is not None:
        return why
    if found["unbounded"] > MAX_UNBOUNDED:
        return (f"text_match has more than {MAX_UNBOUNDED} repeats that can run long (+, *, {{n,}} or a maximum over "
                f"{MAX_BOUNDED_REPEAT}): use fewer")
    if found["variable"] > MAX_VARIABLE:
        return f"text_match has more than {MAX_VARIABLE} repeats (including ?): use fewer"
    if found["branches"] > MAX_BRANCHES:
        return f"text_match has more than {MAX_BRANCHES} alternations: use fewer"
    return None


_PLACEHOLDER = re.compile(r"\{count(?::([^{}]{1,%d}))?\}" % NAME_MAX_CHARS)


def _text(field: str, raw: Any, limit: int) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > limit or re.search(r"[{}\x00]", raw):
        raise ValueError(f"{field} must be 1 to {limit} characters without braces")
    return raw.strip()


def _kinds(field: str, raw: Any, allowed: Sequence[str]) -> list[str]:
    if not isinstance(raw, list) or not raw or any(item not in allowed for item in raw):
        raise ValueError(f"{field} must be a nonempty list of {', '.join(allowed)}")
    return list(dict.fromkeys(raw))


def _chats(field: str, raw: Any) -> list[str]:
    if not isinstance(raw, list) or len(raw) > MAX_AREAS or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200 for item in raw):
        raise ValueError(f"{field} must be a list of at most {MAX_AREAS} chat ids")
    return list(dict.fromkeys(item.strip() for item in raw))


def check_pattern(pattern: Any) -> str:
    """A ``text_match`` pattern that is safe to save; ``ValueError`` saying why not."""
    if not isinstance(pattern, str) or not pattern or len(pattern) > MATCH_MAX_CHARS or "\x00" in pattern:
        raise ValueError(f"text_match must be a pattern of 1 to {MATCH_MAX_CHARS} characters")
    if (why := unsafe(pattern)) is not None:
        raise ValueError(why)
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"text_match is not a valid pattern: {exc}") from None
    return pattern


def parse_rule(raw: Any, index: int) -> dict[str, Any]:
    where = f"rules[{index}]"
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be an object")
    if unknown := [key for key in raw if key not in _KEYS]:
        raise ValueError(f"{where}: unknown field {unknown[0]!r}; a rule has {', '.join(_KEYS)}")
    if "name" not in raw:
        raise ValueError(f"{where} needs a name")
    try:
        rule: dict[str, Any] = {"name": _text("name", raw["name"], NAME_MAX_CHARS)}
        rule["label"] = _text("label", raw["label"], NAME_MAX_CHARS) if raw.get("label") else ""
        rule["chat_kinds"] = _kinds("chat_kinds", raw.get("chat_kinds", list(CHAT_KINDS)), CHAT_KINDS)
        rule["include_chats"] = _chats("include_chats", raw.get("include_chats", []))
        rule["exclude_chats"] = _chats("exclude_chats", raw.get("exclude_chats", []))
        rule["sender_kinds"] = _kinds("sender_kinds", raw.get("sender_kinds", list(SENDER_KINDS)), SENDER_KINDS)
        for flag in ("exclude_self", "never_quiet"):
            if not isinstance(raw.get(flag, False), bool):
                raise ValueError(f"{flag} must be true or false")
            rule[flag] = raw.get(flag, False)
        if raw.get("mentions_me", "any") not in MENTIONS:
            raise ValueError(f"mentions_me must be one of {', '.join(MENTIONS)}")
        rule["mentions_me"] = raw.get("mentions_me", "any")
        rule["text_match"] = check_pattern(raw["text_match"]) if raw.get("text_match") else ""
        areas = raw.get("areas", {})
        if not isinstance(areas, Mapping) or len(areas) > MAX_AREAS:
            raise ValueError(f"areas must map at most {MAX_AREAS} chat ids to a name")
        rule["areas"] = {_chats("areas", [chat])[0]: _text("an area", area, NAME_MAX_CHARS) for chat, area in areas.items()}
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from None
    return rule


def parse_rules(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > MAX_RULES:
        raise ValueError(f"rules must be a list of at most {MAX_RULES} rules (an empty list: the subscription's own filter)")
    return [parse_rule(rule, index) for index, rule in enumerate(raw)]


def check_header(header: str) -> str:
    """A header's only braces are ``{count}`` and ``{count:RULE NAME}``."""
    if re.search(r"[{}]", _PLACEHOLDER.sub("", header)):
        raise ValueError("output_header may hold {count} and {count:RULE NAME} only: no other braces")
    return header


# A chat the directory does not list yet (a new direct chat) is stored as ``unknown``: it may be one.
PRIVATE_KINDS = ("p2p", "unknown")


def is_private(sub: Mapping[str, Any]) -> bool:
    """Whether the subscription can select what only its operator should read: direct chats, messages that @-mention
    them, or a never_quiet fallback of the messages themselves."""
    rules = sub.get("rules") or []
    if not rules:
        return any(kind in PRIVATE_KINDS for kind in sub.get("chat_kinds") or ())
    return any(any(kind in PRIVATE_KINDS for kind in rule["chat_kinds"]) or rule["mentions_me"] == "only"
               or rule["never_quiet"] for rule in rules)


PRIVATE_NOTE = "本订阅包含私聊、@我的消息或原文兜底清单（私密内容），只能推送到你自己的私聊"


_WORKER = r'''
import json, re, sys
job = json.load(sys.stdin)
out = []
for pattern in job["patterns"]:
    compiled = re.compile(pattern, re.IGNORECASE)
    out.append([compiled.search(text) is not None for text in job["texts"]])
json.dump(out, sys.stdout)
'''


class Matcher:
    """The rules of one run, compiled; ``prepare`` runs every ``text_match`` over a window's rows in a process that
    is killed at ``REGEX_TIMEOUT_SECONDS`` (a regular expression cannot be interrupted in this one); ``__call__`` then
    gives the index of the first rule that takes an event row (its ``seq``, ``chat_kind``, ``sender_kind``,
    ``sender_id``, ``chat_id``, ``mentions_me`` and ``text``), or None. Past the deadline (``timed_out``) every
    pattern takes nothing."""

    def __init__(self, rules: Sequence[Mapping[str, Any]], *, me: str | None) -> None:
        self.rules = list(rules)
        self._me = me
        self._hits: dict[int, set[Any]] = {}
        self.timed_out = False

    async def prepare(self, rows: Sequence[Mapping[str, Any]]) -> None:
        indexes = [i for i, rule in enumerate(self.rules) if rule["text_match"]]
        if not indexes or not rows:
            return
        job = {"patterns": [self.rules[i]["text_match"] for i in indexes],
               "texts": [(row["text"] or "")[:MATCH_TEXT_CHARS] for row in rows]}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", _WORKER, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(job).encode()),
                                               timeout=REGEX_TIMEOUT_SECONDS)
            answers = json.loads(stdout)
        except (asyncio.TimeoutError, ValueError, OSError):
            self.timed_out = True
            return
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        for index, found in zip(indexes, answers):
            self._hits[index] = {row["seq"] for row, hit in zip(rows, found) if hit}

    def __call__(self, row: Mapping[str, Any]) -> int | None:
        for index, rule in enumerate(self.rules):
            if row["chat_kind"] not in rule["chat_kinds"] or row["sender_kind"] not in rule["sender_kinds"]:
                continue
            if rule["include_chats"] and row["chat_id"] not in rule["include_chats"] or row["chat_id"] in rule["exclude_chats"]:
                continue
            if rule["exclude_self"] and self._me and row["sender_id"] == self._me:
                continue
            if (rule["mentions_me"] == "only") != bool(row["mentions_me"]) and rule["mentions_me"] != "any":
                continue
            if rule["text_match"] and row["seq"] not in self._hits.get(index, ()):
                continue
            return index
        return None

    def tag(self, row: Mapping[str, Any]) -> str:
        """``【label·area】`` (or ``【label】``, or nothing) for a selected row."""
        rule = self.rules[row["rule"]]
        area = rule["areas"].get(row["chat_id"], "")
        label = rule["label"] or rule["name"]
        return f"【{label}·{area}】" if area else f"【{label}】"


def counts(rows: Sequence[Mapping[str, Any]], rules: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many selected rows each rule name took (rules sharing a name add up)."""
    found: dict[str, int] = {}
    for row in rows:
        name = rules[row["rule"]]["name"]
        found[name] = found.get(name, 0) + 1
    return found


def format_header(header: str, found: Mapping[str, int], total: int) -> str:
    return _PLACEHOLDER.sub(lambda m: str(total if m.group(1) is None else found.get(m.group(1), 0)), header)


def fallback_text(rows: Sequence[Mapping[str, Any]], rules: Sequence[Mapping[str, Any]]) -> str:
    """The messages of the rules that must never be quiet, as a list written by code, one block per label: what a
    reader gets when the processor said there was nothing to say but something is directed at them."""
    blocks: dict[str, list[str]] = {}
    for row in rows:
        rule = rules[row["rule"]]
        if not rule["never_quiet"]:
            continue
        label = rule["label"] or rule["name"]
        blocks.setdefault(label, [])
        if len(blocks[label]) < FALLBACK_PER_RULE:
            text = " ".join((row["text"] or "").split())[:FALLBACK_TEXT_CHARS]
            area = rule["areas"].get(row["chat_id"], "")
            blocks[label].append(f"- **{row.get('chat_name') or row['chat_id']}{f'（{area}）' if area else ''}**：{text}")
    return "\n\n".join(f"{label}\n" + "\n".join(lines) for label, lines in blocks.items())
