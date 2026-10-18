"""Grok's own login in Looper's Grok homes (``looper/agents/runtimes/grok_login.py``): every login home links to the
operator's login file, a run that left a copy or another entry in its place is caught, and the login is then
unavailable to runs until the operator clears it; the self-check reads the login file without opening it."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, adapter, grok_login
from looper.agents.runtimes.grok_login import LoginState, LoginStatus
from looper.agents.self_check import FAIL, OK, SKIP, check_grok_login
from looper.agents.system_prompt import SystemPrompt

GROK = adapter("grok")
LOGIN = ProviderSpec("prov-grok-login", "grok-login", AuthKind.OAUTH, frozenset({ApiProtocol.OPENAI_RESPONSES}),
                     "grok")


@pytest.fixture
def settings_table(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """``system_settings`` in memory."""
    from looper.db import system_settings

    table: dict[str, str] = {}

    async def get_settings(keys: Any, **_: Any) -> dict[str, str]:
        return {key: table[key] for key in keys if key in table}

    async def put_settings(values: Any, **_: Any) -> None:
        for key, value in values.items():
            if value is None:
                table.pop(key, None)
            else:
                table[key] = value

    monkeypatch.setattr(system_settings, "get_settings", get_settings)
    monkeypatch.setattr(system_settings, "put_settings", put_settings)
    grok_login.forget_probe()
    return table


def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "operator-grok"
    directory.mkdir()
    monkeypatch.setenv("GROK_HOME", str(directory))
    login = directory / "auth.json"
    login.write_text("stand-in login")
    return login


def _run(tmp_path: Path, prompt: str = "PROMPT") -> tuple[Launch, list[str]]:
    profile = AgentProfile("agent-grok-login", "grok/login", RuntimeSpec("rt-grok", "grok", "grok", "",
                                                                       GROK.capabilities), LOGIN, "grok-4.7")
    launch = Launch(AgentSettings(profile=profile))
    command = GROK.run_command("grok", cwd=tmp_path, prompt_file=SystemPrompt(prompt).write_file(tmp_path / "rt"),
                               launch=launch)
    return launch, command


def _home(command: list[str]) -> Path:
    return Path(command[1].removeprefix("GROK_HOME="))


def _copy(home: Path) -> None:
    """As a save that replaced the link with a file of its own would leave it."""
    (home / "auth.json").unlink()
    (home / "auth.json").write_text("forked tokens")


def _elsewhere(home: Path) -> None:
    (home / "auth.json").unlink()
    (home / "auth.json").symlink_to(home / "config.toml")


def _removed(home: Path) -> None:
    (home / "auth.json").unlink()


@pytest.mark.parametrize("grok_home_set", [True, False], ids=["GROK_HOME", "~/.grok"])
def test_the_login_file_is_the_servers_grok_homes_else_the_users(grok_home_set: bool, tmp_path: Path) -> None:
    environ = {"GROK_HOME": str(tmp_path / "g")} if grok_home_set else {}
    expected = tmp_path / "g" / "auth.json" if grok_home_set else Path.home() / ".grok" / "auth.json"
    assert grok_login.login_file(environ) == expected


@pytest.mark.parametrize(("change", "words"), [
    (_copy, "had become a copy of the Grok login"),
    (_elsewhere, "was not the link to the Grok login"),
    (_removed, "as `grok logout` there would"),
], ids=["copy", "link-elsewhere", "link-removed"])
def test_a_run_that_forked_the_login_is_caught_and_the_login_is_unavailable_until_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings_table: dict[str, str], change: Any, words: str,
) -> None:
    login = _operator(tmp_path, monkeypatch)
    launch, command = _run(tmp_path)
    home = _home(command)
    assert asyncio.run(GROK.after_run(launch, command)) == []  # an intact link: nothing to say
    assert asyncio.run(GROK.login_problem(LOGIN)) is None

    change(home)
    (warning,) = asyncio.run(GROK.after_run(launch, command))
    assert warning.startswith("Grok login unavailable:") and words in warning and str(home) in warning
    assert "grok login" in warning
    # A copy or another entry is removed, never followed, and the link put back; the operator's file is untouched.
    assert os.readlink(home / "auth.json") == str(login) and login.read_text() == "stand-in login"
    assert asyncio.run(GROK.after_run(launch, command)) == []  # put right, so reported once
    # Recorded: runs with the login cannot start, and the dashboard says so.
    problem = asyncio.run(GROK.login_problem(LOGIN))
    assert problem is not None and problem.startswith("the Grok login is unavailable since") and words in problem
    assert asyncio.run(grok_login.login_view())["state"] == "unavailable"
    # The operator checked the login and cleared the problem: runs start again, in the same home.
    asyncio.run(grok_login.clear_problem())
    assert asyncio.run(GROK.login_problem(LOGIN)) is None and _home(_run(tmp_path)[1]) == home
    assert asyncio.run(grok_login.login_view()) == {"state": "logged_in", "detail": f"signed in ({login})"}


def test_without_the_login_file_a_run_does_not_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    settings_table: dict[str, str]) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "logged-out"))
    problem = asyncio.run(GROK.login_problem(LOGIN))
    assert problem is not None and "Grok is not signed in on this machine" in problem and "grok login" in problem


def test_the_server_start_catches_a_fork_no_run_saw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    settings_table: dict[str, str]) -> None:
    login = _operator(tmp_path, monkeypatch)
    _, command = _run(tmp_path)
    _copy(_home(command))
    problem = asyncio.run(grok_login.inspect_at_start(tmp_path / "rt"))
    assert problem is not None and "had become a copy" in problem and login.read_text() == "stand-in login"
    assert asyncio.run(grok_login.inspect_at_start(tmp_path / "rt")) is None  # put right, so reported once


def test_the_login_state_is_read_without_opening_the_file(tmp_path: Path) -> None:
    login = tmp_path / "auth.json"
    assert grok_login.probe_login(login).state is LoginState.NOT_LOGGED_IN
    login.mkdir()
    assert grok_login.probe_login(login) == LoginStatus(LoginState.NOT_LOGGED_IN, f"{login} is not a file")
    login.rmdir()
    login.write_text("tokens")
    login.chmod(0o000)  # unreadable: its state is still known
    try:
        assert grok_login.probe_login(login) == LoginStatus(LoginState.LOGGED_IN, f"signed in ({login})")
    finally:
        login.chmod(0o600)


@pytest.mark.parametrize(("profiles", "problem", "state", "status"), [
    (None, None, LoginState.LOGGED_IN, SKIP),
    (0, None, LoginState.LOGGED_IN, SKIP),
    (1, {"detail": "a copy", "at": "2026-10-17T10:00:00+00:00"}, LoginState.LOGGED_IN, FAIL),
    (1, None, LoginState.LOGGED_IN, OK),
    (2, None, LoginState.NOT_LOGGED_IN, FAIL),
], ids=["no-database", "no-profile", "forked", "logged-in", "not-logged-in"])
def test_the_grok_login_check_is_skipped_without_a_profile_and_fails_on_a_fork(
    profiles: int | None, problem: dict[str, str] | None, state: LoginState, status: str,
) -> None:
    probed: list[bool] = []

    def probe() -> LoginStatus:
        probed.append(True)
        return LoginStatus(state, state.value)

    check = check_grok_login(profiles, problem, probe)
    assert (check.id, check.label, check.status) == ("grok_login", "Grok login", status)
    assert bool(probed) == (bool(profiles) and problem is None)
    if problem is not None:
        assert "a copy" in check.detail and "grok login" in check.fix
