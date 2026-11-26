"""Agent-authored Markdown must not make a chat platform fetch a remote image.

Agent text can carry data an agent read, so ``![](https://host/?d=...)`` is an outbound request the moment a
client or platform renders it. ``remote_images_as_links`` turns each remote image (inline, reference-style, or an
HTML ``<img>``) into a Markdown link that shows the host and fetches nothing until someone clicks it. Images whose
target is not an ``http(s)`` URL (a platform image key) and anything inside code spans or fenced code stay as is.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
_INLINE = re.compile(r"!\[([^\]]*)\]\(\s*<?(https?://[^\s<>)]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)", re.I)
_REFERENCE = re.compile(r"!\[([^\]]*)\](?:\[([^\]]*)\])?")
_DEFINITION = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*<?(https?://[^\s>]+)>?", re.I | re.M)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_SRC = re.compile(r"\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>\"']+))", re.I)
_ALT = re.compile(r"\balt\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>\"']+))", re.I)
_HTTP = re.compile(r"https?://", re.I)


def _link(alt: str, url: str) -> str:
    host = urlsplit(url).hostname or ""
    label = re.sub(r"[\[\]\r\n]+", " ", alt).strip()
    return f"[image: {label} ({host})]({url})" if label else f"[image: ({host})]({url})"


def _first(match: re.Match[str] | None) -> str:
    return next((g for g in match.groups() if g is not None), "") if match else ""


def _rewrite(text: str, definitions: dict[str, str]) -> str:
    text = _INLINE.sub(lambda m: _link(m[1], m[2]), text)

    def reference(m: re.Match[str]) -> str:
        label = m[1] if m[2] is None or m[2] == "" else m[2]
        url = definitions.get(" ".join(label.lower().split()))
        return _link(m[1], url) if url else m[0]

    text = _REFERENCE.sub(reference, text)

    def html(m: re.Match[str]) -> str:
        src = _first(_SRC.search(m[0])).strip()
        return _link(_first(_ALT.search(m[0])), src) if _HTTP.match(src) else m[0]

    return _IMG.sub(html, text)


def remote_images_as_links(markdown: str) -> str:
    if "![" not in markdown and "<img" not in markdown.lower():
        return markdown
    # Split into fenced blocks (kept verbatim) and prose; then prose into code spans (kept) and the rest.
    chunks: list[tuple[bool, str]] = []
    fence: str | None = None
    for line in markdown.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence is None and match:
            fence = match[1]
            chunks.append((True, line))
        elif fence is not None:
            chunks.append((True, line))
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not line.strip().strip(fence[0]):
                fence = None
        elif chunks and not chunks[-1][0]:
            chunks[-1] = (False, chunks[-1][1] + line)
        else:
            chunks.append((False, line))
    prose = "".join(text for code, text in chunks if not code)
    # A definition inside a code span is not a definition: strip spans before collecting.
    definitions = {" ".join(m[1].lower().split()): m[2] for m in _DEFINITION.finditer(_CODE_SPAN.sub("", prose))}
    out: list[str] = []
    for code, text in chunks:
        if code:
            out.append(text)
            continue
        position = 0
        for span in _CODE_SPAN.finditer(text):
            out.append(_rewrite(text[position:span.start()], definitions))
            out.append(span[0])
            position = span.end()
        out.append(_rewrite(text[position:], definitions))
    return "".join(out)
