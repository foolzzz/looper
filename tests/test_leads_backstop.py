"""What the leads group backstop compares (message-leads-intents RFC, R-2): the visible text of a reply against every
summary a Manager's searches returned in the last day, however it is formatted and however many leads followed."""

from __future__ import annotations

import pytest

from looper.agents.ops import leads as ops_leads


@pytest.fixture(autouse=True)
def _nothing_seen():
    ops_leads._SEEN.clear()
    ops_leads._WITHHELD.clear()
    yield
    ops_leads._SEEN.clear()
    ops_leads._WITHHELD.clear()

LEAD = ("lead-1", "Project Aurora launch moves to December 3 after the security review")


@pytest.mark.parametrize("reply", [
    "`Project` `Aurora` `launch` `moves` `to` `December` `3`",               # inline code around every word
    "~~Project~~ ~~Aurora~~ ~~launch~~ ~~moves~~ ~~to~~ ~~December~~ ~~3~~",  # strike-through around every word
    "[Project](https://e.test/1) [Aurora](https://e.test/2) [launch](https://e.test/3) [moves](https://e.test/4) "
    "[to](https://e.test/5) [December](https://e.test/6) 3",                  # a link's target is not shown
    "Project\u200bAurora\u200blaunch\u200bmoves\u200bto\u200bDecember\u200b3",  # zero-width spaces
    "Project ➡️ Aurora ➡️ launch ➡️ moves ➡️ to ➡️ December ➡️ 3",          # symbols between the words
])
def test_formatting_does_not_hide_a_quoted_summary(reply: str) -> None:
    ops_leads.remember("manager", [LEAD], now=0.0)
    assert ops_leads.quotes_a_lead("manager", reply, now=1.0)


def test_a_summary_stays_protected_for_its_whole_day_however_many_leads_follow() -> None:
    ops_leads.remember("manager", [LEAD], now=0.0)
    for batch in range(198):  # 990 other leads returned after it, and LEAD returned again in between
        ops_leads.remember("manager", [(f"lead-{batch}-{n}", f"other summary {batch} {n} " * 3) for n in range(5)],
                           now=1.0 + batch)
        if batch == 50:
            ops_leads.remember("manager", [LEAD], now=150.0)
    assert ops_leads.quotes_a_lead("manager", LEAD[1], now=300.0)
    assert not ops_leads.quotes_a_lead("manager", LEAD[1], now=150.0 + ops_leads.SEEN_SECONDS + 1)


def test_past_its_capacity_the_backstop_withholds_every_group_reply_until_the_day_ends() -> None:
    ops_leads.remember("manager", [(f"lead-{n}", f"summary number {n}") for n in range(ops_leads.SEEN_MAX + 1)],
                       now=0.0)
    assert ops_leads.quotes_a_lead("manager", "nothing in common with any lead at all", now=1.0)
    assert not ops_leads.quotes_a_lead("manager", "nothing in common with any lead at all",
                                       now=ops_leads.SEEN_SECONDS + 1)
