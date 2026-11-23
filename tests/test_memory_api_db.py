"""The operator-only ``/api/v1/memory/*`` operations (memory-write-path RFC, section 9.3): reading candidates, the
memory_write setting and the preflight check over a real loopback server; API clients get none of them, and the API
gains no tool."""

import json
import subprocess

import pytest

from looper.agents import api_v1, vault
from tests import memory_support as sup
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
_WRITES = ("confirm", "confirm_many", "reject", "edit", "retry", "revive", "delete")
_OPERATIONS = ("list", "get", "settings_get", "settings_put", "check", *_WRITES)


def test_the_operations_are_the_operators_and_add_no_tool():
    found = {op.operation_id: op for op in api_v1.operations() if op.operation_id.startswith("memory.")}
    assert set(found) == {f"memory.{name}" for name in _OPERATIONS}
    for op in found.values():
        assert op.operator_only and op.tool is None and not op.hand and len(op.summary) <= 120
        assert op.description.startswith(op.summary)
        assert op.risk == ("write" if op.operation_id.rsplit(".", 1)[1] in (*_WRITES, "settings_put") else "read")
        if op.operation_id.rsplit(".", 1)[1] in _WRITES:
            assert not op.audit_operator  # the candidate's erasable events own this trail


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


def test_confirm_batch_has_independent_results_and_cannot_accept_cli_identity(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        numbers = [await sup.candidate(pool, statement=f"Preference {number}") for number in range(2)]
        revisions = [await sup.revise_current(pool, number, settings) for number in numbers]
        items = [{"candidate": f"M-{number}", "content_hash": revision["content_hash"]}
                 for number, revision in zip(numbers, revisions)]
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.confirm", {**items[0], "via": "cli"})
            assert status == 400, answer
            status, answer = await call("memory.confirm", {**items[0], "content_hash": "0" * 64})
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            status, answer = await call("memory.confirm_many", {"items": [
                items[0], {**items[1], "content_hash": "0" * 64}]})
            assert status == 200, answer
            results = answer["result"]["items"]
            assert [item["ok"] for item in results] == [True, False]
            assert results[0]["result"]["candidate"]["state"] == "confirmed"
            assert results[1]["error"]["code"] == "conflict"
            status, answer = await call("memory.confirm_many", {"items": []})
            assert status == 400, answer
            status, answer = await call("memory.reject", {"candidate": items[1]["candidate"], "reason": "not useful"})
            assert status == 200 and answer["result"]["candidate"]["state"] == "rejected", answer
            status, answer = await call("memory.delete", {"candidate": items[1]["candidate"]})
            assert status == 200 and answer["result"]["deleted"], answer

    run_db(check, monkeypatch, timeout=60)


def test_candidate_edits_use_only_their_erasable_event_trail(monkeypatch, tmp_path):
    from looper.db import memory as db_memory

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        number = await sup.candidate(pool)
        revision = await sup.revise_current(pool, number, settings)
        candidate = f"M-{number}"
        async with v1_api(tmp_path, pool) as call:
            changes = [{**sup.NOTE, "content": sup.NOTE["content"] + "Private synthetic correction.\n"}, sup.INDEX]
            status, answer = await call("memory.edit", {"candidate": candidate,
                                                        "content_hash": revision["content_hash"],
                                                        "changes": changes})
            assert status == 200 and answer["result"]["candidate"]["revision"] == 2, answer
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool='memory.edit'") == 0
            status, answer = await call("memory.delete", {"candidate": candidate})
            assert status == 200 and answer["result"]["deleted"], answer
            assert await db_memory.get(number, db=pool) is None
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool='memory.delete'") == 0
            await pool.execute("DELETE FROM system_settings WHERE key='memory_write'")
            before = await pool.fetchval("SELECT count(*) FROM manager_actions")
            status, answer = await call("memory.confirm", {"candidate": "M-9999", "content_hash": "0" * 64})
            assert status == 403, answer
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == before

    run_db(check, monkeypatch, timeout=60)


def test_memory_get_adds_a_code_made_diff_per_file_and_nothing_else(monkeypatch, tmp_path):
    from looper.agents import memory
    from looper.db import memory as db_memory

    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await sup.configure_write(repo, pool)
        number = await sup.candidate(pool)
        await sup.revise_current(pool, number, settings)
        candidate = f"M-{number}"
        recorded = memory.detail_view(await db_memory.get(number, db=pool))
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.get", {"candidate": candidate})
            assert status == 200, answer
            shown = answer["result"]["candidate"]
            diffs = shown["current"].pop("diffs")
            assert shown["current"].pop("diffs_problem") is None
            # Every other field is what the function the page used before returns.
            assert shown == json.loads(json.dumps(recorded))
            assert set(diffs) == {"mem/prefs/weekly.md", "mem/prefs/_index.md"}
            assert diffs["mem/prefs/weekly.md"].startswith("--- /dev/null\n+++ b/mem/prefs/weekly.md\n")
            assert "+Conclusion first." in diffs["mem/prefs/weekly.md"]
            assert "+- [[weekly]] conclusion first" in diffs["mem/prefs/_index.md"]
            assert "--- a/mem/prefs/_index.md" in diffs["mem/prefs/_index.md"]
            # The diff is made from the vault file as it is now: an anchor that went missing is said in words.
            (repo / "mem/prefs/_index.md").write_text("no heading any more\n")
            status, answer = await call("memory.get", {"candidate": candidate})
            current = answer["result"]["candidate"]["current"]
            assert current["diffs"] == {} and "anchor_missing" in current["diffs_problem"]
            # A missing setting is said in words too.
            await pool.execute("DELETE FROM system_settings WHERE key='memory_write'")
            status, answer = await call("memory.get", {"candidate": candidate})
            current = answer["result"]["candidate"]["current"]
            assert current["diffs"] == {} and "not configured" in current["diffs_problem"]

    run_db(check, monkeypatch, timeout=60)
