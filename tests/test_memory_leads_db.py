"""Message leads become untrusted candidates only on the operator's request, on a disposable database."""

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import memory_actions, memory_leads, memory_model as model, memory_settings, memory_write, vault
from looper.db import memory as store, message_stream
from tests import leads_support, memory_support as sup, subscription_support as stream
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


async def configure(pool, repo):
    for folder in ("decisions", "people", "projects"):
        (repo / "mem" / folder).mkdir()
        (repo / "mem" / folder / "_index.md").write_text("## Index\n")
    (repo / "mem/commitments.md").write_text("## Commitments\n")
    subprocess.run(["git", "-C", str(repo), "add", "--", "mem"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic targets"],
                   check=True, capture_output=True)
    settings = await sup.configure_write(repo, pool)
    body = settings.to_json()
    body["sources"]["leads"] = True
    body["targets"].update({
        "decision": {"shape": "note", "folders": ["decisions"], "index": "decisions/_index.md"},
        "person": {"shape": "note", "folders": ["people"], "index": "people/_index.md"},
        "project": {"shape": "note", "folders": ["projects"], "index": "projects/_index.md"},
        "commitment": {"shape": "entry", "files": ["commitments.md"]},
        "fact": {"shape": "entry", "files": ["commitments.md"]}})
    await memory_settings.update_settings(body, db=pool)
    settings, problem = await memory_settings.load(pool)
    assert problem is None
    return settings


async def lead(pool, **fields):
    await stream.source(pool)
    await stream.person(pool, "ou_synthetic_sender", "Synthetic sender")
    seq = await stream.add(pool, "Synthetic message", sent_at=NOW, sender="ou_synthetic_sender",
                           chat_id="oc_synthetic_memory", chat_name="Synthetic planning")
    return await leads_support.lead(pool, [seq], **fields)


async def propose(pool, number, **fields):
    return (await memory_leads.propose_from_lead(f"lead-{number}", actor=model.Actor.USER,
                                                via=model.Via.DASHBOARD, db=pool, **fields))["candidate"]


def test_operator_proposes_a_lead_from_the_dashboard(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await configure(pool, repo)
        number = await lead(pool, summary="Ship on Friday", detail="After the review")
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.propose_from_lead", {"lead": f"lead-{number}"})
            assert status == 200, answer
            candidate = answer["result"]["candidate"]
            assert {key: candidate[key] for key in ("state", "source_kind", "source_class", "untrusted", "statement")} == {
                "state": "drafting", "source_kind": "lead", "source_class": "external", "untrusted": True,
                "statement": "Ship on Friday"}
            assert candidate["why"] == "After the review"
            assert candidate["keywords"] == ["Synthetic sender", "Synthetic planning"]
            assert candidate["sources_total"] == 1
            assert candidate["sources"][0]["lead"] == f"lead-{number}"
            assert candidate["sources"][0]["quote"] is None
            event = candidate["events"][0]
            assert (event["action"], event["actor"], event["via"]) == ("propose", "user", "dashboard")
            assert await pool.fetchval("SELECT state FROM message_leads WHERE id=$1", number) == "active"
            status, _ = await call("memory.propose_from_lead", {"lead": f"lead-{number}"}, client=True)
            assert status == 403
            assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool='memory.propose_from_lead'") == 0

    run_db(check, monkeypatch, timeout=60)


def test_kind_override_and_refusals(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await configure(pool, repo)
        person = await lead(pool)
        request = await lead(pool, kind="request")
        other = await lead(pool, summary="Another decision")
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.propose_from_lead", {"lead": f"lead-{person}", "kind": "person"})
            assert status == 200 and answer["result"]["candidate"]["kind"] == "person", answer
            for arguments in ({"lead": f"lead-{request}"}, {"lead": f"lead-{other}", "kind": "preference"},
                              {"lead": "not-a-lead"}):
                status, answer = await call("memory.propose_from_lead", arguments)
                assert status == 400 and answer["error"]["code"] == "invalid", answer
            body = dict(await memory_settings.stored(pool))
            del body["targets"]["project"]
            await memory_settings.update_settings(body, db=pool)
            status, answer = await call("memory.propose_from_lead", {"lead": f"lead-{other}", "kind": "project"})
            assert status == 400 and "target" in answer["error"]["message"], answer
            status, answer = await call("memory.propose_from_lead", {"lead": "lead-999999"})
            assert status == 404 and answer["error"]["code"] == "not_found", answer
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 1

    run_db(check, monkeypatch, timeout=60)


@pytest.mark.parametrize("state", ["active", "expired", "superseded", "inaccurate", "dropped", "promoted"])
def test_promotable_states(monkeypatch, tmp_path, state):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await configure(pool, repo)
        number = await lead(pool, valid_until=NOW - timedelta(days=1) if state == "expired" else None)
        target = await lead(pool, summary="Replacement decision")
        if state == "superseded":
            await pool.execute("UPDATE message_leads SET state='superseded', superseded_by=$2 WHERE id=$1", number, target)
        elif state == "promoted":
            await pool.execute("UPDATE message_leads SET state='promoted', promoted_kind='vault_proposal', "
                               "promoted_ref='M-0042' WHERE id=$1", number)
        elif state == "inaccurate":
            await pool.execute("UPDATE message_leads SET state='inaccurate', marked_via='dashboard' WHERE id=$1", number)
        elif state == "dropped":
            await pool.execute("UPDATE message_leads SET state='dropped', check_verdict='drop', "
                               "check_reason='Synthetic unsupported lead' WHERE id=$1", number)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.propose_from_lead", {"lead": f"lead-{number}"})
            if state in ("active", "expired"):
                assert status == 200 and answer["result"]["candidate"]["state"] == "drafting", answer
            else:
                assert status == 409 and answer["error"]["code"] == "conflict", answer
                if state == "superseded":
                    assert f"lead-{target}" in answer["error"]["message"]
                if state == "promoted":
                    assert "M-0042" in answer["error"]["message"]
                assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0

    run_db(check, monkeypatch, timeout=60)


def test_a_lead_already_open_is_not_proposed_twice(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await configure(pool, repo)
        number = await lead(pool)
        first = await propose(pool, number)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.propose_from_lead", {"lead": f"lead-{number}"})
            assert status == 409 and first["id"] in answer["error"]["message"], answer
            assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 1
        # A stand-in draft permits the ordinary user rejection transition.
        cid = model.parse_id(first["id"])
        await sup.revise_current(pool, cid, (await memory_settings.load(pool))[0], changes=[{
            "op": "append_line", "path": "mem/commitments.md", "under": "## Commitments", "line": "- Ship Friday"}])
        await memory_actions.act("reject", first["id"], db=pool)
        assert (await propose(pool, number))["id"] != first["id"]

    run_db(check, monkeypatch, timeout=60)


def test_concurrent_requests_keep_only_one_open_candidate(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await configure(pool, repo)
        number = await lead(pool)
        answers = await asyncio.gather(propose(pool, number), propose(pool, number), return_exceptions=True)
        successful = [answer for answer in answers if isinstance(answer, dict)]
        refused = [answer for answer in answers if isinstance(answer, store.MemoryRefused)]
        assert len(successful) == len(refused) == 1, answers
        assert refused[0].code == "busy" and successful[0]["id"] in str(refused[0])
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 1

    run_db(check, monkeypatch, timeout=60)


@pytest.mark.parametrize("field", ["summary", "detail"])
def test_credentials_in_a_lead_are_refused(monkeypatch, tmp_path, field):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await configure(pool, repo)
        number = await lead(pool, **{field: "Synthetic key sk-abcdefghijklmnopqrstuvwx"})
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.propose_from_lead", {"lead": f"lead-{number}"})
            assert status == 400 and "credentials" in answer["error"]["message"], answer
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0

    run_db(check, monkeypatch, timeout=60)


def test_tombstoned_lead_seed_is_refused_and_not_stored(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        await configure(pool, repo)
        number = await lead(pool)
        candidate = await propose(pool, number)
        await memory_actions.act("delete", candidate["id"], db=pool)
        with pytest.raises(store.MemoryRefused) as error:
            await propose(pool, number)
        assert error.value.code == "tombstoned"
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_sources") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_tombstones") == 1

    run_db(check, monkeypatch, timeout=60)


@pytest.mark.parametrize("raw", [None, {"enabled": False, "sources": {"leads": True}},
                                      {"enabled": True, "sources": {"leads": False}}])
def test_switch_off_refuses_without_writing(monkeypatch, tmp_path, raw):
    async def check(pool):
        number = await lead(pool)
        if raw is not None:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('memory_write',$1)", json.dumps(raw))
        load = AsyncMock(side_effect=AssertionError("disabled source must not read the vault"))
        monkeypatch.setattr(vault, "load", load)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.propose_from_lead", {"lead": f"lead-{number}"})
            assert status == 403 and answer["error"]["code"] == "forbidden", answer
        load.assert_not_awaited()
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_sources") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events") == 0
        assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
        assert await pool.fetchval("SELECT state FROM message_leads WHERE id=$1", number) == "active"

    run_db(check, monkeypatch, timeout=60)


def test_written_candidate_promotes_the_lead_and_a_rejected_one_does_not(monkeypatch, tmp_path):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await configure(pool, repo)
        written_lead = await lead(pool, kind="commitment", summary="Send a synthetic report on Friday")
        rejected_lead = await lead(pool, kind="commitment", summary="Send another synthetic report")
        written, rejected = await propose(pool, written_lead), await propose(pool, rejected_lead)
        for candidate in (written, rejected):
            cid = model.parse_id(candidate["id"])
            revision = await sup.revise_current(pool, cid, settings, changes=[{
                "op": "append_line", "path": "mem/commitments.md", "under": "## Commitments",
                "line": "- " + candidate["statement"]}])
            await memory_actions.act("reject" if candidate is rejected else "confirm", candidate["id"],
                                     content_hash=revision["content_hash"], db=pool)
        assert await memory_write.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        row = await pool.fetchrow("SELECT state,promoted_kind,promoted_ref FROM message_leads WHERE id=$1", written_lead)
        assert tuple(row) == ("promoted", "vault_proposal", written["id"])
        assert await pool.fetchval("SELECT state FROM message_leads WHERE id=$1", rejected_lead) == "active"
        assert (repo / "mem/commitments.md").read_text() == "## Commitments\n- Send a synthetic report on Friday\n"
        message = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%B"], check=True,
                                 capture_output=True, text=True).stdout
        assert f"lead-{written_lead}" in message and "Synthetic planning" in message

    run_db(check, monkeypatch, timeout=60)


@pytest.mark.parametrize("delete_chat", [False, True])
def test_deleting_the_lead_takes_the_unwritten_candidate_and_purges_a_written_one(monkeypatch, tmp_path, delete_chat):
    repo = sup.synthetic_vault(tmp_path)

    async def check(pool):
        settings = await configure(pool, repo)
        unwritten_lead = await lead(pool, kind="commitment", summary="Unwritten synthetic commitment")
        written_lead = await lead(pool, kind="commitment", summary="Written synthetic commitment")
        unwritten, written = await propose(pool, unwritten_lead), await propose(pool, written_lead)
        cid = model.parse_id(written["id"])
        revision = await sup.revise_current(pool, cid, settings, changes=[{
            "op": "append_line", "path": "mem/commitments.md", "under": "## Commitments",
            "line": "- Written synthetic commitment"}])
        await memory_actions.act("confirm", written["id"], content_hash=revision["content_hash"], db=pool)
        assert await memory_write.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        before = (await store.get(cid, db=pool))["candidate"]
        if delete_chat:
            result = await message_stream.delete_events(stream.SOURCE, "chat", chat_id="oc_synthetic_memory", db=pool)
            assert result["deleted"] == 2
        else:
            await pool.execute("DELETE FROM message_leads WHERE id=ANY($1::bigint[])", [unwritten_lead, written_lead])
        assert await store.get(model.parse_id(unwritten["id"]), db=pool) is None
        found = await store.get(cid, db=pool)
        row = found["candidate"]
        assert row["state"] == "written" and row["purged_at"] is not None
        assert (row["statement"], row["why"], list(row["keywords"])) == ("", "", [])
        assert (row["content_hash"], row["commit_sha"]) == (before["content_hash"], before["commit_sha"])
        assert found["revision"]["changes"] == [] and found["sources"] == []
        assert (repo / "mem/commitments.md").read_text() == "## Commitments\n- Written synthetic commitment\n"

    run_db(check, monkeypatch, timeout=60)
