"""Operator retrieval shares the Manager's answers, switches and vault boundary, without chat side effects."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import api_v1, untrusted, vault
from looper.agents.ops import Call, ErrorCode, OpError, Turn
from looper.agents.ops import leads as ops_leads
from looper.agents.ops import manager_vault
from looper.agents.v1_domains import manager_retrieval as retrieval
from tests import leads_support as leads
from tests import subscription_support as stream
from tests.db_support import run_db, v1_api

NOTE = "Atlas 服务的部署端口是 47219"


@pytest.fixture(autouse=True)
def nothing_seen():
    ops_leads._SEEN.clear()
    yield
    ops_leads._SEEN.clear()


async def setting(tmp_path: Path, *, manager_read: bool = True) -> Path:
    root = tmp_path / "kb"
    (root / "projects").mkdir(parents=True)
    (root / "projects" / "atlas.md").write_text(NOTE, encoding="utf-8")
    await vault.update_settings({"path": str(root), "writable": [], "rules_file": None,
                                 "manager_read": manager_read})
    return root


async def run(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    operation = next(op for op in retrieval.OPERATIONS if op.operation_id == "manager_retrieval." + name)
    return await retrieval.run(None, operation, arguments, api_v1.OPERATOR, {})


@pytest.mark.parametrize("name,arguments", [
    ("vault_search", {"query": "Atlas 部署端口"}),
    ("vault_read", {"path": "projects/atlas.md"}),
])
def test_vault_ops_answer_what_the_manager_tools_answer(monkeypatch, tmp_path, name, arguments):
    async def test(pool):
        await setting(tmp_path)
        answer = await run(name, arguments)
        assert ops_leads._SEEN == {}
        call = Call(arguments, Turn(channel="web", chat="synthetic-chat", message_id="m-1"), None)
        manager = manager_vault._search if name == "vault_search" else manager_vault._read
        assert answer == await manager(call)
        assert NOTE in json.dumps(answer, ensure_ascii=False)
        assert ops_leads._SEEN  # Manager still remembers its answer for group protection.
    run_db(test, monkeypatch)


@pytest.mark.parametrize("state", ["absent", "off", "gone"])
@pytest.mark.parametrize("name,arguments", [
    ("vault_search", {"query": "Atlas"}), ("vault_read", {"path": "projects/atlas.md"}),
])
def test_vault_ops_follow_manager_read(monkeypatch, tmp_path, state, name, arguments):
    async def test(pool):
        if state != "absent":
            root = await setting(tmp_path, manager_read=state != "off")
            if state == "gone":
                (root / "projects" / "atlas.md").unlink()
                (root / "projects").rmdir()
                root.rmdir()
        with pytest.raises(OpError) as caught:
            await run(name, arguments)
        assert caught.value.code == ErrorCode.CONFLICT
        assert str(caught.value).startswith("the Manager does not read the knowledge vault now")
    run_db(test, monkeypatch)


def test_vault_ops_stay_inside_the_vault_root(monkeypatch, tmp_path):
    async def test(pool):
        root = await setting(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("ZQXJ-OUTSIDE", encoding="utf-8")
        (root / "link.md").symlink_to(outside / "secret.md")
        (root / "linkdir").symlink_to(outside, target_is_directory=True)
        # Positive control proves the search actually reads the configured vault.
        assert (await run("vault_search", {"query": "Atlas"}))["results"]
        assert (await run("vault_search", {"query": "ZQXJ-OUTSIDE"}))["results"] == []
        for path, code in [("../outside/secret.md", ErrorCode.INVALID), ("link.md", ErrorCode.FORBIDDEN),
                           ("linkdir/secret.md", ErrorCode.FORBIDDEN)]:
            with pytest.raises(OpError) as caught:
                await run("vault_read", {"path": path})
            assert caught.value.code == code
    run_db(test, monkeypatch)


@pytest.mark.parametrize("fence", [False, True])
def test_leads_op_is_the_manager_answer_with_a_label(monkeypatch, fence):
    async def test(pool):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true'), ($1, $2)",
                           untrusted.ENABLED_KEY, "true" if fence else "false")
        await stream.source(pool)
        await stream.person(pool, "ou_bobsynthetic", "Bob Example")
        for summary in ["Beacon launches Friday", "Beacon review is ready"]:
            seq = await stream.add(pool, summary, sender="ou_bobsynthetic",
                                   sent_at=datetime.now(timezone.utc) - timedelta(days=1))
            await leads.lead(pool, [seq], summary=summary)
        arguments = {"query": "Beacon"}
        expected, shown = await ops_leads.manager_answer(arguments)
        answer = await run("leads_search", arguments)
        assert len(shown) == len(answer["leads"]) == 2
        assert answer == {**expected, "leads": [{"label": retrieval.LEAD_LABEL, **lead}
                                               for lead in expected["leads"]]}
        assert '"id"' not in json.dumps(answer)
        assert ops_leads._SEEN == {}
        for item in answer["leads"]:
            assert item["summary"].startswith('<untrusted source="lead"') is fence
    run_db(test, monkeypatch)


def test_leads_op_is_refused_while_leads_are_off(monkeypatch):
    async def test(pool):
        with pytest.raises(OpError) as caught:
            await run("leads_search", {"query": "Beacon"})
        assert caught.value.code == ErrorCode.CONFLICT
        assert str(caught.value) == "message leads are turned off"
    run_db(test, monkeypatch)


def test_retrieval_ops_are_operator_only(monkeypatch, tmp_path):
    async def test(pool):
        await setting(tmp_path)
        async with v1_api(tmp_path, pool) as call:
            for name, arguments in [("vault_search", {"query": "Atlas"}),
                                    ("vault_read", {"path": "projects/atlas.md"}),
                                    ("leads_search", {"query": "Beacon"})]:
                status, body = await call("manager_retrieval." + name, arguments, client=True)
                assert status == 403
                assert body["error"]["message"] == "operator identity is required"
            status, body = await call("manager_retrieval.vault_search", {"query": "Atlas"})
            assert status == 200 and body["status"] == "ok"
            assert body["result"]["results"][0]["path"] == "projects/atlas.md"
    run_db(test, monkeypatch)


# Each model-visible item is frozen independently against the parent implementation.
@pytest.mark.parametrize("tool,digest", [
    (manager_vault.TOOLS[0], "0a6dd7ee3d04dd52fcfa57d14ad3c9be44b3aae8c1609033257d652fb0c76a62"),
    (manager_vault.TOOLS[1], "f56d67953201c1aac08e6cce6c442ca4b20be0ac9f7416c1aff07423fc9de619"),
    (ops_leads.TOOL, "67945acfd32c0a479ba0af56b58a57b9906a3d8d8f93b721421328849111ea69"),
    (ops_leads.TOOL_FULLTEXT, "5d532a16b1d7ce1055b4f3b0ab59c77d4ce76504635be382b7611097670039b1"),
])
def test_manager_tool_listings_are_unchanged(tool, digest):
    serialized = json.dumps(tool.listing(), ensure_ascii=False, sort_keys=True)
    assert hashlib.sha256(serialized.encode()).hexdigest() == digest


@pytest.mark.parametrize("contract,digest", [
    (manager_vault.VAULT_CONTRACT, "74b2f963f8942792b5d9243c397bf040742aa358f61bddbd8f5426194d157c01"),
    (ops_leads.LEADS_CONTRACT, "e25ebcbdaaa0675e8f69afa2fa36e51188414dbebf4adc9db0fd4bb926cb06db"),
])
def test_manager_retrieval_contracts_are_unchanged(contract, digest):
    assert hashlib.sha256(contract.encode()).hexdigest() == digest
