"""Doctor and system status report sign-in only for TTADK runtimes in use."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from looper.agents import self_check
from looper.agents.http_server import AgentHTTPServer
from looper.agents.runtimes import ttadk
from looper.db import profile_registry
from fake_claude import fake_claude


def test_doctor_waits_for_cold_ttadk_sign_in_and_fails_when_signed_out(tmp_path, monkeypatch, capsys):
    from unittest.mock import AsyncMock

    from looper import ops_config
    from looper.agents import service

    executable = fake_claude(tmp_path / "ttadk", "import sys\nsys.exit(1)\n")
    runtimes = [{"type": "codex", "executable": executable}]
    ok = self_check.Check("unrelated", "Unrelated", self_check.OK, "ready")
    monkeypatch.setattr(self_check, "check_database", AsyncMock(return_value=ok))
    monkeypatch.setattr(self_check, "_database_facts", AsyncMock(
        return_value=self_check._DatabaseFacts(ttadk_runtimes=runtimes)))
    monkeypatch.setattr(self_check, "check_claude", lambda *args: [])
    for name in ("check_codex_login", "check_grok_login", "check_cursor_login", "check_git", "check_lark_cli"):
        monkeypatch.setattr(self_check, name, lambda *args: ok)
    monkeypatch.setattr(self_check, "check_pg_dump", lambda *args: (ok, None))
    monkeypatch.setattr(self_check, "check_step_worktrees", AsyncMock(return_value=ok))
    monkeypatch.setattr(service, "doctor_check", lambda: ok)
    ttadk.forget_probe()
    try:
        ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops_config.FILE)
        assert self_check.doctor(ops) == 1
        output = capsys.readouterr().out
        assert "fail  TTADK" in output and "no valid sign-in" in output
        assert ttadk._probed[(executable, "codex")][1] is ttadk.TtadkProblem.NOT_SIGNED_IN
    finally:
        ttadk.forget_probe()


@pytest.mark.parametrize(("profiles", "problem", "status"), [
    (None, None, self_check.SKIP),
    (0, None, self_check.OK),
    (1, None, self_check.OK),
    (1, ttadk.TtadkProblem.NOT_FOUND, self_check.FAIL),
    (1, ttadk.TtadkProblem.NOT_SIGNED_IN, self_check.FAIL),
    (1, ttadk.TtadkProblem.CHECK_FAILED, self_check.WARN),
    (1, ttadk.TtadkProblem.CHECKING, self_check.PENDING),
], ids=["no-database", "no-profile", "signed-in", "missing", "signed-out", "check-failed", "checking"])
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

    async def sign_in(executable, tool, *, wait):
        assert wait is False
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


def test_pending_ttadk_check_does_not_log_a_failure_or_empty_fix(monkeypatch, caplog):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(ttadk, 'sign_in_problem', AsyncMock(return_value=ttadk.TtadkProblem.CHECKING))
    check = asyncio.run(self_check.check_ttadk([{'type': 'codex', 'executable': ''}], wait=False))
    self_check.log_problems([check])
    assert check.status == self_check.PENDING
    assert self_check.format_checks([check]).startswith('pending TTADK')
    assert 'cannot run' not in caplog.text and 'Fix:' not in caplog.text


def test_system_status_refreshes_the_ttadk_startup_check(monkeypatch):
    async def current():
        return self_check.Check("ttadk", "TTADK", self_check.OK, "signed in now")

    monkeypatch.setitem(self_check.LOGIN_CHECKS, "ttadk", current)
    startup = self_check.Check("ttadk", "TTADK", self_check.FAIL, "signed out earlier").to_dict()
    other = self_check.Check("database", "PostgreSQL", self_check.OK, "ready").to_dict()
    server = SimpleNamespace(_self_checks=[other, startup], _worker_pool=None)
    result = asyncio.run(AgentHTTPServer._checks_now(server))
    assert result == [other, asyncio.run(current()).to_dict()]


@pytest.mark.parametrize("previous", ["cold", None, ttadk.TtadkProblem.NOT_SIGNED_IN],
                         ids=["cold", "stale-success", "stale-failure"])
def test_system_status_does_not_wait_for_ttadk_and_launch_shares_refresh(previous, monkeypatch):
    ttadk.forget_probe()
    key = ("stand-in-ttadk", "cursor-cli")
    if previous != "cold":
        ttadk._probed[key] = (time.monotonic() - 61, previous)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe(executable, tool):
        calls.append((executable, tool))
        entered.set()
        assert release.wait(5)
        return None

    async def count(*args, **kwargs):
        return 1

    async def runtimes(**kwargs):
        return [{"launcher": "ttadk", "profile_count": 1, "type": "cursor-agent", "executable": key[0]}]

    monkeypatch.setattr(ttadk, "_sign_in_problem", probe)
    monkeypatch.setattr(profile_registry, "launcher_profile_count", count)
    monkeypatch.setattr(profile_registry, "list_runtimes", runtimes)
    server = SimpleNamespace(_self_checks=[self_check.Check("ttadk", "TTADK", self_check.OK, "old").to_dict()],
                             _worker_pool=None)

    async def check():
        try:
            start = time.monotonic()
            result = await AgentHTTPServer._checks_now(server)
            assert time.monotonic() - start < 0.2
            expected = self_check.PENDING if previous == "cold" else self_check.FAIL if previous else self_check.OK
            assert result[0]["status"] == expected
            assert await asyncio.to_thread(entered.wait, 1)
            assert await AgentHTTPServer._checks_now(server) == result
            launch = asyncio.create_task(ttadk.sign_in_problem(*key))
            await asyncio.sleep(0)
            assert not launch.done()
            release.set()
            assert await asyncio.wait_for(launch, 2) is None
            assert (await AgentHTTPServer._checks_now(server))[0]["status"] == self_check.OK
            assert calls == [key]
        finally:
            release.set()
            await asyncio.to_thread(ttadk.forget_probe)

    asyncio.run(asyncio.wait_for(check(), 8))
