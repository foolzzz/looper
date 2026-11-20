"""Subscription selection rules: their validation, what they take, how a window reads and writes: no database."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from looper.agents import subscription_rules as rules, subscriptions

ROW = {"chat_kind": "group", "sender_kind": "user", "sender_id": "ou_alice", "chat_id": "oc_a", "mentions_me": False,
       "text": "hello", "chat_name": "Room", "sent_at": datetime(2026, 11, 19, 16, 5, 3, tzinfo=timezone.utc),
       "sender_name": "Alice"}


def pick(matcher, row):
    """The index of the rule that takes ``row`` (the text patterns run in their own process, as in a window)."""
    row = {"seq": 1, **row}
    asyncio.run(matcher.prepare([row]))
    return matcher(row)


def rule(**fields):
    return rules.parse_rule({"name": "r", **fields}, 0)


def test_a_rule_names_its_conditions_and_defaults_to_everything():
    found = rule()
    assert found["chat_kinds"] == list(rules.CHAT_KINDS) and found["mentions_me"] == "any" and not found["never_quiet"]
    assert found["text_match"] == "" and found["areas"] == {}


@pytest.mark.parametrize("raw, message", [
    ({}, "needs a name"), ({"name": "a{b}"}, "braces"), ({"name": "r", "chat_kinds": []}, "chat_kinds"),
    ({"name": "r", "chat_kinds": ["room"]}, "chat_kinds"), ({"name": "r", "sender_kinds": ["bot"]}, "sender_kinds"),
    ({"name": "r", "mentions_me": "yes"}, "mentions_me"), ({"name": "r", "exclude_self": 1}, "exclude_self"),
    ({"name": "r", "never_quiet": "true"}, "never_quiet"), ({"name": "r", "mystery": 1}, "unknown field 'mystery'"),
    ({"name": "r", "text_match": "("}, "not a valid pattern"), ({"name": "r", "text_match": "x" * 501}, "text_match"),
    ({"name": "r", "text_match": "(a+)+$"}, "repeats a group"), ({"name": "r", "text_match": "(\\w*)*x"}, "repeats a group"),
    ({"name": "r", "areas": {"oc": ""}}, "an area"), ({"name": "r", "include_chats": "oc_a"}, "include_chats")])
def test_a_rule_the_api_cannot_use_is_refused_and_named(raw, message):
    with pytest.raises(ValueError, match=message):
        rules.parse_rule(raw, 3)


def test_too_many_rules_or_none_at_all_in_the_wrong_shape_are_refused():
    with pytest.raises(ValueError, match="at most 20"):
        rules.parse_rules([{"name": f"r{n}"} for n in range(21)])
    with pytest.raises(ValueError, match="list"):
        rules.parse_rules({"name": "r"})
    assert rules.parse_rules([]) == []


CATASTROPHIC = ["(a+)+$", "(a|aa)+$", "(?:a|aa)+b", "((a|aa))*b", "(a|a)*c", "(.*)*x", "(x+x+)+y", "(.*a){12}",
                "(\\w+\\s?)*$", "([a-z]+)*\\d", "(a|b|ab)+c", "a*a*a*a*b", ".*.*.*x", "(a+){2,}b",
                # The ways round a text scan of the pattern, as Python parses them:
                "(a+){,50}b", "(?x)(a+) *b", "(?x)(a+) # comment\n *b", "a{0,1000}a{0,1000}a{0,1000}b", "(a?){30}a{30}",
                "a?a?a?a?a?a?a?a?a?b", "(a|a)(a|a)(a|a)(a|a)(a|a)(a|a)(a|a)(a|a)(a|a)c", "(?:a{1,2}){1,20}b",
                "(a{,5}){,5}b"]


@pytest.mark.parametrize("pattern", CATASTROPHIC)
def test_a_pattern_that_can_backtrack_without_bound_is_refused(pattern):
    with pytest.raises(ValueError, match="repeats a group|repeats that can run long|more than \\d+ (repeats|alternations)"):
        rules.check_pattern(pattern)


def test_a_pattern_that_is_only_quadratic_runs_on_a_bounded_text():
    import time

    # Two unbounded repeats are allowed and cost at most the square of the text, which is cut to MATCH_TEXT_CHARS.
    assert rules.check_pattern(".*a.*b") == ".*a.*b"
    matcher = rules.Matcher([rule(text_match=".*a.*b")], me=None)
    started = time.monotonic()
    assert pick(matcher, {**ROW, "text": "a" * 50_000}) is None  # no b: the worst case, on the first 1,000 characters
    assert time.monotonic() - started < rules.REGEX_TIMEOUT_SECONDS


def test_a_search_that_overruns_its_deadline_is_killed_and_no_pattern_takes_anything(monkeypatch):
    import time

    monkeypatch.setattr(rules, "_WORKER", "import time\ntime.sleep(60)")
    monkeypatch.setattr(rules, "REGEX_TIMEOUT_SECONDS", 0.5)
    matcher = rules.Matcher([rule(text_match="needle"), rule(name="all")], me=None)
    started = time.monotonic()
    assert pick(matcher, {**ROW, "text": "a needle"}) == 1  # the pattern rule took nothing; the one without a pattern did
    assert time.monotonic() - started < 5 and matcher.timed_out


@pytest.mark.parametrize("pattern", ["告警|故障", r"\b(P0|P1)\b", r"\[(critical|warning)\]", "MR|PR|CR", "(ab)+c", "a+b*", "(?:(?:a|b)c)*", "(?x) 告警 | 故障 # either"])
def test_ordinary_patterns_are_accepted(pattern):
    assert rules.check_pattern(pattern) == pattern


def test_a_pattern_runs_on_a_bounded_text():
    matcher = rules.Matcher([rule(text_match="needle")], me=None)
    assert pick(matcher, {**ROW, "text": "x" * rules.MATCH_TEXT_CHARS + "needle"}) is None  # past the bound: not read
    assert pick(matcher, {**ROW, "text": "a needle"}) == 0
    assert pick(rules.Matcher([rule(text_match="NEEDLE")], me=None), {**ROW, "text": "a needle"}) == 0  # any case


def test_the_first_rule_that_takes_an_event_names_it_and_each_condition_holds_alone():
    me = "ou_me"
    ruleset = [rule(name="direct", chat_kinds=["p2p"], sender_kinds=["user"], exclude_self=True),
               rule(name="mention", mentions_me="only"),
               rule(name="alerts", include_chats=["oc_alerts"], text_match="告警"),
               rule(name="quiet-chats", chat_kinds=["group"], exclude_chats=["oc_a"], mentions_me="exclude")]
    matcher = rules.Matcher(ruleset, me=me)
    p2p = {**ROW, "chat_kind": "p2p", "chat_id": "oc_dm"}
    assert pick(matcher, p2p) == 0
    assert pick(matcher, {**p2p, "sender_id": me}) is None  # my own reply
    assert pick(matcher, {**p2p, "sender_kind": "app", "sender_id": "cli_x"}) is None  # a bot's notification
    assert pick(matcher, {**ROW, "mentions_me": True}) == 1
    assert pick(matcher, {**ROW, "chat_id": "oc_alerts", "text": "磁盘告警"}) == 2
    assert pick(matcher, {**ROW, "chat_id": "oc_alerts", "text": "all good"}) == 3
    assert pick(matcher, {**ROW, "chat_id": "oc_alerts", "text": "磁盘告警", "mentions_me": True}) == 1  # first rule wins
    assert pick(matcher, ROW) is None  # oc_a is excluded by the last rule, taken by no other
    assert pick(matcher, {**ROW, "chat_id": "oc_b", "chat_kind": "topic"}) is None


def test_a_line_carries_the_tag_of_the_rule_and_the_area_of_the_chat():
    ruleset = [rule(name="alerts", label="告警", areas={"oc_a": "存储"}), rule(name="direct")]
    tagged = [{**ROW, "rule": 0}, {**ROW, "chat_id": "oc_z", "rule": 0}, {**ROW, "rule": 1}]
    text = subscriptions.render_lines_v2(tagged, text_max_chars=200, zone_name="UTC", rules=ruleset)
    assert text.splitlines() == [
        "[2026/11/19 16:05:03] 【告警·存储】《Room》 Alice: hello", "[2026/11/19 16:05:03] 【告警】《Room》 Alice: hello",
        "[2026/11/19 16:05:03] 【direct】《Room》 Alice: hello"]
    # Without a rule it is lines_v1's line.
    assert subscriptions.render_lines_v2([ROW], text_max_chars=200, zone_name="UTC", rules=[]) == \
        subscriptions.render_lines_v1([ROW], text_max_chars=200, zone_name="UTC")
    assert subscriptions.processor_input([ROW], text_max_chars=200, zone_name="UTC") == \
        "消息(共 1 条):\n" + subscriptions.render_lines_v1([ROW], text_max_chars=200, zone_name="UTC")


def test_counts_add_up_by_rule_name_and_fill_the_headers_placeholders():
    ruleset = [rule(name="直接", label="单聊"), rule(name="直接", label="@我"), rule(name="告警")]
    chosen = [{"rule": 0}, {"rule": 1}, {"rule": 1}, {"rule": 2}]
    found = rules.counts(chosen, ruleset)
    assert found == {"直接": 3, "告警": 1}
    header = "🔔 工作速报 · 直接找你 {count:直接} / 告警 {count:告警} / 评审 {count:评审} · 共 {count}"
    assert rules.format_header(header, found, 4) == "🔔 工作速报 · 直接找你 3 / 告警 1 / 评审 0 · 共 4"
    assert rules.format_header("🍉 plain", found, 4) == "🍉 plain"


@pytest.mark.parametrize("header", ["{count:x}{count}", "no braces", "{count:直接} {count:告警}",
                                   "{date}", "{date} · {count} · {count:评审}"])
def test_a_headers_counts_are_accepted(header):
    assert rules.check_header(header) == header


@pytest.mark.parametrize("header", ["{name}", "{count:}", "}{", "{count:a{b}", "{{count}}x{"])
def test_any_other_brace_in_a_header_is_refused(header):
    with pytest.raises(ValueError, match="braces"):
        rules.check_header(header)


def test_the_fallback_lists_only_the_rules_that_must_not_be_quiet_one_block_each():
    ruleset = [rule(name="d", label="单聊", never_quiet=True), rule(name="c", label="变更"),
               rule(name="m", label="@我", never_quiet=True, areas={"oc_a": "存储"})]
    chosen = [{**ROW, "rule": 0, "text": "line one\n\n  line  two " + "x" * 300}, {**ROW, "rule": 1, "text": "skip me"},
              {**ROW, "rule": 2, "text": "@所有人 look"}]
    text = rules.fallback_text(chosen, ruleset)
    blocks = text.split("\n\n")
    assert blocks[0].startswith("单聊\n- **Room**：line one line two x") and "skip me" not in text
    assert len(blocks[0].splitlines()[1]) < 230 and blocks[1] == "@我\n- **Room（存储）**：@所有人 look"
    assert rules.fallback_text([{**ROW, "rule": 1}], ruleset) == ""
    many = [{**ROW, "rule": 0, "text": str(n)} for n in range(50)]
    assert len(rules.fallback_text(many, ruleset).splitlines()) == 1 + rules.FALLBACK_PER_RULE


@pytest.mark.parametrize("sub, private", [
    ({"rules": [], "chat_kinds": ["group", "topic"]}, False), ({"rules": [], "chat_kinds": ["p2p"]}, True),
    ({"rules": [rule(chat_kinds=["group"])]}, False), ({"rules": [rule(chat_kinds=["p2p"])]}, True),
    ({"rules": [rule(chat_kinds=["group"], mentions_me="only")]}, True),
    ({"rules": [rule(chat_kinds=["group"], never_quiet=True)]}, True),
    ({"rules": [rule(chat_kinds=["group"], mentions_me="exclude")]}, False),
    # A direct chat the directory does not list yet is stored as kind unknown: it may be one.
    ({"rules": [], "chat_kinds": ["group", "unknown"]}, True), ({"rules": [rule(chat_kinds=["group", "unknown"])]}, True),
    ({"rules": [rule()]}, True)])
def test_what_can_select_private_content(sub, private):
    assert rules.is_private(sub) is private


@pytest.mark.parametrize("channel, chat, operator, ok", [
    ("lark", "ou_operator", "ou_operator", True), ("lark_bot", "ou_operator", "ou_operator", True),
    ("lark", "ou_colleague", "ou_operator", False),  # a direct chat, but not the operator's
    ("lark", "oc_group", "ou_operator", False), ("lark", "ou_operator", None, False),  # nobody to compare with
    ("telegram", "123456", "ou_operator", False), ("telegram", "-100123", "ou_operator", False)])
def test_a_private_subscription_may_push_to_the_operators_own_direct_chat_only(channel, chat, operator, ok):
    private = {"rules": [rule(never_quiet=True)]}
    if ok:
        subscriptions.check_private_target(private, channel, chat, operator)
    else:
        with pytest.raises(ValueError, match="only to your own direct chat"):
            subscriptions.check_private_target(private, channel, chat, operator)
    subscriptions.check_private_target({"rules": [rule(chat_kinds=["group"])]}, channel, chat, operator)  # nothing private
    subscriptions.check_private_target(private, None, None, operator)  # no target: the dashboard only


def test_rules_in_a_request_fill_the_filter_columns_they_replace():
    raw = {"source_id": "s", "max_age_minutes": 60, "max_events": 300, "text_max_chars": 300, "render": "lines_v2",
           "rules": [{"name": "direct", "chat_kinds": ["p2p"]}]}
    found = subscriptions.parse(raw, update=False)
    assert found["chat_kinds"] == list(rules.CHAT_KINDS) and found["sender_kinds"] == ["user", "app"]
    assert found["rules"][0]["name"] == "direct" and found["render"] == "lines_v2"
    with pytest.raises(ValueError, match="subscription.rules.*text_match"):
        subscriptions.parse({**raw, "rules": [{"name": "r", "text_match": "(a+)+"}]}, update=False)
    with pytest.raises(ValueError, match="subscription.output_header"):
        subscriptions.parse({**raw, "output_header": "{oops}"}, update=False)
