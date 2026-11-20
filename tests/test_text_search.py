"""The search's tokens and terms (``db.text_search``): Chinese as overlapping pairs, English as words, both
lower-cased; and the SQL it builds, every value a parameter."""

from __future__ import annotations

import re

import pytest

from looper.db import text_search
from looper.db.text_search import Match, Term, terms, tokens


def test_chinese_becomes_overlapping_pairs_and_english_words_lower_cased() -> None:
    assert tokens("张三决定下周五发布Looper新版本，v2.15 王 a") == (
        "张三", "三决", "决定", "定下", "下周", "周五", "五发", "发布", "新版", "版本", "王", "looper", "v2", "15")


def test_a_word_of_one_ascii_character_is_no_token_and_a_term_needs_a_token_or_two_characters() -> None:
    assert tokens("a b 1") == ()
    assert terms(["A", "c++", "C++", "发布", "", "  "]) == (Term("c++", ()), Term("发布", ("发布",)))


def test_a_query_has_at_most_eight_terms_of_at_most_sixteen_tokens() -> None:
    found = terms([f"word{i}" for i in range(12)] + ["甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"])
    assert len(found) == text_search.TERMS_MAX == 8
    assert len(terms(["甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"])[0].tokens) == text_search.TOKENS_MAX == 16


def test_the_sql_carries_every_value_as_a_parameter_numbered_after_the_callers() -> None:
    match = Match(terms(["50%_off", "部署方案", "deploy", "v2"]), start=4)
    prefilter = match.prefilter("doc")
    coverage = [match.coverage("doc", i) for i in range(4)]
    sql = " ".join([prefilter, *coverage])
    assert "50%" not in sql and "部署" not in sql and "deploy" not in sql
    numbers = sorted(int(n) for n in re.findall(r"\$(\d+)", sql))
    assert numbers == list(range(4, 4 + len(match.params)))
    patterns = match.params[0]
    assert "%50\\%\\_off%" in patterns and "%部署%" in patterns and "%署方%" in patterns and "%deploy%" in patterns
    # A long ASCII token matches the start of a word; a short one only a whole word.
    assert "(^|[^a-z0-9])deploy" in match.params and "(^|[^a-z0-9])v2([^a-z0-9]|$)" in match.params


def test_a_search_without_a_term_is_refused() -> None:
    with pytest.raises(ValueError):
        Match((), start=1)
