"""Matching a search's words against stored text in SQL, for Chinese and English alike (assistant search RFC,
section 3).

PostgreSQL's text search parser keeps a run of Chinese characters as one word, and no Chinese segmenter ships with
the PostgreSQL image, so a query is split here and nothing is tokenized in the database:

- each distinct word of the query is a term; its tokens are its lower-cased runs of ASCII letters and digits of two
  characters or more, and each overlapping pair of CJK characters (a lone one on its own);
- a text covers a term fully when it holds the term as written, ignoring case (what the word search matched), and
  otherwise by the share of the term's tokens it holds: a CJK pair anywhere, an ASCII token at the start of a word
  (three characters or more as a prefix, two as a whole word);
- a text matches when it covers some term at least ``HIT``; its score is then the sum of its coverage of every term,
  each weighted by how rare the term is among the candidates, ``ln(1 + candidates / holders)`` (holders: the
  candidates covering it at least ``HIT``), and a text scoring under ``RELATIVE_MIN`` of the best one is left out.

``Match`` builds the SQL: a prefilter (the lower-cased text holds a term or one of its tokens) and a CASE
expression per term; every value is a parameter, numbered from where the caller's own end.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

TERMS_MAX = 8
TOKENS_MAX = 16  # per term
HIT = 0.5  # the coverage at which a term counts for a text
RELATIVE_MIN = 0.6  # a match scoring under this share of the best match is left out
PREFIX_MIN = 3  # an ASCII token this long matches the start of a word; a shorter one only a whole word

_CJK = "㐀-䶿一-鿿豈-﫿"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_ASCII_WORD = re.compile(r"[a-z0-9]{2,}")


def tokens(text: str) -> tuple[str, ...]:
    """The tokens of ``text``, each once, in order: its CJK pairs (a lone CJK character on its own), then its
    lower-cased ASCII words of two characters or more."""
    lowered = text.lower()
    found: list[str] = []
    for run in _CJK_RUN.findall(lowered):
        found.extend([run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)])
    found.extend(_ASCII_WORD.findall(_CJK_RUN.sub(" ", lowered)))
    return tuple(dict.fromkeys(found))


@dataclass(frozen=True)
class Term:
    text: str  # lower-cased, as the user wrote it
    tokens: tuple[str, ...]


def terms(words: Sequence[str]) -> tuple[Term, ...]:
    """The terms of a query's words: each distinct word with a token or at least two characters, at most
    ``TERMS_MAX``, each with at most ``TOKENS_MAX`` tokens."""
    found: dict[str, Term] = {}
    for word in words:
        text = word.strip().lower()
        if text in found or len(found) >= TERMS_MAX:
            continue
        parts = tokens(text)[:TOKENS_MAX]
        if parts or len(text) >= 2:
            found[text] = Term(text, parts)
    return tuple(found.values())


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _word_start(token: str) -> str:
    """The regular expression of ``token`` at the start of an ASCII word (a whole word when it is short)."""
    tail = "" if len(token) >= PREFIX_MIN else "([^a-z0-9]|$)"
    return f"(^|[^a-z0-9]){token}{tail}"


class Match:
    """The SQL that matches ``terms`` against a lower-cased text expression, with its values (``params``) numbered
    from ``start``."""

    def __init__(self, found: Sequence[Term], *, start: int) -> None:
        if not found:
            raise ValueError("a search needs at least one term")
        self.terms = tuple(found)
        self.params: list[object] = []
        self._start = start

    def param(self, value: object, cast: str = "") -> str:
        """``value`` as the next parameter, with ``cast``: its placeholder."""
        self.params.append(value)
        return f"${self._start + len(self.params) - 1}{cast}"

    def prefilter(self, doc: str) -> str:
        """True for a text that may match: it holds a term as written or one of its tokens."""
        patterns = list(dict.fromkeys(_like(value) for term in self.terms for value in (term.text, *term.tokens)))
        return f"{doc} LIKE ANY(ARRAY(SELECT lower(p) FROM unnest({self.param(patterns, '::text[]')}) AS p))"

    def coverage(self, doc: str, index: int, *, whole: str | None = None, fuzzy: str | None = None) -> str:
        """How much of term ``index`` the text covers, from 0 to 1. ``whole``: an SQL condition that also covers it
        fully (a name it concerns); ``fuzzy``: an SQL expression of a partial coverage to take when larger."""
        term = self.terms[index]
        # The term lower-cased as the database does it, as ``doc`` is.
        full = f"strpos({doc}, lower({self.param(term.text, '::text')})) > 0" + (f" OR {whole}" if whole else "")
        if term.tokens:
            held = " + ".join(
                f"({doc} ~ {self.param(_word_start(token), '::text')})::int" if token.isascii()
                else f"(strpos({doc}, {self.param(token, '::text')}) > 0)::int" for token in term.tokens)
            partial = f"({held})::float8 / {len(term.tokens)}"
        else:
            partial = "0::float8"
        if fuzzy:
            partial = f"greatest({partial}, {fuzzy})"
        return f"CASE WHEN {full} THEN 1::float8 ELSE {partial} END"

    def weights(self, columns: Sequence[str]) -> str:
        """The window columns ``d<i>`` (how many candidates each term counts for) and ``n`` (how many there are),
        over ``columns``, the coverage of each term, in order."""
        held = ", ".join(f"count(*) FILTER (WHERE {column} >= {HIT}) OVER () AS d{i}"
                         for i, column in enumerate(columns))
        return f"count(*) OVER () AS n, {held}"

    @staticmethod
    def score(columns: Sequence[str]) -> str:
        """A candidate's score from the coverage ``columns`` and the ``weights`` columns: 0 unless it covers some
        term at least ``HIT``, else every term's coverage, weighted. Keep the rows whose score is at least
        ``RELATIVE_MIN`` of the best one's (``max(score) OVER ()`` of those above 0)."""
        counted = " OR ".join(f"{column} >= {HIT}" for column in columns)
        weighted = " + ".join(f"{column} * ln(1 + n::float8 / greatest(d{i}, 1))" for i, column in enumerate(columns))
        return f"CASE WHEN {counted} THEN {weighted} ELSE 0 END"
