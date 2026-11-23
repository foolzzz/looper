"""The operator-only ``/api/v1/memory/*`` operations (memory-write-path RFC, section 9.3): reading candidates, the
memory_write setting and the preflight check over a real loopback server; API clients get none of them, and the API
gains no tool."""

import subprocess

import pytest

from looper.agents import api_v1, vault
from tests import memory_support as sup
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
_OPERATIONS = ("list", "get", "settings_get", "settings_put", "check")


def test_the_operations_are_the_operators_and_add_no_tool():
    found = {op.operation_id: op for op in api_v1.operations() if op.operation_id.startswith("memory.")}
    assert set(found) == {f"memory.{name}" for name in _OPERATIONS}
    for op in found.values():
        assert op.operator_only and op.tool is None and not op.hand and len(op.summary) <= 120
        assert op.risk == ("write" if op.operation_id == "memory.settings_put" else "read")


def test_the_operator_lists_and_reads_candidates(monkeypatch, tmp_path):
    async def check(pool):
        first = await sup.candidate(pool)
        await sup.revise(pool, first)
        second = await sup.candidate(pool, kind="decision", statement="Ship on Friday")
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.list")
            assert status == 200, answer
            assert [item["id"] for item in answer["result"]["candidates"]] == [f"M-{second:04d}", f"M-{first:04d}"]
            listed = answer["result"]["candidates"][1]
            assert (listed["state"], listed["kind"], listed["sources_total"], listed["note_path"]) == (
                "pending", "preference", 1, "mem/prefs/weekly.md")
            status, answer = await call("memory.list", {"state": "pending", "limit": 1})
            assert [item["id"] for item in answer["result"]["candidates"]] == [f"M-{first:04d}"]
            status, answer = await call("memory.get", {"candidate": f"m-{first}"})
            assert status == 200, answer
            shown = answer["result"]["candidate"]
            assert shown["current"]["changes"] == [sup.NOTE, sup.INDEX]
            assert [event["action"] for event in shown["events"]] == ["propose", "draft"]
            assert shown["sources"] == [{"kind": "manager_turn", "source_class": "operator",
                                         "quote": "from now on, conclusion first", "channel": "lark",
                                         "chat": "ou_operator_synthetic", "message_id": "om_synthetic",
                                         "said_at": shown["sources"][0]["said_at"], "task_id": None, "lead": None}]
            status, answer = await call("memory.get", {"candidate": "M-999999"})
            assert status == 404 and answer["error"]["code"] == "not_found"
            for body in ({"candidate": "42"}, {"state": "lost"}):
                status, answer = await call("memory.get" if "candidate" in body else "memory.list", body)
                assert status == 400 and answer["error"]["code"] == "invalid", answer

    run_db(check, monkeypatch, timeout=60)


def test_the_operator_saves_reads_checks_and_clears_the_setting(monkeypatch, tmp_path):
    repo = tmp_path / "vault"
    (repo / "mem" / "prefs").mkdir(parents=True)
    (repo / "mem" / "prefs" / "_index.md").write_text("## Index\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

    async def check(pool):
        await vault.update_settings({"path": str(repo)})
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.settings_get")
            assert status == 200 and answer["result"] == {"configured": False, "enabled": False, "settings": None,
                                                          "problem": None}
            status, answer = await call("memory.settings_put", {"root": "mem", "targets": {"wish": {"shape": "note"}}})
            assert status == 400 and "unknown kinds" in answer["error"]["message"]
            status, answer = await call("memory.settings_put", {
                "root": "mem", "enabled": True, "sources": {"manager": True},
                "targets": {"preference": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"}}})
            assert status == 200, answer
            assert answer["result"]["enabled"] and answer["result"]["settings"]["branch"] == "main"
            status, answer = await call("memory.check")
            assert status == 200 and answer["result"]["ok"], answer
            status, answer = await call("memory.settings_put", {"root": None})
            assert status == 200 and answer["result"]["configured"] is False

    run_db(check, monkeypatch, timeout=60)


@pytest.mark.parametrize("action", _OPERATIONS)
def test_an_api_client_can_use_no_memory_operation(monkeypatch, tmp_path, action):
    async def check(pool):
        body = {"candidate": "M-1"} if action == "get" else {}
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call(f"memory.{action}", body, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden", answer

    run_db(check, monkeypatch, timeout=30)
