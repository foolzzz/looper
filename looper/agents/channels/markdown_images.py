"""Agent-authored Markdown must not make a chat platform fetch a remote image.

Agent text can carry data an agent read, so ``![](https://host/?d=...)`` is an outbound request the moment a client
or platform renders it. ``neutralize_images`` therefore does not try to recognise every URL shape; it removes the
ability to render an image from anything that is not provably safe:

* An image construct is ``![label]`` followed by ``(``, ``[`` or ``[]``, or a bare ``![label]`` whose label has a
  link reference definition in the text. It is kept only as ``![alt](target)`` with ``target`` exactly a Lark image
  key. A plain ``http(s)`` target becomes ``image: [alt (host)](url)`` (a link, fetched only on a click); every
  other construct (reference-style, nested brackets or escapes in the label, angle-bracket, titled or spaced
  targets, protocol-relative or entity-encoded URLs, unterminated or over-long labels) loses its ``!``, written as ``&#33;``, so it stays
  text or a plain link and the brackets around it keep their structure. Other ``![`` such as ``vec![1, 2]`` are left alone.
* Every HTML tag opener (``<`` plus a letter, ``/`` and a letter, ``!`` or ``?``) has its ``<`` written as
  ``&#60;``, the escape Lark's card Markdown documents, except Lark's own tags (``at``, ``a``, ``br``, ``hr``,
  ``person``, ``local_datetime``, ``text_tag``, ``number_tag``, ``raw``, ``font``, ``u``) and ``<http(s)://...>`` or
  ``<mailto:...>`` autolinks. Lark documents that its ``md`` tag does not support arbitrary raw HTML and does not say
  what the card Markdown component does with tags it does not list, so no unlisted tag is sent as a tag. Lark's
  ``<link>`` and ``<audio>`` are left out of the allowlist on purpose: they are also resource-loading HTML.
* Code is preserved: top-level fenced blocks (a backtick fence info string has no backticks) and inline code
  spans. A span never crosses a newline, which also covers the block boundaries (headings, quotes, list items,
  reference definitions) that end a CommonMark paragraph. Indented code and fences indented 4+ spaces are not
  recognised, so images in them are neutralized (a code sample there may show ``&#33;[`` instead of ``![``).
  Neutralizing is the safe direction when this parser and the renderer disagree about what is code.

The scan is a single pass with every inner search bounded, so the cost is linear in the input and the function is
safe to call on the event loop.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_KEY = re.compile(r"img_[A-Za-z0-9_-]{1,120}")
_URL = re.compile(r"https?://[^\s<>()\"'\\\[\]`]{1,1800}", re.I)
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_RUN = re.compile(r"`+")
_BRACKET = re.compile(r"[\[\]\\]")
_DEFINITION = re.compile(r"[ \t>]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)*\[((?:[^\[\]\\]|\\.){1,999})\]:")
LARK_TAGS = ("at", "a", "br", "hr", "person", "local_datetime", "text_tag", "number_tag", "raw", "font", "u")
_TAG = re.compile(
    r"<(?=[A-Za-z]|/[A-Za-z]|[!?])(?!/?(?:" + "|".join(LARK_TAGS) + r")(?![A-Za-z0-9_-]))"
    r"(?!(?:https?://|mailto:)[^\s<>]*>)", re.I)
_ALT_MAX = 300
_DEST_MAX = 2000
_NESTING_STEPS = 16
_NEUTRAL = "&#33;["


def _normalize(label: str) -> str:
    return " ".join(label.split()).casefold()


def _close(text: str, i: int) -> int:
    """Index of the ``]`` that closes the ``![`` at ``i`` within the label window, or -1."""
    end = i + 2 + _ALT_MAX
    j = text.find("]", i + 2, end)
    if j == -1:
        return -1
    if text.find("[", i + 2, j) == -1 and text.find("\\", i + 2, j) == -1:
        return j
    depth, skip, steps = 1, -1, 0
    for match in _BRACKET.finditer(text, i + 2, end):
        steps += 1
        if steps > _NESTING_STEPS:
            return -1
        if match.start() < skip:
            continue
        char = match[0]
        if char == "\\":
            skip = match.end() + 1
        elif char == "[":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return match.start()
    return -1


def _images(text: str, definitions: frozenset[str]) -> str:
    """Neutralize the image constructs in prose ``text``; keep ``![alt](image_key)``; escape tag openers."""
    out: list[str] = []
    position = 0
    i = text.find("![")
    while i != -1:
        close = _close(text, i)
        piece_end = i + 2
        replacement = _NEUTRAL
        if close != -1:
            alt = text[i + 2:close]
            following = text[close + 1:close + 2]
            if following == "(":
                end = text.find(")", close + 2, close + 2 + _DEST_MAX)
                dest = text[close + 2:end] if end != -1 else ""
                clean = not any(c in alt for c in "[]\\\n")
                if clean and _KEY.fullmatch(dest):
                    replacement, piece_end = text[i:end + 1], end + 1
                elif clean and _URL.fullmatch(dest) and urlsplit(dest).hostname:
                    host = urlsplit(dest).hostname
                    replacement, piece_end = f"image: [{alt.strip() + ' ' if alt.strip() else ''}({host})]({dest})", end + 1
            elif following != "[" and _normalize(alt) not in definitions:
                replacement = "!["  # not an image: no target, no reference, no definition (``vec![1, 2]``)
        out.append(text[position:i])
        out.append(replacement)
        position = piece_end
        i = text.find("![", position)
    out.append(text[position:])
    return _TAG.sub("&#60;", "".join(out))


def _spans(line: str) -> list[tuple[int, int]]:
    """Inline code spans (opening run start, closing run end) in one line: equal-length backtick runs pair up
    in order; a backslash before a run escapes its first backtick."""
    runs: list[tuple[int, int]] = []
    for match in _RUN.finditer(line):
        start, end = match.span()
        slashes = 0
        while start - slashes - 1 >= 0 and line[start - slashes - 1] == "\\":
            slashes += 1
        if slashes % 2:
            start += 1
        if end > start:
            runs.append((start, end))
    following: dict[int, int] = {}
    next_same: list[int | None] = [None] * len(runs)
    for index in range(len(runs) - 1, -1, -1):
        length = runs[index][1] - runs[index][0]
        next_same[index] = following.get(length)
        following[length] = index
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(runs):
        close = next_same[index]
        if close is None:
            index += 1
        else:
            spans.append((runs[index][0], runs[close][1]))
            index = close + 1
    return spans


def _line(line: str, definitions: frozenset[str]) -> str:
    out: list[str] = []
    position = 0
    for start, end in _spans(line):
        out.append(_images(line[position:start], definitions))
        out.append(line[start:end])
        position = end
    out.append(_images(line[position:], definitions))
    return "".join(out)


def neutralize_images(markdown: str) -> str:
    if "![" not in markdown and "<" not in markdown:
        return markdown
    lines = markdown.splitlines(keepends=True)
    definitions = frozenset(_normalize(m[1]) for m in map(_DEFINITION.match, lines) if m)
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        match = _FENCE.fullmatch(line.rstrip("\r\n"))
        if fence is not None:
            out.append(line)
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = None
        elif match and not (match[1][0] == "`" and "`" in match[2]):
            fence = match[1]
            out.append(line)
        else:
            out.append(_line(line, definitions))
    return "".join(out)
