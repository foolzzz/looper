"""The profile source is operator-only and stays out of chat settings and the Manager."""

import asyncio
import hashlib
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

_TOOLS = "733a09157b22ebf31e591e117c55a04b23c41bf70e49a06108082ab045d4922c"


@pytest.fixture
def store(monkeypatch):
    saved: dict[str, str] = {}
    block: dict[str, object] = {}
    writes: list[tuple] = []

    async def read(keys):
        return {key: saved[key] for key in keys if key in saved}

    async def put_source(key, path):
        writes.append(("put", key, path))
        saved[key] = path

    async def clear_source(key):
        writes.append(("clear", key))
        saved.pop(key, None)
        block.clear()

    async def retain_or_replace(*, text, body):
        writes.append(("retain", text))
        if block.get("text") == text and block.get("body") == body:
            return block["version"]
        version = 1 if "version" not in block else block["version"] + 1
        block.update(text=text, body=body, version=version)
        return version

    monkeypatch.setattr(operator_profile.db_settings, "get_settings", read)
    monkeypatch.setattr(operator_profile.blocks, "put_source", put_source)
    monkeypatch.setattr(operator_profile.blocks, "clear_source", clear_source)
    monkeypatch.setattr(operator_profile.blocks, "retain_or_replace", retain_or_replace)
    return saved, block, writes


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "关于我.md").write_text("## 名字\nAda\n", encoding="utf-8")
    return root


def _arm(saved: dict, folder: Path) -> None:
    saved[vault.SETTING_KEY] = json.dumps(
        {"path": str(folder.resolve()), "writable": [], "rules_file": None})


def test_descriptions_levels_and_cli_commands_are_operator_only_and_inside() -> None:
    by_id = {op.operation_id: op for op in profile_api.OPERATIONS}
    assert set(by_id) == {"profile.get", "profile_source.get", "profile_source.update"}
    for operation in by_id.values():
        assert operation.description.startswith(operation.summary)
        assert len(operation.summary) <= 120
        assert operation.operator_only and operation.tool is None
        assert operation.level is ActionLevel.INSIDE
    assert by_id["profile.get"].risk == "read" and by_id["profile_source.get"].risk == "read"
    assert by_id["profile_source.update"].risk == "high" and by_id["profile_source.update"].confirms
    commands = {(head, tail): item.operator_only
                for head, tail, _, _, item in loopercli.generated_operations(api_v1.document())}
    assert commands[("profile", "get")] is True
    assert commands[("profile-source", "get")] is True
    assert commands[("profile-source", "update")] is True


def test_off_leaves_the_manager_tool_list_and_chat_settings_unchanged() -> None:
    names = "\n".join(tool.name for tool in MANAGER_TOOLS)
    assert hashlib.sha256(names.encode()).hexdigest() == _TOOLS
    blob = "\n".join(tool.name + json.dumps(tool.schema) + tool.description for tool in MANAGER_TOOLS)
    assert "manager.profile.source" not in blob and "profile.get" not in blob
    manager = (Path(__file__).parents[1] / "looper/agents/tiers/manager.py").read_text(encoding="utf-8")
    assert "operator_profile" not in manager and "profile_blocks" not in manager
    assert "manager.profile.source" not in manager
    assert chat_settings.FIELDS == ("stop_enabled", "completion_turns", "research_order", "ledger_enabled")
    with pytest.raises(ValueError, match="unknown setting"):
        chat_settings.checked({operator_profile.SOURCE_KEY: "/vault"})


def test_an_unset_read_compiles_nothing(store) -> None:
    saved, block, writes = store

    async def check():
        assert await operator_profile.source_view() == {"source": None}
        view = await operator_profile.profile_view()
        assert view["configured"] is False and view["version"] is None and view["text"] == ""
        assert view["cap"] == 1500 and view["sources"] == [] and block == {} and writes == []
        saved[operator_profile.SOURCE_KEY] = ""
        with pytest.raises(operator_profile.ProfileError, match="absolute directory"):
            await operator_profile.profile_view()
        saved[operator_profile.SOURCE_KEY] = "notes"
        with pytest.raises(operator_profile.ProfileError, match="absolute directory"):
            await operator_profile.profile_view()
        assert await operator_profile.source_view() == {"source": "notes"}
        assert writes == []

    asyncio.run(check())


def test_null_clears_and_a_relative_path_is_not_stored(store, tmp_path: Path) -> None:
    saved, block, writes = store
    folder = _vault(tmp_path)
    _arm(saved, folder)

    async def check():
        stored = await operator_profile.update_source(str(folder))
        assert stored == {"source": str(folder.resolve())}
        assert writes == [("put", operator_profile.SOURCE_KEY, str(folder.resolve()))]
        block.update(version=3, text="old")
        await operator_profile.update_source(str(folder))
        assert [item[0] for item in writes] == ["put", "put"]
        assert block["version"] == 3
        cleared = await operator_profile.update_source(None)
        assert cleared == {"source": None} and set(saved) == {vault.SETTING_KEY} and block == {}
        with pytest.raises(operator_profile.ProfileError, match="absolute directory"):
            await operator_profile.update_source("vault")
        assert set(saved) == {vault.SETTING_KEY} and writes[-1][0] == "clear"

    asyncio.run(check())


def test_update_impact_names_the_folder_and_stores_nothing(store, tmp_path: Path) -> None:
    operation = api_v1.find("POST", "/api/v1/profile_source/update")
    folder = _vault(tmp_path)
    shown = str(folder.resolve())
    _arm(store[0], folder)

    async def check():
        domain = api_v1.domain_for(operation)
        impact = await domain.impact(None, operation, {"source": None}, api_v1.OPERATOR, {})
        assert impact == (
            "Set manager.profile.source to unset (nothing compiled)? This stores the folder. "
            "Nothing is injected into the Manager.")
        named = await domain.impact(None, operation, {"source": str(folder)}, api_v1.OPERATOR, {})
        assert named == (
            f"Set manager.profile.source to {shown}? This stores the folder. "
            "Nothing is injected into the Manager.")
        with pytest.raises(OpError, match="absolute directory"):
            await domain.impact(None, operation, {"source": "vault"}, api_v1.OPERATOR, {})
        assert operator_profile.SOURCE_KEY not in store[0] and store[2] == []

    asyncio.run(check())


@pytest.mark.parametrize("operation_id", ["profile.get", "profile_source.get", "profile_source.update"])
@pytest.mark.usefixtures("clients")
def test_an_agent_token_cannot_read_or_change_the_profile(tmp_path, monkeypatch, store, operation_id):
    operation = next(op for op in api_v1.operations() if op.operation_id == operation_id)
    run = AsyncMock(side_effect=AssertionError("agent token reached the profile"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)
    body = {"source": str(tmp_path)} if operation_id == "profile_source.update" else {}

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN),
                                            json.dumps(body))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            run.assert_not_awaited()
            assert store[2] == []

    asyncio.run(check())


@pytest.mark.usefixtures("clients")
def test_the_operator_reads_and_clears_the_profile_through_v1(tmp_path, monkeypatch, store):
    from looper.db import manager_actions

    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))
    folder = _vault(tmp_path)
    shown = str(folder.resolve())
    _arm(store[0], folder)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())

            async def post(path, body):
                status, _, answer = await _send(server, f"POST {path}", _bearer(TOKEN), json.dumps(body))
                assert status == 200 and answer["status"] == "ok", answer
                return answer["result"]

            assert await post("/api/v1/profile_source/get", {}) == {"source": None}
            unset = await post("/api/v1/profile/get", {})
            assert unset["configured"] is False and unset["version"] is None and "retain" not in [
                item[0] for item in store[2]]
            assert await post("/api/v1/profile_source/update", {"source": str(folder)}) == {"source": shown}
            compiled = await post("/api/v1/profile/get", {})
            assert compiled["configured"] is True and compiled["version"] == 1
            assert compiled["fields"]["name"] == {
                "value": "Ada", "path": "关于我.md", "heading": "名字", "line": 1}
            assert compiled["text"] == "Name: Ada"
            again = await post("/api/v1/profile/get", {})
            assert again["version"] == 1
            assert await post("/api/v1/profile_source/update", {"source": None}) == {"source": None}
            cleared = await post("/api/v1/profile/get", {})
            assert cleared["configured"] is False and store[1] == {}
            status, _, answer = await _send(server, "POST /api/v1/profile_source/update", _bearer(TOKEN),
                                            json.dumps({"source": "vault"}))
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert set(store[0]) == {vault.SETTING_KEY}

    asyncio.run(check())


def test_the_source_must_resolve_inside_the_knowledge_vault(store, tmp_path: Path) -> None:
    saved, _block, writes = store
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    notes = vault_root / "notes"
    notes.mkdir()
    (notes / "关于我.md").write_text("## 名字\nAda\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "关于我.md").write_text("## 名字\nLeak\n", encoding="utf-8")

    async def check():
        with pytest.raises(operator_profile.ProfileError, match="knowledge vault is not configured") as missing:
            await operator_profile.update_source(str(notes))
        assert missing.value.reason == "vault_unconfigured"
        assert [item[0] for item in writes] == []
        _arm(saved, vault_root)

        stored = await operator_profile.update_source(str(notes))
        assert stored == {"source": str(notes.resolve())}
        link = vault_root / "alias"
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(operator_profile.ProfileError, match="resolves outside") as escaped:
            await operator_profile.update_source(str(link))
        assert escaped.value.reason == "outside_vault"
        assert "Leak" not in str(escaped.value)
        assert all(item[0] != "retain" for item in writes)

        jump = tmp_path / "jump"
        jump.symlink_to(notes, target_is_directory=True)
        assert await operator_profile.update_source(str(jump)) == {"source": str(notes.resolve())}

        kept = vault_root / "kept"
        notes.rename(kept)
        notes.symlink_to(outside, target_is_directory=True)
        with pytest.raises(operator_profile.ProfileError, match="resolves outside") as reread:
            await operator_profile.profile_view()
        assert reread.value.reason == "outside_vault"
        assert "Leak" not in repr(writes)

    asyncio.run(check())
