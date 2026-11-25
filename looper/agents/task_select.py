"""Deterministic task selection: normalize, match and rank. No I/O."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

WINDOW_DAYS = 90
RECENT_DAYS = 14
CANDIDATE_LIMIT = 10
RECENT_LIMIT = 20
TASK_ID = re.compile(r"^task-[0-9a-f]{12}$")

_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("「", "」"), ("“", "”"), ("‘", "’"))
_URL = re.compile(r"https?://[^\s]+")
# The number is 1–7 digits. A longer run fails the trailing boundary, so it is not a shorter prefix.
_SEGMENT = re.compile(r"/(?:pull|merge_requests)/([0-9]{1,7})(?=/|$)")
_SHORTHAND = (
    re.compile(r"(?<!\d)!([0-9]{1,7})(?!\d)"),
    re.compile(r"(?<!\d)#([0-9]{1,7})(?!\d)"),
    re.compile(r"(?<![a-z0-9])mr ([0-9]{1,7})(?!\d)"),
    re.compile(r"(?<![a-z0-9])pr ([0-9]{1,7})(?!\d)"),
)
_MARKED = re.compile(r"(?:!|#)([0-9]{1,7})")
_NAMED = re.compile(r"(?:mr|pr) ([0-9]{1,7})")
_BARE = re.compile(r"([0-9]{1,7})")


def normalize(text: str) -> str:
    """NFKC, lowercase, one space, strip, then at most one surrounding quote pair."""
    folded = unicodedata.normalize("NFKC", text).lower()
    collapsed = re.sub(r"\s+", " ", folded).strip()
    if len(collapsed) >= 2:
        for opening, closing in _QUOTE_PAIRS:
            if collapsed.startswith(opening) and collapsed.endswith(closing):
                return collapsed[len(opening):-len(closing)].strip()
    return collapsed


@dataclass(frozen=True)
class MrRef:
    number: int
    repo: str | None = None

    def __post_init__(self) -> None:
        if self.repo is not None:
            folded = normalize(self.repo)
            object.__setattr__(self, "repo", folded or None)


def _url_ref(folded: str) -> MrRef | None:
    """One http(s) URL whose path contains `/pull/N` or `/merge_requests/N`; otherwise nothing.

    A URL that is not that reference does not fall through to a bare number. A trailing GitLab `/-`
    is not part of the repo (`group/sub/name/-/merge_requests/12` is `group/sub/name`).
    """
    if re.search(r"\s", folded):
        return None
    try:
        parts = urlsplit(folded)
    except ValueError:
        # A stored URL can be syntactically invalid. It is not a reference, and the rest of the text still is.
        return None
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    found = _SEGMENT.search(parts.path)
    if found is None:
        return None
    repo_parts = [piece for piece in parts.path[:found.start()].split("/") if piece]
    if repo_parts and repo_parts[-1] == "-":
        repo_parts.pop()
    return MrRef(int(found.group(1)), "/".join(repo_parts) or None)


def parse_mr(text: str) -> MrRef | None:
    """The whole selection is one reference, or nothing. Bare digits count only here."""
    folded = normalize(text)
    if folded.startswith(("http://", "https://")):
        return _url_ref(folded)
    found = _MARKED.fullmatch(folded) or _NAMED.fullmatch(folded) or _BARE.fullmatch(folded)
    return MrRef(int(found.group(1))) if found is not None else None


def text_refs(text: str) -> set[MrRef]:
    """References inside a longer text. A bare number in prose is not one; a URL contributes its path only."""
    folded = normalize(text)
    spans: list[tuple[int, int]] = []
    found: set[MrRef] = set()
    for match in _URL.finditer(folded):
        spans.append((match.start(), match.end()))
        ref = _url_ref(match.group(0).rstrip(".,);]"))
        if ref is not None:
            found.add(ref)
    for pattern in _SHORTHAND:
        for match in pattern.finditer(folded):
            if any(start <= match.start() and match.end() <= end for start, end in spans):
                continue
            found.add(MrRef(int(match.group(1))))
    return found


def tokens(text: str) -> list[str]:
    """Split on spaces. A CJK run without spaces stays one token; CJK stays attached to Latin without a space."""
    folded = normalize(text)
    return folded.split(" ") if folded else []


def text_match(title: str | None, requirement: str | None, parts: Sequence[str]) -> str | None:
    """Every token is a literal substring of the title (tier ``title``) or of the title or requirement."""
    if not parts:
        return None
    title_text = normalize(title or "")
    requirement_text = normalize(requirement or "")
    if all(part in title_text for part in parts):
        return "title"
    if all(part in title_text or part in requirement_text for part in parts):
        return "requirement"
    return None


def mr_match(selection: MrRef, refs: Iterable[MrRef]) -> str | None:
    """``repo`` when a reference's repo matches too, else ``number``. A different repo does not match."""
    number_only = False
    for ref in refs:
        if ref.number != selection.number:
            continue
        if selection.repo is not None and ref.repo == selection.repo:
            return "repo"
        if selection.repo is None or ref.repo is None:
            number_only = True
    return "number" if number_only else None


@dataclass(frozen=True)
class Candidate:
    task_id: str
    tier: int
    ended: bool
    activity: float


def rank(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Tier, then a task that has not ended, then newer activity, then task id."""
    return sorted(candidates, key=lambda item: (item.tier, int(item.ended), -item.activity, item.task_id))


def prefer_single_open(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Several tasks in one directory collapse to the only one that has not ended."""
    if len(candidates) > 1:
        open_ones = [item for item in candidates if not item.ended]
        if len(open_ones) == 1:
            return open_ones
    return list(candidates)


def present(ranked: Sequence[Candidate], *, limit: int, single: bool) -> tuple[str, list[Candidate], int]:
    """One only when ``single`` and there is exactly one. Otherwise a capped list; the first is not chosen."""
    if not ranked:
        return "none", [], 0
    if single and len(ranked) == 1:
        return "one", [ranked[0]], 0
    shown = list(ranked[:limit])
    return "candidates", shown, len(ranked) - len(shown)


def deepest_paths(cwd: str, paths: Iterable[str]) -> set[str]:
    """Directories that contain ``cwd``, keeping only the ones with the most path components.

    ``commonpath`` so ``/proj`` does not contain ``/proj-other``. Paths are already real.
    """
    cwd_norm = os.path.normpath(cwd)
    matched: list[str] = []
    for path in paths:
        if not path:
            continue
        folder = os.path.normpath(path)
        try:
            if os.path.commonpath((folder, cwd_norm)) == folder:
                matched.append(folder)
        except ValueError:
            continue
    if not matched:
        return set()
    depth = max(len(Path(folder).parts) for folder in matched)
    return {folder for folder in matched if len(Path(folder).parts) == depth}
