"""Manager runtime errors have bounded code-owned descriptions, without replaying a tool turn."""
from __future__ import annotations

import pytest

from looper.agents.failures import Failure, FailureCause
from looper.agents.tiers.manager import _typed_failure_reply


@pytest.mark.parametrize("cause,category", [
    (FailureCause.CONNECTION_FAILED, "瞬时故障"),
    (FailureCause.PROVIDER_OVERLOADED, "瞬时故障"),
    (FailureCause.START_TIMEOUT, "瞬时故障"),
    (FailureCause.RUNTIME_MISSING, "环境问题"),
    (FailureCause.LOGIN, "环境问题"),
    (FailureCause.TLS, "环境问题"),
    (FailureCause.PROFILE, "环境问题"),
])
def test_manager_type_reply_is_code_owned_and_does_not_claim_a_retry(cause, category):
    reply = _typed_failure_reply(Failure(cause))
    assert category in reply and cause.value in reply
    assert "重试" not in reply or "自动重试" in reply


@pytest.mark.parametrize("cause", [FailureCause.USAGE_LIMIT, FailureCause.REPORT, FailureCause.UNKNOWN])
def test_quota_retention_and_judgment_keep_their_existing_paths(cause):
    assert _typed_failure_reply(Failure(cause)) is None
