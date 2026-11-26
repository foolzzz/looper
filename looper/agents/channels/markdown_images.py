"""Agent-authored Markdown must not make a chat platform fetch a remote image.

Agent text can carry data an agent read, so ``![](https://host/?d=...)`` is an outbound request the moment a client
or platform renders it. ``neutralize_images`` therefore does not try to recognise every URL shape; it removes the
ability to render an image from anything that is not provably safe:

* An image construct ``![alt](target)`` is kept only when ``target`` is exactly a Lark image key. Anything else
  loses its image marker. A plain ``http(s)`` target is shown as ``image: [alt (host)](url)`` (a link, fetched only
  on a click); every other form (reference-style, nested brackets, angle-bracket or titled targets, protocol-relative
  or entity-encoded URLs, unterminated) becomes ``image: \\[`` plus the original text, so its URL stays visible.
* An HTML tag that loads a resource (``img``, ``picture``, ``source``, ``video``, ``audio``, ``svg``, ``embed``,
  ``object``, ``iframe``, ``link``, ``script``, ``style``, ``input``) gets a zero-width space after its ``<``.
  Other tags, including Lark's ``<at ...>``, are untouched.
* Code is preserved: top-level fenced blocks (a backtick fence info string has no backticks) and inline code
  spans, which cannot cross a blank line and are not opened by an escaped backtick. Indented code and fences
  indented 4+ spaces are not recognised, so images in them are neutralized (a code sample there may show
  ``image: \\[`` instead of ``![``). Fences inside quotes or lists whose backtick runs happen to pair as a span
  are preserved; the rest is neutralized. Neutralizing is the safe direction when this parser and the renderer
  disagree about what is code.

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
_TAG = re.compile(r"<(?=/?(?:img|image|picture|source|video|audio|svg|embed|object|iframe|link|script|style|input)\b)",
                  re.I)
_ALT_MAX = 300
_DEST_MAX = 2000
_NEUTRAL = "image: \\["


def _images(text: str) -> str:
    """Neutralize every ``![`` in prose ``text``; keep ``![alt](image_key)``."""
    out: list[str] = []
    position = 0
    i = text.find("![")
    while i != -1:
        alt_end = text.find("]", i + 2, i + 2 + _ALT_MAX)
        piece_end = i + 2
        replacement = _NEUTRAL
        if alt_end != -1 and text.startswith("(", alt_end + 1) and not any(
                text.find(c, i + 2, alt_end) != -1 for c in "[\\\n"):
            close = text.find(")", alt_end + 2, alt_end + 2 + _DEST_MAX)
            dest = text[alt_end + 2:close] if close != -1 else ""
            if _KEY.fullmatch(dest):
                replacement, piece_end = text[i:close + 1], close + 1
            elif _URL.fullmatch(dest) and urlsplit(dest).hostname:
                alt = text[i + 2:alt_end].strip()
                host = urlsplit(dest).hostname
                replacement, piece_end = f"image: [{alt + ' ' if alt else ''}({host})]({dest})", close + 1
        out.append(text[position:i])
        out.append(replacement)
        position = piece_end
        i = text.find("![", position)
    out.append(text[position:])
    return _TAG.sub("<​", "".join(out))


def _spans(paragraph: str) -> list[tuple[int, int]]:
    """Inline code spans (opening run start, closing run end) in one paragraph: equal-length backtick runs
    pair up in order; a backslash before a run escapes its first backtick."""
    runs: list[tuple[int, int]] = []
    for match in _RUN.finditer(paragraph):
        start, end = match.span()
        slashes = 0
        while start - slashes - 1 >= 0 and paragraph[start - slashes - 1] == "\\":
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


def _paragraph(paragraph: str) -> str:
    out: list[str] = []
    position = 0
    for start, end in _spans(paragraph):
        out.append(_images(paragraph[position:start]))
        out.append(paragraph[start:end])
        position = end
    out.append(_images(paragraph[position:]))
    return "".join(out)


def neutralize_images(markdown: str) -> str:
    if "![" not in markdown and "<" not in markdown:
        return markdown
    out: list[str] = []
    paragraph: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if paragraph:
            out.append(_paragraph("".join(paragraph)))
            paragraph.clear()

    for line in markdown.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        match = _FENCE.fullmatch(body)
        if fence is not None:
            out.append(line)
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = None
        elif match and not (match[1][0] == "`" and "`" in match[2]):
            flush()
            fence = match[1]
            out.append(line)
        elif not body.strip():
            flush()
            out.append(line)
        else:
            paragraph.append(line)
    flush()
    return "".join(out)
