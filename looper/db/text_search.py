"""Matching a search's words against stored text in SQL, for Chinese and English alike (assistant search RFC,
section 3).

PostgreSQL's text search parser keeps a run of Chinese characters as one word, and no Chinese segmenter ships with
the PostgreSQL image, so the database's own ``looper_search_tokens(text)`` (schema.sql) splits a text into
**tokens** from its first 8192 characters: its lower-cased runs of ASCII letters and digits of two characters or more, and each overlapping pair of
CJK characters (a lone one on its own). ASCII tokens are capped at 512 bytes, safely below PostgreSQL's lexeme
limit; the stored text is unchanged. A table searched this way has a GIN index on
``array_to_tsvector(looper_search_tokens(<its text>))``, and the query's words are split by the same function, so
the two always agree:

- each distinct word of the query is a **term**, with its tokens;
- the **candidates** are the rows the index finds holding a token of a term (``candidates``: an ASCII token of three
  characters or more as a prefix, ``'deploy':*``);
- a candidate covers a term fully when it holds the term as written, ignoring case, and otherwise by the share of
  the term's tokens it holds: a CJK pair anywhere, an ASCII token at the start of a word (three characters or more
  as a prefix, two as a whole word);
- a candidate matches when it covers some term at least ``HIT``; its score is the sum of its coverage of every term,
  each weighted by how rare the term is among the candidates, ``ln(1 + candidates / holders)`` (holders: the
  candidates covering it at least ``HIT``), and a match scoring under ``RELATIVE_MIN`` of the best one is left out.

``Match`` builds the SQL; every value is a parameter, numbered from where the caller's own end.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

TERMS_MAX = 8
TOKENS_MAX = 16  # per term
HIT = 0.5  # the coverage at which a term counts for a text
RELATIVE_MIN = 0.6  # a match scoring under this share of the best match is left out
PREFIX_MIN = 3  # an ASCII token this long matches the start of a word; a shorter one only a whole word


@dataclass(frozen=True)
class Term:
    text: str  # lower-cased as the database does it, as the user wrote it
    tokens: tuple[str, ...]


async def terms(words: Sequence[str], db: Any) -> tuple[Term, ...]:
    """The terms of a query's words, split by the database's ``looper_search_tokens``: each distinct word with a
    token, at most ``TERMS_MAX``, each with at most ``TOKENS_MAX`` tokens."""
    wanted = list(dict.fromkeys(word.strip() for word in words if word.strip()))
    if not wanted:
        return ()
    rows = await db.fetch("SELECT w.ord, lower(w.word) AS text, looper_search_tokens(w.word) AS tokens "
                          "FROM unnest($1::text[]) WITH ORDINALITY AS w(word, ord) ORDER BY w.ord", wanted)
    found: dict[str, Term] = {}
    for row in rows:
        if row["tokens"] and row["text"] not in found and len(found) < TERMS_MAX:
            found[row["text"]] = Term(row["text"], tuple(row["tokens"][:TOKENS_MAX]))
    return tuple(found.values())


async def without_jit(db: Any, sql: str, *args: Any) -> list[Any]:
    """``sql``'s rows, with JIT off for it: the tokenizer's high cost makes the planner's estimate large enough to
    JIT-compile a query that the index answers in milliseconds, and the compiling takes a fifth of a second."""
    if hasattr(db, "acquire"):
        async with db.acquire() as conn:
            return await without_jit(conn, sql, *args)
    async with db.transaction():
        await db.execute("SET LOCAL jit = off")
        return await db.fetch(sql, *args)


def tsquery(term: Term) -> str:
    """The text search query of a row holding any of ``term``'s tokens (a long ASCII one as a prefix)."""
    return " | ".join(f"'{token}':*" if token.isascii() and len(token) >= PREFIX_MIN else f"'{token}'"
                      for token in term.tokens)


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

    def holds(self, vector: str, index: int) -> str:
        """True for a row whose token vector (``array_to_tsvector(looper_search_tokens(...))``, as its index has it)
        holds a token of term ``index``: what the index answers."""
        return f"{vector} @@ {self.param(tsquery(self.terms[index]), '::tsquery')}"

    def coverage(self, doc: str, index: int, *, whole: str | None = None, fuzzy: str | None = None) -> str:
        """How much of term ``index`` the lower-cased text ``doc`` covers, from 0 to 1. ``whole``: an SQL condition
        that also covers it fully (a name it concerns); ``fuzzy``: an SQL expression of a partial coverage to take
        when larger."""
        term = self.terms[index]
        full = f"strpos({doc}, {self.param(term.text, '::text')}) > 0" + (f" OR {whole}" if whole else "")
        held = " + ".join(
            f"({doc} ~ {self.param(_word_start(token), '::text')})::int" if token.isascii()
            else f"(strpos({doc}, {self.param(token, '::text')}) > 0)::int" for token in term.tokens)
        partial = f"({held})::float8 / {len(term.tokens)}"
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
