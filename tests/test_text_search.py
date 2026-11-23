"""The search's tokens and terms (``db.text_search``, the database's ``looper_search_tokens``): Chinese as overlapping
pairs, English as words, both lower-cased; the index query of a term; and the SQL it builds, every value a parameter.

The tokens' tests run only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from looper.db import text_search
from looper.db.text_search import Match, Term, terms, tsquery
from tests.db_support import DSN, run_db

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@needs_db
def test_chinese_becomes_overlapping_pairs_and_english_words_lower_cased(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        tokens = await pool.fetchval("SELECT looper_search_tokens($1)", "张三决定下周五发布Looper新版本，v2.15 王 a")
        assert sorted(tokens) == sorted(["张三", "三决", "决定", "定下", "下周", "周五", "五发", "发布", "新版", "版本",
                                         "王", "looper", "v2", "15"])
        # The index's tokens and a query's are the same function's.
        assert await pool.fetchval("SELECT array_to_tsvector(looper_search_tokens('We DEPLOYED 下周五')) @@ $1::tsquery",
                                   tsquery(Term("deploy 周五", ("deploy", "周五"))))

    run_db(test, monkeypatch)


@needs_db
def test_a_term_needs_a_token_and_a_query_has_at_most_eight_of_sixteen_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        assert await terms(["A", "c++", "C++", "发布", "", "  ", "发布"], pool) == (Term("发布", ("发布",)),)
        found = await terms([f"word{i}" for i in range(12)] + ["甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"], pool)
        assert len(found) == text_search.TERMS_MAX == 8
        (long,) = await terms(["甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"], pool)
        assert len(long.tokens) == text_search.TOKENS_MAX == 16

    run_db(test, monkeypatch)


def test_a_long_ascii_token_is_a_prefix_in_the_index_query() -> None:
    assert tsquery(Term("deploy v2 发布", ("deploy", "v2", "发布"))) == "'deploy':* | 'v2' | '发布'"


def test_the_sql_carries_every_value_as_a_parameter_numbered_after_the_callers() -> None:
    match = Match((Term("50%_off", ("50", "off")), Term("部署方案", ("部署", "署方", "方案")),
                   Term("deploy", ("deploy",)), Term("v2", ("v2",))), start=4)
    sql = " ".join([match.holds("vector", 0), *(match.coverage("doc", i) for i in range(4))])
    assert "50%" not in sql and "部署" not in sql and "deploy" not in sql
    numbers = sorted(int(n) for n in re.findall(r"\$(\d+)", sql))
    assert numbers == list(range(4, 4 + len(match.params)))
    # A long ASCII token matches the start of a word; a short one only a whole word.
    assert "(^|[^a-z0-9])deploy" in match.params and "(^|[^a-z0-9])v2([^a-z0-9]|$)" in match.params


def test_a_search_without_a_term_is_refused() -> None:
    with pytest.raises(ValueError):
        Match((), start=1)


@needs_db
def test_tokenization_bounds_text_and_tokens_without_losing_normal_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        tokens = await pool.fetchval("SELECT looper_search_tokens($1)", "deploy " + "a" * 10_000 + " tailmarker")
        assert "deploy" in tokens and "tailmarker" not in tokens
        assert max(len(token.encode()) for token in tokens) <= 512
        assert await pool.fetchval("SELECT array_to_tsvector(looper_search_tokens($1)) @@ '''aaa'':*'::tsquery",
                                   "a" * 10_000)
        chinese = await pool.fetchval("SELECT looper_search_tokens($1)", "甲" * 8192 + "乙丙")
        assert chinese == ["甲甲"]

    run_db(test, monkeypatch)
