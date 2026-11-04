"""Cursor file-store login links, failure recovery and the public doctor check."""

import asyncio
import os
from pathlib import Path

import pytest

from looper.agents import profile_registry, self_check
from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType, cursor_login
from looper.db import profile_registry as db_registry, system_settings


@pytest.fixture
def login_settings(monkeypatch):
    table = {}

    async def get_settings(keys, **kwargs):
        return {key: table[key] for key in keys if key in table}

    async def put_settings(values, **kwargs):
        for key, value in values.items():
            if value is None:
                table.pop(key, None)
            else:
                table[key] = value

    monkeypatch.setattr(system_settings, "get_settings", get_settings)
    monkeypatch.setattr(system_settings, "put_settings", put_settings)
    cursor_login.forget_probe()
    yield table
    cursor_login.forget_probe()


@pytest.mark.parametrize(("platform", "xdg", "relative"), [
    ("darwin", "ignored", ".cursor/auth.json"),
    ("linux", "config-link", "config-link/cursor/auth.json"),
    ("linux", "", ".config/cursor/auth.json"),
])
def test_operator_login_path_uses_platform_file_store(platform, xdg, relative, tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_login.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # A configured link stays in the returned path, rather than being resolved.
    (tmp_path / "real-config").mkdir()
    (tmp_path / "config-link").symlink_to(tmp_path / "real-config", target_is_directory=True)
    environ = {"XDG_CONFIG_HOME": str(tmp_path / xdg)} if xdg else {}
    assert cursor_login.login_file(environ) == tmp_path / relative


@pytest.mark.parametrize("state", ["intact", "missing-target", "copy"])
def test_cursor_login_links_and_launch_gate(state, tmp_path, monkeypatch, login_settings):
    target = tmp_path / "operator-auth.json"
    if state != "missing-target":
        target.write_text("stand-in login")
    monkeypatch.setattr(cursor_login, "login_file", lambda: target)
    cursor_dir = tmp_path / ".local" / "cursor-agent"
    homes = [cursor_dir / "runs" / "run", cursor_dir / "sessions" / "manager"]
    for home in homes:
        cursor_login.link_login(home, target)
        assert os.readlink(home / cursor_login.LOGIN_FILE) == str(target)
        if state == "copy":
            (home / cursor_login.LOGIN_FILE).unlink()
            (home / cursor_login.LOGIN_FILE).write_text("forked tokens")

    problems = cursor_login.inspect_homes(cursor_dir, target)
    assert len(problems) == (2 if state == "copy" else 0)
    if problems:
        assert all(problem.state.value == "copy" for problem in problems)
        warning = asyncio.run(cursor_login.record_problems(problems))
        assert "had become a copy" in warning
        assert all(os.readlink(home / cursor_login.LOGIN_FILE) == str(target) for home in homes)
        assert target.read_text() == "stand-in login"
        assert cursor_login.inspect_homes(cursor_dir, target) == []

    problem = asyncio.run(cursor_login.launch_problem())
    assert (problem is None) == (state == "intact")
    if problem:
        assert "AGENT_CLI_CREDENTIAL_STORE=file cursor-agent login" in problem
        if state == "missing-target":
            assert "no " + str(target) in problem
        else:
            assert "unavailable since" in problem
    view = asyncio.run(cursor_login.login_view())
    assert view["state"] == {"intact": "logged_in", "missing-target": "not_logged_in", "copy": "unavailable"}[state]
    if state == "copy":
        asyncio.run(cursor_login.clear_problem())
        assert asyncio.run(cursor_login.launch_problem()) is None
    for home in homes:
        cursor_login.unlink_login(home)
        assert not (home / cursor_login.LOGIN_FILE).is_symlink()


def test_startup_inspects_cursor_homes(tmp_path, monkeypatch, login_settings):
    target = tmp_path / "operator-auth.json"
    target.write_text("stand-in login")
    monkeypatch.setattr(cursor_login, "login_file", lambda: target)
    home = tmp_path / ".local" / "cursor-agent" / "sessions" / "manager"
    cursor_login.link_login(home, target)
    (home / cursor_login.LOGIN_FILE).unlink()
    (home / cursor_login.LOGIN_FILE).write_text("forked tokens")
    assert "had become a copy" in asyncio.run(cursor_login.inspect_at_start(tmp_path))
    assert asyncio.run(cursor_login.inspect_at_start(tmp_path)) is None


@pytest.mark.parametrize(("profiles", "state", "expected"), [
    (None, "missing", self_check.SKIP),
    (0, "missing", self_check.SKIP),
    (1, "missing", self_check.FAIL),
    (1, "recorded", self_check.FAIL),
    (1, "ok", self_check.OK),
], ids=["no-database", "no-profile", "missing-file", "recorded-problem", "ok"])
def test_cursor_doctor_check(profiles, state, expected, tmp_path, monkeypatch, login_settings):
    target = tmp_path / "auth.json"
    if state != "missing":
        target.write_text("stand-in login")
    monkeypatch.setattr(cursor_login, "login_file", lambda: target)
    if state == "recorded":
        login_settings[cursor_login.FORK_KEY] = '{"detail":"a copy","at":"2026-10-30"}'

    async def count(runtime_type):
        assert runtime_type == "cursor-agent"
        if profiles is None:
            raise ConnectionError("unreachable")
        return profiles

    monkeypatch.setattr(db_registry, "login_profile_count", count)
    check = asyncio.run(self_check.LOGIN_CHECKS["cursor_login"]())
    assert (check.id, check.label, check.status) == ("cursor_login", "Cursor login", expected)
    if expected == self_check.FAIL:
        assert "AGENT_CLI_CREDENTIAL_STORE=file cursor-agent login" in check.fix
    if state == "recorded":
        assert "a copy" in check.detail


def test_cursor_vendor_exposes_its_login_module():
    vendor = VendorType.CURSOR_LOGIN
    assert (vendor.auth_kind, vendor.oauth_runtime_type, vendor.login_protocol) == (
        AuthKind.OAUTH, "cursor-agent", ApiProtocol.OPENAI_RESPONSES)
    assert profile_registry._login_modules()["cursor-agent"] is cursor_login
