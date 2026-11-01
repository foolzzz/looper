"""Doctor and system status report sign-in only for TTADK runtimes in use."""

import asyncio
from types import SimpleNamespace

import pytest

from looper.agents import self_check
from looper.agents.http_server import AgentHTTPServer
from looper.agents.runtimes import ttadk
from looper.db import profile_registry


@pytest.mark.parametrize(("profiles", "problem", "status"), [
    (None, None, self_check.SKIP),
    (0, None, self_check.OK),
    (1, None, self_check.OK),
    (1, ttadk.TtadkProblem.NOT_FOUND, self_check.FAIL),
    (1, ttadk.TtadkProblem.NOT_SIGNED_IN, self_check.FAIL),
    (1, ttadk.TtadkProblem.CHECK_FAILED, self_check.WARN),
], ids=["no-database", "no-profile", "signed-in", "missing", "signed-out", "check-failed"])
def test_ttadk_check_reports_usage_and_noninteractive_sign_in(monkeypatch, profiles, problem, status):
    calls = []

    async def count(launcher, **kwargs):
        assert launcher == "ttadk"
        if profiles is None:
            raise RuntimeError("no database")
        return profiles

    async def runtimes(**kwargs):
        assert profiles
        return [{"launcher": "native", "profile_count": 1},
                {"launcher": "ttadk", "profile_count": 0},
                {"launcher": "ttadk", "profile_count": 1, "type": "codex", "executable": "custom-ttadk"}]

    async def sign_in(executable, tool):
        calls.append((executable, tool))
        return problem

    monkeypatch.setattr(profile_registry, "launcher_profile_count", count)
    monkeypatch.setattr(profile_registry, "list_runtimes", runtimes)
    monkeypatch.setattr(ttadk, "sign_in_problem", sign_in)
    check = asyncio.run(self_check.ttadk_check())
    assert (check.id, check.status) == ("ttadk", status)
    assert calls == ([("custom-ttadk", "codex")] if profiles else [])
    if problem is not None:
        assert check.detail == problem.message("codex")


def test_system_status_refreshes_the_ttadk_startup_check(monkeypatch):
    async def current():
        return self_check.Check("ttadk", "TTADK", self_check.OK, "signed in now")

    monkeypatch.setitem(self_check.LOGIN_CHECKS, "ttadk", current)
    startup = self_check.Check("ttadk", "TTADK", self_check.FAIL, "signed out earlier").to_dict()
    other = self_check.Check("database", "PostgreSQL", self_check.OK, "ready").to_dict()
    server = SimpleNamespace(_self_checks=[other, startup], _worker_pool=None)
    result = asyncio.run(AgentHTTPServer._checks_now(server))
    assert result == [other, asyncio.run(current()).to_dict()]
