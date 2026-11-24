"""The profile note and resident switch are operator-only, with fail-closed profile reads."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1, chat_settings, operator_profile, vault
from looper.agents.action_levels import ActionLevel
from looper.agents.ops import OpError
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import MANAGER_TOOLS
from looper.agents.v1_domains import operator_profile as profile_api
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


@pytest.fixture
def store(monkeypatch):
    saved: dict[str, str] = {}
    block: dict[str, object] = {}
    writes: list[tuple] = []

    async def read(keys):
        return {key: saved[key] for key in keys if key in saved}

    async def put(values):
        writes.append(("settings", dict(values)))
        for key, value in values.items():
            if value is None:
                saved.pop(key, None)
            else:
                saved[key] = value

    async def retain_or_replace(*, text, body):
        writes.append(("retain", text))
        if block.get("text") == text and block.get("body") == body:
            return block["version"]
        version = int(block.get("version", 0)) + 1
        block.update(text=text, body=body, version=version)
        return version

    monkeypatch.setattr(operator_profile.db_settings, "get_settings", read)
    monkeypatch.setattr(operator_profile.db_settings, "put_settings", put)
    monkeypatch.setattr(operator_profile.blocks, "retain_or_replace", retain_or_replace)
    monkeypatch.setattr(vault, "_git_root", AsyncMock(return_value=None))
    return saved, block, writes


def _vault(tmp_path: Path, saved: dict) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "关于我.md").write_text("## 名字\nAda\n", encoding="utf-8")
    (root / "notes").mkdir()
    saved[vault.SETTING_KEY] = json.dumps({
        "path": str(root.resolve()), "writable": ["notes"], "rules_file": None, "manager_read": True})
    return root


def test_profile_operations_are_private_and_not_manager_tools() -> None:
    by_id = {op.operation_id: op for op in profile_api.OPERATIONS}
    assert set(by_id) == {"profile.get", "profile_source.get", "profile_source.update",
                          "profile_settings.get", "profile_settings.update"}
    for operation in by_id.values():
        assert operation.description.startswith(operation.summary)
        assert operation.operator_only and operation.tool is None and operation.level is ActionLevel.INSIDE
        if operation.operation_id.endswith("update"):
            assert operation.risk == "high" and operation.confirms
    commands = {(head, tail): item.operator_only
                for head, tail, _, _, item in loopercli.generated_operations(api_v1.document())}
    assert commands[("profile", "get")] is True
    assert commands[("profile-settings", "update")] is True
    assert not [field for field in chat_settings.FIELDS if "profile" in field]
    assert not [tool for tool in MANAGER_TOOLS if tool.name.startswith("looper_profile")]


def test_default_note_is_compiled_without_a_legacy_source_or_injection_setting(store, tmp_path) -> None:
    saved, block, writes = store

    async def check():
        assert (await operator_profile.profile_view())["configured"] is False
        assert not writes
        _vault(tmp_path, saved)
        # Legacy directory values cannot silently select another source.
        saved["manager.profile.source"] = "unrelated"
        view = await operator_profile.profile_view()
        assert view["text"] == "Name: Ada" and view["version"] == 1
        assert await operator_profile.source_view() == {"source": "关于我.md"}
        assert await operator_profile.settings_view() == {"enabled": False}
        assert await operator_profile.resident_text() == view["text"]
        assert block["version"] == 1 and len(writes) == 1  # resident reads do not update the cache

    asyncio.run(check())


def test_source_alias_preserves_the_vault_and_null_restores_the_default(store, tmp_path) -> None:
    saved, _, _ = store
    root = _vault(tmp_path, saved)
    (root / "notes/profile.md").write_text("## Name\nBee\n", encoding="utf-8")

    async def check():
        assert await operator_profile.update_source("notes/profile.md") == {"source": "notes/profile.md"}
        raw = json.loads(saved[vault.SETTING_KEY])
        assert raw["writable"] == ["notes"] and raw["manager_read"] is True
        assert await operator_profile.resident_text() == "Name: Bee"
        assert await operator_profile.update_source(None) == {"source": "关于我.md"}
        assert await operator_profile.resident_text() == "Name: Ada"
        (root / "关于我.md").unlink()
        assert await operator_profile.update_source(None) == {"source": "关于我.md"}
        assert await operator_profile.resident_text() == ""
        with pytest.raises(operator_profile.ProfileError, match="relative"):
            await operator_profile.update_source(str(root / "关于我.md"))
        with pytest.raises(operator_profile.ProfileError, match="relative Markdown"):
            operator_profile.checked_update({"source": 1})
        with pytest.raises(operator_profile.ProfileError, match="relative Markdown"):
            operator_profile.checked_update({})

    asyncio.run(check())


def test_source_confirmation_is_read_only_and_switch_default_is_off(store, tmp_path) -> None:
    saved, _, writes = store
    _vault(tmp_path, saved)

    async def check():
        operation = api_v1.find("POST", "/api/v1/profile_source/update")
        text = await profile_api.impact(None, operation, {"source": None}, api_v1.OPERATOR, {})
        assert "knowledge_vault.profile_source" in text and "关于我.md" in text
        assert not writes
        assert await operator_profile.update_enabled(True) == {"enabled": True}
        assert await operator_profile.update_enabled(False) == {"enabled": False}
        saved[operator_profile.ENABLED_KEY] = "True"
        assert await operator_profile.enabled() is False
        assert await operator_profile.update_enabled(None) == {"enabled": False}
        assert operator_profile.ENABLED_KEY not in saved
        with pytest.raises(operator_profile.ProfileError, match="enabled"):
            await operator_profile.update_enabled("true")

    asyncio.run(check())


@pytest.mark.parametrize("problem", ["missing", "outside", "malformed", "settings_unavailable"])
def test_read_failures_log_and_omit_the_profile_without_returning_cached_text(
    store, tmp_path, monkeypatch, caplog, problem,
) -> None:
    saved, block, writes = store
    root = _vault(tmp_path, saved)
    note = root / "关于我.md"

    async def check():
        assert (await operator_profile.profile_view())["text"] == "Name: Ada"  # positive read control
        if problem == "missing":
            note.unlink()
        elif problem == "outside":
            outside = tmp_path / "outside.md"
            outside.write_text("## 名字\nLeak\n", encoding="utf-8")
            note.unlink()
            note.symlink_to(outside)
        elif problem == "malformed":
            note.write_text("---\nsummary: unclosed", encoding="utf-8")
        else:
            monkeypatch.setattr(operator_profile.db_settings, "get_settings", AsyncMock(side_effect=OSError("offline")))
        assert await operator_profile.resident_text() == ""
        assert (await operator_profile.profile_view())["configured"] is False
        assert "omitting it" in caplog.text
        assert block["text"] == "Name: Ada" and len(writes) == 1

    asyncio.run(check())


@pytest.mark.parametrize("operation_id", [op.operation_id for op in profile_api.OPERATIONS])
@pytest.mark.usefixtures("clients")
def test_an_agent_token_cannot_read_or_change_the_profile(tmp_path, monkeypatch, store, operation_id):
    operation = next(op for op in api_v1.operations() if op.operation_id == operation_id)
    run = AsyncMock(side_effect=AssertionError("agent token reached the profile"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)
    body = ({"source": "关于我.md"} if operation_id == "profile_source.update" else
            {"enabled": True} if operation_id == "profile_settings.update" else {})

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN), json.dumps(body))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
            assert store[2] == []

    asyncio.run(check())


@pytest.mark.usefixtures("clients")
def test_operator_v1_reads_the_profile_and_controls_injection(tmp_path, monkeypatch, store):
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))
    _vault(tmp_path, store[0])

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())

            async def post(path, body):
                status, _, answer = await _send(server, f"POST /api/v1/{path}", _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["status"] == "ok", answer
                return answer["result"]

            assert (await post("profile/get", {}))["text"] == "Name: Ada"
            assert await post("profile_settings/get", {}) == {"enabled": False}
            assert await post("profile_settings/update", {"enabled": True}) == {"enabled": True}
            assert await post("profile_settings/update", {"enabled": None}) == {"enabled": False}
            status, _, answer = await _send(server, "POST /api/v1/profile_source/update", _bearer(TOKEN),
                                            json.dumps({"source": "missing.md"}))
            assert status == 400 and answer["error"]["code"] == "invalid"

    asyncio.run(check())
