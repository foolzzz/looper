"""The knowledge vault's HTTP routes and the Manager's confirmed settings action, without a database."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, projects, vault
from looper.agents.ops import Call, ErrorCode, OpError, Turn, check_arguments
from looper.agents.ops import settings
from looper.agents.ops.service import CONFIRM, ManagerOps, Platform
from looper.agents.tiers.task import TaskRouter
from looper.db import manager_actions
from tests.test_projects_api import _call, _server

pytestmark = pytest.mark.usefixtures("current_turn")


@pytest.fixture
def stored_vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The settings table in memory; all validation, serialization and readback are the real vault's."""
    rows: dict[str, str] = {}

    async def get_settings(keys: Sequence[str]) -> dict[str, str]:
        return {key: rows[key] for key in keys if key in rows}

    async def put_settings(values: Mapping[str, str | None]) -> None:
        for key, value in values.items():
            if value is None:
                rows.pop(key, None)
            else:
                rows[key] = value

    monkeypatch.setattr(vault.system_settings, "get_settings", AsyncMock(side_effect=get_settings))
    monkeypatch.setattr(vault.system_settings, "put_settings", AsyncMock(side_effect=put_settings))
    monkeypatch.setattr(vault, "_git_root", AsyncMock(return_value=None))
    return rows


@pytest.mark.parametrize("clear", [
    {"path": None},
    {"path": None, "writable": [], "rules_file": None},
    {"path": None, "writable": None, "rules_file": ""},
])
def test_vault_settings_are_saved_read_back_and_cleared_without_a_database(
    tmp_path: Path, stored_vault: dict[str, str], clear: dict[str, Any],
) -> None:
    (tmp_path / "codex-mem").mkdir()
    (tmp_path / "rules.md").write_text("Keep notes short.", encoding="utf-8")
    stored_vault["unrelated"] = "keep this"

    async def test() -> None:
        assert await vault.load() == (None, None)
        assert await vault.settings_view() == _view()
        view = await vault.update_settings({"path": str(tmp_path), "writable": [" codex-mem/ "],
                                            "rules_file": "rules.md"})
        expected = {"path": str(tmp_path.resolve()), "writable": ["codex-mem"], "rules_file": "rules.md"}
        assert json.loads(stored_vault[vault.SETTING_KEY]) == expected
        assert view == {"configured": True, **expected, "git": False, "problem": None}
        assert await vault.settings_view() == view
        config, problem = await vault.load()
        assert config == vault.VaultConfig(tmp_path.resolve(), ("codex-mem",), "rules.md") and problem is None
        assert await vault.active() == config
        assert await vault.update_settings(clear) == _view()
        assert stored_vault == {"unrelated": "keep this"}
        assert await vault.load() == (None, None)

    asyncio.run(test())


@pytest.mark.parametrize("raw", ["{", "[]", "null", '"not an object"', "27"])
def test_invalid_saved_json_disables_the_vault_with_a_problem(
    stored_vault: dict[str, str], raw: str,
) -> None:
    stored_vault[vault.SETTING_KEY] = raw

    async def test() -> None:
        config, problem = await vault.load()
        assert config is None and problem and problem.startswith("the knowledge vault setting no longer holds: ")
        view = await vault.settings_view()
        assert view == {**_view(), "problem": problem}
        assert await vault.active() is None
        assert stored_vault[vault.SETTING_KEY] == raw

    asyncio.run(test())


def test_a_removed_vault_is_inactive_but_its_saved_values_stay_visible(
    tmp_path: Path, stored_vault: dict[str, str],
) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    writable = root / "codex-mem"
    writable.mkdir()
    rules = root / "rules.md"
    rules.write_text("Update existing notes.", encoding="utf-8")

    async def test() -> None:
        saved = {"path": str(root.resolve()), "writable": ["codex-mem"], "rules_file": "rules.md"}
        assert (await vault.update_settings(saved))["configured"] is True
        raw = stored_vault[vault.SETTING_KEY]
        rules.unlink()
        writable.rmdir()
        root.rmdir()
        config, problem = await vault.load()
        assert config is None and problem and "does not exist" in problem
        assert await vault.settings_view() == {"configured": False, **saved, "git": False, "problem": problem}
        assert await vault.active() is None
        assert stored_vault[vault.SETTING_KEY] == raw

    asyncio.run(test())


@pytest.mark.parametrize("body", [
    {"path": "relative"},
    {"path": None, "writable": ["codex-mem"]},
    {"path": None, "extra": None},
])
def test_an_invalid_vault_update_leaves_the_saved_setting_untouched(
    tmp_path: Path, stored_vault: dict[str, str], body: dict[str, Any],
) -> None:
    async def test() -> None:
        before = await vault.update_settings({"path": str(tmp_path)})
        raw = stored_vault[vault.SETTING_KEY]
        vault.system_settings.put_settings.reset_mock()
        with pytest.raises(projects.RequestError) as caught:
            await vault.update_settings(body)
        assert caught.value.status == projects.HTTP_400
        vault.system_settings.put_settings.assert_not_awaited()
        assert stored_vault[vault.SETTING_KEY] == raw
        assert await vault.settings_view() == before

    asyncio.run(test())


@pytest.mark.parametrize("git", [False, True])
def test_the_vault_settings_view_reports_whether_its_path_is_in_git(
    tmp_path: Path, stored_vault: dict[str, str], monkeypatch: pytest.MonkeyPatch, git: bool,
) -> None:
    monkeypatch.setattr(vault, "_git_root", AsyncMock(return_value=tmp_path if git else None))
    stored_vault[vault.SETTING_KEY] = json.dumps({"path": str(tmp_path), "writable": [], "rules_file": None})
    view = asyncio.run(vault.settings_view())
    assert view["configured"] is True and view["git"] is git


def _setting_call(values: Any = None, *, include: bool = True) -> Call:
    arguments = {"action": "knowledge_vault"}
    if include:
        arguments["knowledge_vault"] = values
    return Call(arguments, Turn("web", "A", message_id="msg-1"), SimpleNamespace())


def _view(path: str | None = None) -> dict[str, Any]:
    return {"configured": path is not None, "path": path, "writable": [], "rules_file": None,
            "git": False, "problem": None}


def test_the_settings_schema_accepts_the_vault_action() -> None:
    values = {"action": "knowledge_vault", "knowledge_vault": {"path": None}}
    assert check_arguments(settings.TOOLS[0].schema, values) == values


@pytest.mark.parametrize("values", [None, {}])
def test_the_vault_action_requires_a_nonempty_object(values: Any) -> None:
    with pytest.raises(OpError) as caught:
        asyncio.run(settings._confirming(_setting_call(values)))
    assert caught.value.code is ErrorCode.INVALID


def test_the_vault_action_requires_its_object() -> None:
    with pytest.raises(OpError) as caught:
        asyncio.run(settings._confirming(_setting_call(include=False)))
    assert caught.value.code is ErrorCode.INVALID


def test_a_bad_vault_is_rejected_before_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    view = AsyncMock()
    monkeypatch.setattr(vault, "settings_view", view)
    with pytest.raises(OpError) as caught:
        asyncio.run(settings._confirming(_setting_call({"path": "relative"})))
    assert caught.value.code is ErrorCode.INVALID
    view.assert_not_awaited()


@pytest.mark.parametrize("clear", [False, True])
def test_vault_changes_always_show_the_before_and_after_for_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear: bool,
) -> None:
    (tmp_path / "codex-mem").mkdir()
    (tmp_path / "rules.md").write_text("Short notes.", encoding="utf-8")
    monkeypatch.setattr(vault, "settings_view", AsyncMock(return_value=_view()))
    values = {"path": None} if clear else {"path": str(tmp_path), "writable": ["codex-mem"],
                                          "rules_file": "rules.md"}
    text = asyncio.run(settings._confirming(_setting_call(values)))
    assert text and "要修改知识库设置吗？" in text
    assert "- 目录：未配置 → " in text
    assert "- 可写目录：无 → " in text and "- 规则文件：无 → " in text
    assert "任务的 agent 可以读这个目录里的全部笔记" in text and "每次写入是一次 git 提交" in text
    if not clear:
        assert str(tmp_path.resolve()) in text and "codex-mem" in text and "rules.md" in text


@pytest.mark.parametrize(("writable", "shown"), [(None, "无"), ([7], "7"), ("broken", "broken")])
def test_an_invalid_stored_vault_can_still_be_cleared(
    monkeypatch: pytest.MonkeyPatch, writable: Any, shown: str,
) -> None:
    monkeypatch.setattr(vault, "settings_view", AsyncMock(return_value={**_view(), "writable": writable,
                                                                       "problem": "invalid stored setting"}))
    text = asyncio.run(settings._confirming(_setting_call({"path": None})))
    assert text and f"- 可写目录：{shown} → 无" in text


def test_the_confirmed_vault_action_uses_the_dashboard_update(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view()
    update = AsyncMock(return_value=view)
    monkeypatch.setattr(vault, "update_settings", update)
    assert asyncio.run(settings._settings(_setting_call({"path": None}))) == {"knowledge_vault": view}
    update.assert_awaited_once_with({"path": None})


def test_the_manager_waits_for_the_next_user_turn_to_change_the_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))
    monkeypatch.setattr(vault, "settings_view", AsyncMock(return_value=_view()))
    update = AsyncMock(return_value=_view())
    monkeypatch.setattr(vault, "update_settings", update)
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                     tools=(*settings.TOOLS, CONFIRM))

    async def test() -> None:
        first = Turn("web", "A", message_id="msg-1")
        ops.begin_turn(first)
        answer = await ops.call("looper_settings", {"action": "knowledge_vault", "knowledge_vault": {"path": None}})
        assert answer["status"] == "needs_confirmation"
        update.assert_not_awaited()
        assert first.blocks and "知识库设置" in first.blocks[0]
        ops.end_turn(first)
        second = Turn("web", "A", message_id="msg-2")
        ops.begin_turn(second)
        confirmed = await ops.call("looper_confirm", {"confirmation_id": answer["confirmation_id"]})
        assert confirmed == {"status": "ok", "result": {"knowledge_vault": _view()}}
        update.assert_awaited_once_with({"path": None})

    asyncio.run(test())


def test_the_vault_action_preserves_dashboard_validation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "update_settings", AsyncMock(side_effect=projects.RequestError(projects.HTTP_400,
                                                                                            "bad vault")))
    with pytest.raises(OpError) as caught:
        asyncio.run(settings._settings(_setting_call({"path": "relative"})))
    assert caught.value.code is ErrorCode.INVALID and "bad vault" in str(caught.value)


def test_the_global_settings_view_includes_the_vault_without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.db_profiles, "list_profiles", AsyncMock(return_value=[]))
    monkeypatch.setattr(settings.db_roles, "list_configs", AsyncMock(return_value=[]))
    monkeypatch.setattr(settings, "_read", AsyncMock(return_value={"system": {"values": {}}, "roles": {}}))
    monkeypatch.setattr(settings.scheduler_settings, "settings_view", AsyncMock(return_value={}))
    view = {**_view(), "problem": "folder vanished"}
    monkeypatch.setattr(vault, "settings_view", AsyncMock(return_value=view))
    call = Call({"action": "view"}, Turn("web", "A"), SimpleNamespace())
    result = asyncio.run(settings._settings(call))
    assert result["knowledge_vault"] == {key: value for key, value in view.items() if key != "git"}


def test_the_vault_http_routes_use_the_settings_functions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The domain may already be loaded before services are replaced, as it is in a full test run.
    api_v1.document()
    view = _view()
    monkeypatch.setattr(vault, "settings_view", AsyncMock(return_value=view))
    update = AsyncMock(return_value=view)
    monkeypatch.setattr(vault, "update_settings", update)
    server = _server(TaskRouter(runtime_root=tmp_path))
    assert _call("POST", "/api/v1/knowledge_vault/get", {}, server=server) == (200, view)
    assert _call("POST", "/api/v1/knowledge_vault/update", {"path": None}, server=server) == (200, view)
    update.assert_awaited_once_with({"path": None})


def test_the_vault_http_route_requires_a_json_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    update = AsyncMock()
    monkeypatch.setattr(vault, "update_settings", update)
    status, body = _call("POST", "/api/v1/knowledge_vault/update", [], server=_server(TaskRouter(runtime_root=tmp_path)))
    assert status == 400 and body["error"]
    update.assert_not_awaited()
