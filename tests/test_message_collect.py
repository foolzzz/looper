"""Collection boundaries, pagination and shared call budgets (synthetic data only)."""

import asyncio
from datetime import timedelta

import pytest

from looper.agents.message_collect import (
    AuthStatus,
    Budget,
    CollectorError,
    CollectorErrorKind,
    Page,
    SearchHit,
    check_identity,
    pages,
    parse_message,
)
from tests import msgflow_support as sup


@pytest.mark.parametrize(
    "auth,kind",
    [
        (AuthStatus("missing", None), CollectorErrorKind.AUTH_MISSING),
        (AuthStatus("expired", sup.ME), CollectorErrorKind.AUTH_EXPIRED),
        (AuthStatus("ready", "ou_other_synthetic"), CollectorErrorKind.IDENTITY),
        (AuthStatus("mystery", sup.ME), CollectorErrorKind.PROTOCOL),
    ],
)
def test_auth_failure_or_identity_change_prevents_all_message_reads(auth, kind):
    with pytest.raises(CollectorError) as failure:
        check_identity(auth, sup.ME)
    assert failure.value.kind == kind
    check_identity(AuthStatus("needs_refresh", sup.ME), sup.ME)


def test_search_chat_and_thread_pagination_keep_equal_timestamp_ids():
    async def check():
        stamp = sup.NOW - timedelta(milliseconds=1)
        answers = [
            Page([SearchHit("m1", "oc_synthetic", stamp)], True, "next"),
            Page([SearchHit("m2", "oc_synthetic", stamp)], False, ""),
        ]

        async def read(token):
            return answers.pop(0)

        hits = [item async for page in pages(Budget(), read) for item in page]
        assert [item.message_id for item in hits] == ["m1", "m2"]
        message = parse_message(sup.message("m2", sent_at=stamp, text="hello\x00", mentions=[(sup.ME, "Operator")]))
        assert message.sent_at == stamp
        assert "\x00" not in message.text and message.mention_ids == [sup.ME]

    asyncio.run(check())


@pytest.mark.parametrize(
    "answers",
    [
        [Page([], True, "")],
        [Page([], True, "loop"), Page([], True, "loop")],
        [Page([SearchHit("m", "c", sup.NOW)], True, "a"), Page([SearchHit("m", "c", sup.NOW)], True, "b")],
    ],
)
def test_protocol_failure_never_completes_a_window(answers):
    async def check():
        async def read(token):
            return answers.pop(0)

        with pytest.raises(CollectorError) as failure:
            _ = [page async for page in pages(Budget(), read)]
        assert failure.value.kind == CollectorErrorKind.PAGINATION

    asyncio.run(check())
    for invalid in (
        {},
        sup.message("bad") | {"create_time": "1790000000"},
        sup.message("bad") | {"create_time": "NaN"},
    ):
        with pytest.raises(CollectorError):
            parse_message(invalid)


def test_deadline_and_retry_budget_are_shared_by_all_calls():
    async def check():
        attempts = 0

        async def limited():
            nonlocal attempts
            attempts += 1
            raise CollectorError(CollectorErrorKind.RATE_LIMIT, retry_after=0)

        with pytest.raises(CollectorError) as failure:
            await Budget().call(limited)
        assert attempts == 3 and failure.value.kind == CollectorErrorKind.RATE_LIMIT
        budget = Budget(seconds=0.01)

        async def slow():
            await asyncio.sleep(1)

        with pytest.raises(CollectorError) as failure:
            await budget.call(slow)
        assert failure.value.kind == CollectorErrorKind.DEADLINE
        with pytest.raises(CollectorError):
            await budget.call(slow)

    asyncio.run(check())
