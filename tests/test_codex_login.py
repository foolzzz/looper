"""Codex's own login in Looper's Codex homes (``looper/agents/runtimes/codex_login.py``).

The login state is read with ``codex login status`` (its lines checked against
the Codex CLI 0.162.0 source and a real run), here through a stand-in
``codex``; the fork check against homes on disk, with a stand-in for the
operator's login file (never the real one, ``conftest._no_operator_codex_login``)
and the ``system_settings`` table kept in memory.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.claude_cli import ClaudeRunRecorder
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, adapter, codex_login
from looper.agents.runtimes.codex_login import LoginState, parse_status
from looper.agents.self_check import FAIL, OK, SKIP, WARN, check_codex_login
from looper.agents.system_prompt import SystemPrompt

CODEX = adapter("codex")
LOGIN = ProviderSpec("prov-codex-login", "codex-login", AuthKind.OAUTH, frozenset({ApiProtocol.OPENAI_RESPONSES}),
                     "codex")
API_KEY = ProviderSpec("prov-cpa", "cpa", AuthKind.API_KEY, frozenset({ApiProtocol.OPENAI_RESPONSES}),
                       base_url="http://127.0.0.1:8317/v1", api_key="sk-test")


# --- What `codex login status` says ---


@pytest.mark.parametrize(("returncode", "output", "state", "detail"), [
    (0, "Logged in using ChatGPT\n", LoginState.CHATGPT, "logged in using ChatGPT"),
    (0, "WARNING: a notice first\nLogged in using ChatGPT\n", LoginState.CHATGPT, "logged in using ChatGPT"),
    (0, "Logged in using an API key - sk-proj-***ABCD\n", LoginState.OTHER_METHOD,
     "logged in using an API key, not ChatGPT"),
    (0, "Logged in using personal access token\n", LoginState.OTHER_METHOD,
     "logged in using personal access token, not ChatGPT"),
    (1, "Not logged in\n", LoginState.NOT_LOGGED_IN, "not logged in"),
    (1, "Error checking login status: bad file\n", LoginState.UNKNOWN,
     "`codex login status` exited 1: Error checking login status: bad file"),
    (0, "", LoginState.UNKNOWN, "`codex login status` exited 0"),
], ids=["chatgpt", "after-a-notice", "api-key", "other-method", "not-logged-in", "error", "nothing"])
def test_login_status_is_read_by_its_exit_code_and_line(returncode: int, output: str, state: LoginState,
                                                        detail: str) -> None:
    status = parse_status(returncode, output)
    assert (status.state, status.detail) == (state, detail)
    assert "ABCD" not in status.detail  # an API key login's key is never kept


def test_the_probe_runs_in_a_home_with_only_the_link_and_leaves_the_login_file(tmp_path: Path,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    login = tmp_path / "operator" / "auth.json"
    login.parent.mkdir()
    login.write_text("stand-in login")
    monkeypatch.setenv("CODEX_API_KEY", "sk-would-replace-the-login")
    codex = fake_claude(tmp_path / "codex", (
        "import os, sys\n"
        "home = os.environ['CODEX_HOME']\n"
        "assert sys.argv[1:] == ['-c', 'cli_auth_credentials_store=\"file\"', 'login', 'status'], sys.argv\n"
        "assert os.listdir(home) == ['auth.json'] and os.readlink(os.path.join(home, 'auth.json')) == "
        f"{str(login)!r}\n"
        "assert 'CODEX_API_KEY' not in os.environ\n"
        "open(os.path.join(home, 'logs.sqlite'), 'w').write('codex writes its state here')\n"
        f"open({str(tmp_path / 'probe-home')!r}, 'w').write(home)\n"
        "print('Logged in using ChatGPT', file=sys.stderr)\n"
    ))
    status = codex_login.probe_login(codex, login)
    assert (status.state, status.detail) == (LoginState.CHATGPT, "logged in using ChatGPT")
    # The probe home is gone, its link unlinked: the login file is as it was.
    assert not os.path.lexists((tmp_path / "probe-home").read_text()) and login.read_text() == "stand-in login"
    assert codex_login.probe_login(str(tmp_path / "no-codex"), login).state is LoginState.UNKNOWN


# --- A forked login ---


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
    return table


def _operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "operator-codex"
    directory.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(directory))
    login = directory / "auth.json"
    login.write_text("stand-in login")
    return login


def _run(tmp_path: Path, provider: ProviderSpec = LOGIN, prompt: str = "PROMPT") -> tuple[Launch, list[str]]:
    profile = AgentProfile("agent-codex-login", "codex/login", RuntimeSpec("rt-codex", "codex", "codex", "",
                                                                         CODEX.capabilities), provider, "gpt-6.1-sol")
    launch = Launch(AgentSettings(profile=profile))
    command = CODEX.run_command("codex", cwd=tmp_path, prompt_file=SystemPrompt(prompt).write_file(tmp_path / "rt"),
                                launch=launch)
    return launch, command


def _home(command: list[str]) -> Path:
    return Path(command[1].removeprefix("CODEX_HOME="))


def _copy(home: Path) -> None:
    """As a save that replaced the link with a file of its own would leave it."""
    (home / "auth.json").unlink()
    (home / "auth.json").write_text("forked tokens")


def _elsewhere(home: Path) -> None:
    (home / "auth.json").unlink()
    (home / "auth.json").symlink_to(home / "config.toml")


def _removed(home: Path) -> None:
    (home / "auth.json").unlink()


@pytest.mark.parametrize(("change", "words"), [
    (_copy, "had become a copy of the Codex login"),
    (_elsewhere, "was not the link to the Codex login"),
    (_removed, "link in"),
], ids=["copy", "link-elsewhere", "link-removed"])
def test_a_run_that_forked_the_login_is_caught_and_the_login_is_unavailable_until_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings_table: dict[str, str], change: Any, words: str,
) -> None:
    login = _operator(tmp_path, monkeypatch)
    launch, command = _run(tmp_path)
    home = _home(command)
    assert asyncio.run(CODEX.after_run(launch, command)) == []  # an intact link: nothing to say
    assert asyncio.run(CODEX.login_problem(LOGIN)) is None

    change(home)
    (warning,) = asyncio.run(CODEX.after_run(launch, command))
    assert words in warning and str(home) in warning and "codex login status" in warning
    # A copy or another entry is removed, never followed, and the link put back; the operator's file is untouched.
    assert os.readlink(home / "auth.json") == str(login) and login.read_text() == "stand-in login"
    assert asyncio.run(CODEX.after_run(launch, command)) == []  # put right, so reported once
    # Recorded: runs with the login cannot start, and the dashboard says so.
    problem = asyncio.run(CODEX.login_problem(LOGIN))
    assert problem is not None and problem.startswith("the Codex login is unavailable since") and words in problem
    assert asyncio.run(codex_login.login_view())["state"] == "unavailable"
    assert asyncio.run(CODEX.login_problem(API_KEY)) is None  # an API key provider is not held up
    # The operator checked the login and cleared the problem: runs start again.
    asyncio.run(codex_login.clear_problem())
    assert asyncio.run(CODEX.login_problem(LOGIN)) is None and _home(_run(tmp_path)[1]) == home


def test_an_api_key_home_without_a_link_is_no_fork(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  settings_table: dict[str, str]) -> None:
    _operator(tmp_path, monkeypatch)
    _, api_command = _run(tmp_path, API_KEY)
    launch, command = _run(tmp_path, prompt="ANOTHER")
    assert not os.path.lexists(_home(api_command) / "auth.json")
    assert asyncio.run(CODEX.after_run(launch, command)) == [] and settings_table == {}


def test_without_the_login_file_a_login_run_does_not_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         settings_table: dict[str, str]) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "logged-out"))
    problem = asyncio.run(CODEX.login_problem(LOGIN))
    assert problem is not None and "Codex is not logged in on this machine" in problem
    assert "codex login" in problem


def test_a_forked_login_is_a_warning_in_the_tasks_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          settings_table: dict[str, str], agent_db: Any) -> None:
    _operator(tmp_path, monkeypatch)
    launch, command = _run(tmp_path)

    async def _record() -> None:
        recorder = ClaudeRunRecorder(agent_id="worker-1", task_id="task-1", command=command, cwd=str(tmp_path),
                                     input_text="work", launch=launch)
        await recorder.start()
        _copy(_home(command))
        await recorder.finish(output_text="done", result_event=None)

    asyncio.run(_record())
    (call,) = agent_db.insert_activity.await_args_list
    assert (call.kwargs["task_id"], call.kwargs["worker"], call.kwargs["kind"]) == ("task-1", "worker-1", "error")
    assert call.kwargs["summary"].startswith("Warning: Codex login unavailable:")


def test_the_server_start_catches_a_fork_no_run_saw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    settings_table: dict[str, str]) -> None:
    login = _operator(tmp_path, monkeypatch)
    _, command = _run(tmp_path)
    _copy(_home(command))
    problem = asyncio.run(codex_login.inspect_at_start(tmp_path / "rt"))
    assert problem is not None and "had become a copy" in problem and login.read_text() == "stand-in login"
    assert asyncio.run(codex_login.inspect_at_start(tmp_path / "rt")) is None  # put right, so reported once


# --- The self-check ---


def _status(state: LoginState, detail: str = "") -> Any:
    return lambda: codex_login.LoginStatus(state, detail or state.value)


@pytest.mark.parametrize(("profiles", "problem", "state", "status"), [
    (None, None, LoginState.CHATGPT, SKIP),
    (0, None, LoginState.CHATGPT, SKIP),
    (1, {"detail": "a copy", "at": "2026-10-14T10:00:00+00:00"}, LoginState.CHATGPT, FAIL),
    (1, None, LoginState.CHATGPT, OK),
    (2, None, LoginState.NOT_LOGGED_IN, FAIL),
    (1, None, LoginState.OTHER_METHOD, FAIL),
    (1, None, LoginState.UNKNOWN, WARN),
], ids=["no-database", "no-profile", "forked", "logged-in", "not-logged-in", "api-key", "unknown"])
def test_the_codex_login_check_is_skipped_without_a_profile_and_fails_on_a_fork(
    profiles: int | None, problem: dict[str, str] | None, state: LoginState, status: str,
) -> None:
    probed: list[bool] = []

    def probe() -> codex_login.LoginStatus:
        probed.append(True)
        return _status(state)()

    check = check_codex_login(profiles, problem, probe)
    assert (check.id, check.status) == ("codex_login", status)
    assert bool(probed) == (bool(profiles) and problem is None)  # `codex login status` only when it matters
    if problem is not None:
        assert "a copy" in check.detail and "codex login status" in check.fix
