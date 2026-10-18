"""The agent room's consults on a real PostgreSQL: a question for a seat whose run ended is answered by a fork of its
last session, read only, and posted as the seat; when that cannot be, the Planner gets the question with the reason.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from looper.agents.consult import CONSULT_CONTRACT
from looper.agents.room import FallbackReason, RoomService
from looper.agents.system_prompt import REVIEW_CONTRACT, WORK_CONTRACT
from tests.fake_claude import fake_claude
from tests.test_room_db import FE, PLANNER, _Pool, _room_task
from tests.test_room_consult import _repo
from tests.test_usage_api_db import DSN, _run
from tests.test_task_agents_db import _until

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


# A Claude Code that answers a consult: it records how it was started, then answers every question its prompt names
# (#<id>), or, as the test asks, fails to find the session, writes no JSON, leaves one unanswered or hangs.
_CONSULTED = """
import json, os, re, sys, time, subprocess
from pathlib import Path
mode = os.environ.get("CONSULT_MODE", "answer")
fork = "11111111-1111-4111-8111-111111111111"
if mode == "no_stdin":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": fork}), flush=True)
    time.sleep(60)
prompt = json.loads(sys.stdin.readline())["message"]["content"]
system = open(sys.argv[sys.argv.index("--append-system-prompt-file") + 1]).read()
open(os.environ["SEEN"], "a").write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "system": system}) + "\\n")
if mode == "gone":
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
                      "errors": ["No conversation found with session ID: s-1"]}), flush=True)
    sys.exit(1)
session = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "consult" / (fork + ".jsonl")
session.parent.mkdir(parents=True, exist_ok=True)
session.write_text("fork")
print(json.dumps({"type": "system", "subtype": "init", "session_id": fork}), flush=True)
if mode in ("stderr", "no_result"):
    sys.stderr.write("x" * 1_000_000 + "stderr tail")
    sys.stderr.flush()
if mode == "no_result":
    sys.exit(1)
if mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(os.environ["CHILD_PID"]).write_text(str(child.pid))
if mode == "hang":
    time.sleep(60)
ids = [int(i) for i in re.findall(r"^- #(\\d+) from", prompt, re.MULTILINE)]
if mode == "prose":
    text = "I think it is qty."
else:
    answers = [{"reply_to": i, "body": "the field is qty"} for i in ids[: 1 if mode == "one" else None]]
    text = "Here.\\n" + json.dumps({"answers": answers, "unsure": "the rest" if mode == "one" else ""})
tools = sys.argv[sys.argv.index("--tools") + 1].split(",") if "--tools" in sys.argv else ["Write"]
disallowed = sys.argv[sys.argv.index("--disallowedTools") + 1].split(",") if "--disallowedTools" in sys.argv else []
write = Path.cwd() / "consult-wrote.txt"
denials = []
if "Write" not in tools or "Write" in disallowed:
    denials.append({"tool_name": "Write", "tool_input": {"file_path": str(write)}})
else:
    write.write_text("written")
result = {"type": "result", "subtype": "success", "is_error": False, "result": text, "permission_denials": denials}
if mode != "no_cost":
    result["total_cost_usd"] = float(os.environ.get("CONSULT_COST", "0.01"))
print(json.dumps(result), flush=True)
if mode == "wait_exit":
    os.close(1)
    os.close(2)
    time.sleep(60)
else:
    sys.stdin.read()
"""


async def _ran(pool: Any, work: Path, *, session: str | None = "s-1", provider: str = "prov-claude-subscription",
               prompt: str = "## Role\nYou build the API.\n\n" + WORK_CONTRACT) -> None:
    """s1's Worker (worker-1, backend_rd@s1) ran in ``work`` and its step was accepted: its record has its
    session."""
    work.mkdir(exist_ok=True)
    await pool.execute("UPDATE tasks SET work_dir = $1", str(work))
    await pool.execute("UPDATE task_steps SET status = 'verified' WHERE id = 'step-s1'")
    metadata = {"profile": {"runtime": {"type": "claude-code"}, "provider": {"id": provider}},
                "system_prompt": prompt, "cwd": str(work)}
    await pool.execute("INSERT INTO llm_records (agent_id, task_id, session_id, status, metadata) "
                       "VALUES ('worker-1', 'task-r', $1, 'completed', $2::jsonb)", session, json.dumps(metadata))


def _service(tmp_path: Path, room: RoomService, pool_runs: _Pool) -> Any:
    """A ConsultService whose runs go through a real WorkerPool on the fake Claude above."""
    from looper.agents.consult import ConsultService
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool

    workers = WorkerPool(config=WorkerConfig(claude_bin=fake_claude(tmp_path / "claude", _CONSULTED)),
                         runtime_root=tmp_path / "runtime")
    workers.room_runs = pool_runs.room_runs  # type: ignore[method-assign]
    workers.remind = pool_runs.remind  # type: ignore[method-assign]
    room.worker_pool = workers
    service = ConsultService(room=room, worker_pool=workers, runtime_root=tmp_path / "runtime",
                             claude_bin=str(tmp_path / "claude"))
    service._recovered = True  # as after the server's start
    room.consults = service
    return service


async def _settled(pool: Any, *, noted: bool = True) -> dict[str, Any]:
    """The last consult once it ended: its row moved, its answers and escalations posted, and (``noted``, all but a
    consult that never started) the outcome noted in the task's activity, last."""
    for _ in range(750):
        row = await pool.fetchrow("SELECT * FROM room_consults ORDER BY id DESC LIMIT 1")
        done = not noted or await pool.fetchval("SELECT count(*) FROM activity WHERE summary LIKE 'Consulted %' "
                                                "OR summary LIKE 'Could not consult %'")
        if row is not None and row["state"] not in ("queued", "running") and done:
            return dict(row)
        await asyncio.sleep(0.02)
    raise AssertionError("the consult did not end")


def test_a_consult_waits_for_recovery_then_answers_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("SEEN", str(seen))

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        service._recovered = False
        try:
            member = (await room._context("task-r")).member("backend_rd@s1")
            question = await _question(pool, "qty?")
            assert await service.ask("task-r", member, question) is None
            assert not service._running
            assert await pool.fetchval("SELECT state FROM room_consults") == "queued"
            assert not seen.exists()
            await service.resume_all()
            assert (await _settled(pool))["state"] == "answered"
            await _until(lambda: not service._running, "the consult's end")
            assert len(seen.read_text().splitlines()) == 1
            assert await pool.fetchval("SELECT count(*) FROM room_messages WHERE kind = 'answer'") == 1
        finally:
            await service.stop()

    _run(test, monkeypatch)


@pytest.mark.parametrize("mode", ["no_cost", "answer"])
def test_a_consult_checks_its_result_cost_when_the_recorded_cost_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, mode: str,
) -> None:
    from looper.agents.consult import db_llm_records

    monkeypatch.setenv("SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("CONSULT_MODE", mode)

    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cost lookup failed")

    monkeypatch.setattr(db_llm_records, "get_record", unavailable)

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        try:
            question = await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
            row = await _settled(pool)
            if mode == "no_cost":
                assert (row["state"], row["fallback_reason"]) == ("failed", "budget")
                assert "cost is unknown" in row["detail"]
                assert await pool.fetchval("SELECT count(*) FROM room_messages WHERE kind = 'answer'") == 0
                escalation = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'escalation'")
                assert escalation["reply_to"] == question["id"] and escalation["planner_pending"]
            else:
                assert row["state"] == "answered" and row["cost_usd"] == pytest.approx(0.01)
                answer = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'answer'")
                assert answer["sender_seat"] == "backend_rd@s1" and answer["reply_to"] == question["id"]
            assert any(record.exc_info and "cost lookup failed" in str(record.exc_info[1])
                       for record in caplog.records if record.name == "looper.agents.consult")
        finally:
            await service.stop()

    _run(test, monkeypatch)


def test_a_consult_with_no_full_provider_price_does_not_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dataclasses import replace

    from looper.agents import consult
    from looper.agents.agent_settings import AgentSettings
    from looper.agents.profile_registry import BUILTIN_PROFILE, cost_basis
    from looper.agents.runtimes import AuthKind

    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("SEEN", str(seen))
    provider = replace(BUILTIN_PROFILE.provider, id="provider-test", auth=AuthKind.API_KEY,
                       base_url="https://provider.invalid", api_key="test-key")
    profile = replace(BUILTIN_PROFILE, provider=provider, model="unpriced-model")
    assert cost_basis(profile)["source"] == "provider_price"
    assert profile.model in cost_basis(profile)["unpriced"]

    async def settings(*args: Any, **kwargs: Any) -> AgentSettings:
        return AgentSettings(profile=profile)

    monkeypatch.setattr(consult, "load_agent_settings", settings)

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work", provider=provider.id)
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        try:
            question = await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
            row = await _settled(pool)
            assert (row["state"], row["fallback_reason"]) == ("fallback", "runtime_cannot")
            assert "model has no full price" in row["detail"]
            assert not seen.exists()
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE agent_id LIKE 'consult-%'") == 0
            escalation = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'escalation'")
            assert escalation["reply_to"] == question["id"] and escalation["planner_pending"]
        finally:
            await service.stop()

    _run(test, monkeypatch)


@pytest.mark.parametrize("mode", ["answer", "prose"])
def test_a_consult_finishes_without_messages_when_its_room_was_turned_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    monkeypatch.setenv("SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("CONSULT_MODE", mode)

    async def test(pool: Any) -> None:
        project = await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        workers = _Pool({"worker-2": "running"})
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, workers)
        entered, release = asyncio.Event(), asyncio.Event()
        execute = service._execute

        async def held(*args: Any) -> Any:
            entered.set()
            await release.wait()
            return await execute(*args)

        monkeypatch.setattr(service, "_execute", held)
        try:
            await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert await pool.fetchval("SELECT state FROM room_consults") == "running"
            await pool.execute("UPDATE projects SET agent_room = 'off' WHERE id = $1", project["id"])
            messages = await pool.fetchval("SELECT count(*) FROM room_messages")
            workers.reminded.clear()
            wakes: list[str] = []
            monkeypatch.setattr(room, "_wake_planner", wakes.append)
            monkeypatch.setattr(room, "_wake_inboxes", wakes.append)
            release.set()
            await _until(lambda: not service._running, "the consult's end")
            row = await pool.fetchrow("SELECT * FROM room_consults")
            assert (row["state"], row["fallback_reason"]) == ("failed", "room_off")
            assert row["detail"] == "its task's agent room was turned off" and row["finished_at"] is not None
            assert await pool.fetchval("SELECT count(*) FROM room_messages") == messages
            assert not wakes and not workers.reminded
        finally:
            release.set()
            await service.stop()

    _run(test, monkeypatch)


def test_a_question_for_a_seat_whose_run_ended_is_answered_by_a_fork_of_its_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("SEEN", str(seen))

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        source = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "consult" / "s-1.jsonl"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("source")
        room = RoomService(batch_seconds=0)
        _service(tmp_path, room, _Pool({"worker-2": "running"}))
        posted = await room.post("task-r", FE, kind="question", body="qty or quantity?", to=["backend_rd@s1"])
        assert "asks its last session" in posted["delivery"][0]["state"]
        consult = await _settled(pool)
        assert (consult["state"], consult["read_only"], consult["denied_writes"]) == ("answered", "runtime", 1)
        answer = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'answer'")
        assert (answer["sender_seat"], answer["via"], answer["audience"], answer["reply_to"], answer["cc_planner"]) == \
            ("backend_rd@s1", "consult", ["fe_rd@s2"], posted["id"], True)
        record = json.loads(seen.read_text().splitlines()[0])
        argv = record["argv"]
        assert argv[argv.index("--resume") + 1] == "s-1" and "--fork-session" in argv
        assert "--dangerously-skip-permissions" not in argv
        assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
        assert argv[argv.index("--allowedTools") + 1] == "Read,Glob,Grep"
        assert "Bash" in argv[argv.index("--disallowedTools") + 1].split(",")
        assert not (tmp_path / "work" / "consult-wrote.txt").exists()
        await room.consults.stop()
        # The fork stays in Claude Code's store, as every Claude Code session does; the source is unchanged.
        assert (source.parent / "11111111-1111-4111-8111-111111111111.jsonl").exists()
        assert source.read_text() == "source"
        assert record["system"].endswith(CONSULT_CONTRACT) and WORK_CONTRACT not in record["system"]
        assert record["prompt"].startswith("[CONSULT] You are backend_rd@s1") and "qty or quantity?" in record["prompt"]
        run = await pool.fetchrow("SELECT agent_id, cost_usd FROM llm_records WHERE agent_id LIKE 'consult-%'")
        assert run is not None and run["agent_id"] == consult["consult_run"]
        # The consult was a run of a Worker's kind under the task's limit, and it is gone now.
        assert not room.worker_pool._active

    _run(test, monkeypatch)



def test_a_running_consult_is_recorded_for_the_next_start_and_a_shutdown_ends_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # A server that exits without finishing its shutdown (its deadline, a second signal, a crash) leaves a running
    # consult to the next start's sweep, which ends only the agents it finds recorded (proc_tree.spawn).
    from looper.agents import proc_tree

    if proc_tree._start_time(os.getpid()) is None:
        pytest.skip("process start times are unavailable in this environment")

    for name, value in (("_records", None), ("_server", None), ("_recorded", {}), ("_ending", set())):
        monkeypatch.setattr(proc_tree, name, value)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.5)
    monkeypatch.setenv("SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("CONSULT_MODE", "hang")
    records = tmp_path / "runtime" / proc_tree.RECORDS_DIR

    async def test(pool: Any) -> None:
        await proc_tree.sweep(tmp_path / "runtime")  # this server records its agents from here on
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        await room.post("task-r", FE, kind="question", body="qty or quantity?", to=["backend_rd@s1"])
        for _ in range(500):
            if listed := sorted(records.glob("*.json")):
                break
            await asyncio.sleep(0.02)
        assert len(listed) == 1
        pid = json.loads(listed[0].read_text())["pid"]
        assert proc_tree._start_time(pid) is not None
        await service.stop()  # the shutdown's first step
        assert proc_tree._start_time(pid) is None and not list(records.glob("*.json"))
        consult = await pool.fetchrow("SELECT state FROM room_consults")
        assert consult["state"] == "running"  # the next start runs it again (resume_all)

    _run(test, monkeypatch)

@pytest.mark.parametrize(("setup", "mode", "reason"), [
    ("no_record", "answer", "no_session"),
    ("no_session", "answer", "no_session"),
    ("limit", "answer", "limit"),
    ("other_profile", "answer", "runtime_cannot"),  # the seat's profile now runs with another provider
    ("ran", "gone", "session_gone"),
    ("ran", "prose", "contract"),
])
def test_a_question_no_consult_can_answer_goes_to_the_planner_with_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup: str, mode: str, reason: str,
) -> None:
    monkeypatch.setenv("SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("CONSULT_MODE", mode)

    async def test(pool: Any) -> None:
        await _room_task(pool, **({"room_consults_per_task": 0} if setup == "limit" else {}))
        if setup == "no_record":
            await pool.execute("UPDATE task_steps SET status = 'verified' WHERE id = 'step-s1'")
        else:
            await _ran(pool, tmp_path / "work", session=None if setup == "no_session" else "s-1",
                       provider="prov-elsewhere" if setup == "other_profile" else "prov-claude-subscription")
        room = RoomService(batch_seconds=0)
        _service(tmp_path, room, _Pool({"worker-2": "running"}))
        posted = await room.post("task-r", FE, kind="question", body="qty or quantity?", to=["backend_rd@s1"])
        if setup in ("ran", "other_profile"):
            consult = await _settled(pool)
            assert consult["fallback_reason"] == reason, consult
        escalation = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'escalation'")
        assert escalation is not None and f"({reason})" in escalation["body"] and escalation["planner_pending"]
        assert escalation["reply_to"] == posted["id"]
        notice = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'notice'")
        assert notice["audience"] == ["fe_rd@s2"]
        assert await pool.fetchval("SELECT count(*) FROM room_messages WHERE kind = 'answer'") == 0

    _run(test, monkeypatch)


def test_a_question_the_consult_left_unanswered_goes_to_the_planner_and_the_answered_one_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("CONSULT_MODE", "one")

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        # Two questions before the consult starts: one consult takes both.
        start = service._start
        held: list[int] = []
        service._start = held.append  # type: ignore[method-assign]
        first = await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
        second = await room.post("task-r", PLANNER, kind="question", body="port?", to=["backend_rd@s1"])
        assert len(held) == 1 and await pool.fetchval("SELECT questions FROM room_consults") == [first["id"],
                                                                                              second["id"]]
        start(held[0])
        consult = await _settled(pool)
        assert consult["state"] == "failed" and consult["detail"] == "the rest"
        answers = await pool.fetch("SELECT reply_to FROM room_messages WHERE kind = 'answer'")
        assert [a["reply_to"] for a in answers] == [first["id"]]
        escalation = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'escalation'")
        assert escalation["reply_to"] == second["id"] and "(unsure)" in escalation["body"]

    _run(test, monkeypatch)


def test_a_reviewer_seat_is_consulted_only_by_the_planner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.ops import OpError

    monkeypatch.setenv("SEEN", str(tmp_path / "seen.jsonl"))

    async def test(pool: Any) -> None:
        await _room_task(pool)
        (tmp_path / "work").mkdir()
        await pool.execute("UPDATE tasks SET work_dir = $1", str(tmp_path / "work"))
        await pool.execute("UPDATE task_steps SET status = 'verified', reviewer_id = 'reviewer-1' WHERE id = 'step-s1'")
        metadata = {"profile": {"runtime": {"type": "claude-code"}, "provider": {"id": "prov-claude-subscription"}},
                    "system_prompt": "## Review\n\n" + REVIEW_CONTRACT, "cwd": str(tmp_path / "work")}
        await pool.execute("INSERT INTO llm_records (agent_id, task_id, session_id, status, metadata) "
                           "VALUES ('reviewer-1', 'task-r', 's-1', 'completed', $1::jsonb)", json.dumps(metadata))
        room = RoomService(batch_seconds=0)
        _service(tmp_path, room, _Pool({"worker-2": "running"}))
        with pytest.raises(OpError):  # a Worker does not ask a Reviewer, offline or not
            await room.post("task-r", FE, kind="question", body="did I pass?", to=["reviewer@s1"])
        await room.post("task-r", PLANNER, kind="question", body="what did you check?", to=["reviewer@s1"])
        consult = await _settled(pool)
        assert consult["state"] == "answered"
        answer = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'answer'")
        assert (answer["sender_seat"], answer["sender_kind"], answer["audience"], answer["planner_pending"]) == (
            "reviewer@s1", "review", ["planner"], True)

    _run(test, monkeypatch)


def test_a_consult_whose_task_ended_while_it_waited_does_not_start(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("SEEN", str(seen))

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        start = service._start
        held: list[int] = []
        service._start = held.append  # type: ignore[method-assign]
        await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
        await pool.execute("UPDATE tasks SET status = 'stopped'")
        start(held[0])
        consult = await _settled(pool, noted=False)
        assert (consult["state"], consult["detail"]) == ("failed", "its task ended first")
        assert not seen.exists()  # nothing ran

    _run(test, monkeypatch)


async def _question(pool: Any, body: str, *, asker: str = "fe_rd@s2") -> dict[str, Any]:
    from looper.db import room as db_room

    return await db_room.add("task-r", sender_seat=asker, sender_kind="planner" if asker == "planner" else "work",
                             kind="question", body=body, audience=["backend_rd@s1"], pool=pool)


async def _queued(pool: Any, questions: list[int], *, state: str = "queued") -> dict[str, Any]:
    record = await pool.fetchval("SELECT id FROM llm_records WHERE agent_id = 'worker-1' ORDER BY id DESC LIMIT 1")
    row = await pool.fetchrow("INSERT INTO room_consults (task_id, seat, questions, source_run, source_record, runtime, "
                              "state) VALUES ('task-r', 'backend_rd@s1', $1, 'worker-1', $2, 'claude-code', $3) "
                              "RETURNING *", questions, record, state)
    return dict(row)


def test_shutdown_does_not_start_the_seats_next_consult(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("SEEN", str(seen))
    monkeypatch.setenv("CONSULT_MODE", "hang")

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        try:
            await room.post("task-r", FE, kind="question", body="first?", to=["backend_rd@s1"])
            await _until(lambda: seen.exists() and len(seen.read_text().splitlines()) == 1, "the first consult")
            await room.post("task-r", FE, kind="question", body="second?", to=["backend_rd@s1"])
            second = await pool.fetchrow("SELECT * FROM room_consults ORDER BY id DESC LIMIT 1")
            assert second["state"] == "queued"
            await service.stop()
            await asyncio.sleep(0.1)
            assert not service._running
            assert await pool.fetchval("SELECT state FROM room_consults WHERE id = $1", second["id"]) == "queued"
            assert len(seen.read_text().splitlines()) == 1
        finally:
            await service.stop()

    _run(test, monkeypatch)


def _overlap_admissions(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    fetchrow = asyncpg.Connection.fetchrow
    queued = "SELECT * FROM room_consults WHERE task_id = $1 AND seat = $2 AND state = 'queued' " \
             "ORDER BY id DESC LIMIT 1"

    async def overlapping_fetchrow(conn: Any, query: str, *args: Any, **kwargs: Any) -> Any:
        row = await fetchrow(conn, query, *args, **kwargs)
        if query == queued:
            await asyncio.sleep(0.03)  # overlap empty reads when admission has no lock
        return row

    monkeypatch.setattr(asyncpg.Connection, "fetchrow", overlapping_fetchrow)


def test_parallel_questions_join_one_queued_consult(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _overlap_admissions(monkeypatch)

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool())
        held: list[int] = []
        service._start = held.append
        member = (await room._context("task-r")).member("backend_rd@s1")
        questions = [await _question(pool, f"question {i}?") for i in range(5)]
        assert await asyncio.gather(*(service.ask("task-r", member, q) for q in questions)) == [None] * 5
        rows = await pool.fetch("SELECT * FROM room_consults")
        assert len(rows) == 1 and held == [rows[0]["id"]]
        assert sorted(rows[0]["questions"]) == [q["id"] for q in questions]
        assert rows[0]["source_record"] == await pool.fetchval("SELECT id FROM llm_records")
        await service.ask("task-r", member, questions[0])
        assert await pool.fetchval("SELECT cardinality(questions) FROM room_consults") == 5

    _run(test, monkeypatch)


def test_parallel_seats_share_the_task_consult_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _overlap_admissions(monkeypatch)

    async def test(pool: Any) -> None:
        await _room_task(pool, room_consults_per_task=1)
        await _ran(pool, tmp_path / "work")
        await pool.execute("UPDATE task_steps SET status = 'verified' WHERE id = 'step-s2'")
        await pool.execute("INSERT INTO llm_records (agent_id, task_id, session_id, status, metadata) "
                           "SELECT 'worker-2', task_id, 's-2', status, metadata FROM llm_records WHERE agent_id = 'worker-1'")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool())
        held: list[int] = []
        service._start = held.append
        context = await room._context("task-r")
        members = [context.member("backend_rd@s1"), context.member("fe_rd@s2")]
        questions = [await _question(pool, f"question {i}?") for i in range(30)]
        results = await asyncio.gather(*(service.ask("task-r", members[i % 2], q) for i, q in enumerate(questions)))
        assert await pool.fetchval("SELECT count(*) FROM room_consults") == 1
        assert len(held) == 1 and results.count(FallbackReason.LIMIT) == 15

    _run(test, monkeypatch)


def test_recovery_requeues_the_running_consult_and_starts_only_the_oldest_per_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        first = await _queued(pool, [], state="running")
        second = await _queued(pool, [])
        service = _service(tmp_path, RoomService(batch_seconds=0), _Pool())
        held: list[int] = []
        service._start = held.append
        await service.resume_all()
        assert held == [first["id"]]
        rows = await pool.fetch("SELECT state FROM room_consults ORDER BY id")
        assert [r["state"] for r in rows] == ["queued", "queued"]
        assert second["id"] > first["id"]

    _run(test, monkeypatch)


def test_start_keeps_a_busy_seat_queued_and_fails_an_ended_task(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    from looper.db import room as db_room

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        await _queued(pool, [], state="running")
        waiting = await _queued(pool, [])
        row, state = await db_room.start_consult(waiting["id"], consult_run="consult-wait", pool=pool)
        assert state is db_room.ConsultStart.BUSY
        assert await pool.fetchval("SELECT state FROM room_consults WHERE id = $1", waiting["id"]) == "queued"
        await pool.execute("UPDATE tasks SET status = 'completed'")
        row, state = await db_room.start_consult(waiting["id"], consult_run="consult-wait", pool=pool)
        assert state is db_room.ConsultStart.TASK_ENDED
        row = await db_room.get_consult(waiting["id"], pool=pool)
        assert (row["state"], row["detail"]) == ("failed", "its task ended first") and row["finished_at"] is not None
        _, state = await db_room.start_consult(waiting["id"], consult_run="consult-wait", pool=pool)
        assert state is db_room.ConsultStart.GONE

    _run(test, monkeypatch)


def test_finishing_a_consult_commits_answers_escalations_and_state_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.db import room as db_room

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        first, second = await _question(pool, "qty?"), await _question(pool, "port?")
        consult = await _queued(pool, [first["id"], second["id"]], state="running")
        room = RoomService(worker_pool=_Pool({"worker-2": "running"}), batch_seconds=0)
        add, inserts = db_room.add, 0

        async def failing_add(*args: Any, **kwargs: Any) -> Any:
            nonlocal inserts
            inserts += 1
            if inserts == 2:
                raise RuntimeError("second insert failed")
            return await add(*args, **kwargs)

        async def finish() -> Any:
            return await room.finish_consult("task-r", consult["id"], "backend_rd@s1", state="failed",
                                             expect=("running",), answers=[(first, "qty")], to_planner=[second],
                                             reason=FallbackReason.UNSURE, why="the port is unknown",
                                             columns={"read_only": "runtime", "detail": "the port is unknown"})

        monkeypatch.setattr(db_room, "add", failing_add)
        with pytest.raises(RuntimeError, match="second insert failed"):
            await finish()
        assert await pool.fetchval("SELECT state FROM room_consults") == "running"
        assert await pool.fetchval("SELECT count(*) FROM room_messages WHERE kind != 'question'") == 0
        monkeypatch.setattr(db_room, "add", add)
        assert await finish() is not None
        rows = await pool.fetch("SELECT * FROM room_messages WHERE kind != 'question' ORDER BY id")
        assert [r["kind"] for r in rows] == ["answer", "escalation", "notice"]
        assert (rows[0]["sender_seat"], rows[0]["via"], rows[0]["audience"], rows[0]["reply_to"]) == (
            "backend_rd@s1", "consult", ["fe_rd@s2"], first["id"])
        assert rows[0]["cc_planner"] and rows[1]["planner_pending"]
        assert rows[1]["reply_to"] == rows[2]["reply_to"] == second["id"]
        assert await finish() is None
        assert await pool.fetchval("SELECT count(*) FROM room_messages WHERE kind != 'question'") == 3

    _run(test, monkeypatch)


def test_killing_a_consult_fails_it_and_gives_its_question_to_the_planner(monkeypatch: pytest.MonkeyPatch,
                                                                        tmp_path: Path) -> None:
    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("SEEN", str(seen))
    monkeypatch.setenv("CONSULT_MODE", "hang")

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        try:
            question = await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
            fork = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "consult" / \
                "11111111-1111-4111-8111-111111111111.jsonl"
            await _until(lambda: seen.exists() and seen.read_text() and fork.exists(), "the hanging consult")
            run = await pool.fetchval("SELECT consult_run FROM room_consults")
            assert await service.worker_pool.kill(run)
            consult = await _settled(pool)
            assert (consult["state"], consult["fallback_reason"]) == ("failed", "stopped")
            escalation = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'escalation'")
            assert escalation["reply_to"] == question["id"] and escalation["planner_pending"]
            assert "(stopped)" in escalation["body"]
            assert not service.worker_pool._active and not service.worker_pool._tasks
            assert fork.exists()  # it stays in Claude Code's store, as every Claude Code session does
        finally:
            await service.stop()

    _run(test, monkeypatch)


def test_a_task_ended_while_a_consult_waits_for_capacity_is_rechecked_before_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.agent_settings import AgentType
    from looper.agents.tiers.worker import WorkerProcess

    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("SEEN", str(seen))

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        from looper.agents.tiers import worker

        async def limit(*args: Any, **kwargs: Any) -> int:
            return 1

        monkeypatch.setattr(worker, "load_task_limit", limit)
        holding, release = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            wp = WorkerProcess(worker_id="holder", task_id="step-s1", plan_id="", role=AgentType.WORKER,
                               parent_task_id="task-r")
            async with service.worker_pool.slot_for(wp):
                holding.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        try:
            await holding.wait()
            await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
            await _until(lambda: bool(service.worker_pool._slot_waiters), "the consult waiting for capacity")
            await pool.execute("UPDATE tasks SET status = 'completed'")
            release.set()
            await holder
            consult = await _settled(pool, noted=False)
            assert (consult["state"], consult["detail"]) == ("failed", "its task ended first")
            assert not seen.exists() or not seen.read_text()
        finally:
            release.set()
            await holder
            await service.stop()

    _run(test, monkeypatch)


def test_a_session_still_being_recorded_cannot_be_consulted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        await pool.execute("UPDATE llm_records SET status = 'running'")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool())
        held: list[int] = []
        service._start = held.append
        member = (await room._context("task-r")).member("backend_rd@s1")
        question = await _question(pool, "qty?")
        assert await service.ask("task-r", member, question) is FallbackReason.NO_SESSION
        assert not held and await pool.fetchval("SELECT count(*) FROM room_consults") == 0

    _run(test, monkeypatch)


def test_a_consult_does_not_resume_its_seats_newer_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("SEEN", str(seen))

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        start, held = service._start, []
        service._start = held.append
        await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
        pinned = await pool.fetchval("SELECT source_record FROM room_consults")
        await _ran(pool, tmp_path / "work", session="s-new")
        assert pinned < await pool.fetchval("SELECT max(id) FROM llm_records")
        start(held[0])
        consult = await _settled(pool)
        assert (consult["state"], consult["fallback_reason"]) == ("fallback", "no_session")
        assert "another run" in consult["detail"]
        assert not seen.exists()

    _run(test, monkeypatch)


@pytest.mark.parametrize("phase", ["prompt", "command", "note", "execute"])
def test_a_failure_after_the_snapshot_cleans_the_checkout_and_session_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str,
) -> None:
    from looper.agents import consult
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime

    async def test(pool: Any) -> None:
        await _room_task(pool)
        repo = tmp_path / "repo"
        _repo(repo)
        await _ran(pool, repo)
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        monkeypatch.setattr(consult, "RUNTIME_READ_ONLY", ())
        command = ClaudeCodeRuntime.consult_command
        forks: list[Path] = []

        def consult_command(self: Any, *args: Any, **kwargs: Any) -> list[str]:
            fork = kwargs["fork_dir"]
            fork.mkdir(parents=True)
            (fork / "session").write_text("fork")
            forks.append(fork)
            if phase == "command":
                raise ValueError("command failed")
            return command(self, *args, **kwargs)

        async def fail(*args: Any, **kwargs: Any) -> Any:
            raise ValueError(f"{phase} failed")

        monkeypatch.setattr(ClaudeCodeRuntime, "consult_command", consult_command)
        if phase == "prompt":
            monkeypatch.setattr(service, "_prompt", fail)
        elif phase == "execute":
            monkeypatch.setattr(service, "_execute", fail)
        elif phase == "note":
            note = service._note

            async def fail_note(task_id: str, run_id: str, summary: str) -> None:
                if summary.startswith("Consulting "):
                    raise ValueError("note failed")
                await note(task_id, run_id, summary)

            monkeypatch.setattr(service, "_note", fail_note)
        question = await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
        row = await _settled(pool)
        assert row["state"] == "failed" and f"{phase} failed" in row["detail"]
        assert await pool.fetchval("SELECT reply_to FROM room_messages WHERE kind = 'escalation'") == question["id"]
        assert not list((service.runtime_root / ".local" / "consults").iterdir())
        assert all(not fork.exists() for fork in forks)
        if phase != "prompt":
            assert len(forks) == 1
        listed = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True, check=True)
        assert "consult-" not in listed.stdout

    _run(test, monkeypatch)


def test_recovery_sweeps_leftover_checkouts_and_session_forks(monkeypatch: pytest.MonkeyPatch,
                                                             tmp_path: Path) -> None:
    from looper.agents.consult import _snapshot

    async def test(pool: Any) -> None:
        repo = tmp_path / "repo"
        _repo(repo)
        service = _service(tmp_path, RoomService(batch_seconds=0), _Pool())
        leftover = service.runtime_root / ".local" / "consults" / "x"
        assert _snapshot(repo, leftover) is not None
        fork = service.runtime_root / ".local" / "codex" / "consult-sessions" / "task-1" / "y"
        fork.parent.mkdir(parents=True)
        fork.write_text("leftover session")
        untouched = service.runtime_root / ".local" / "codex" / "sessions" / "kept"
        untouched.parent.mkdir(parents=True)
        untouched.write_text("run session")
        await service.resume_all()
        assert not leftover.exists() and not fork.exists()
        assert untouched.read_text() == "run session"
        listed = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True, check=True)
        assert str(leftover) not in listed.stdout

    _run(test, monkeypatch)


def test_shutdown_waits_for_a_snapshot_being_created_and_removes_its_checkout_and_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import threading

    from looper.agents import consult

    async def test(pool: Any) -> None:
        await _room_task(pool)
        repo = tmp_path / "repo"
        _repo(repo)
        await _ran(pool, repo)
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        monkeypatch.setattr(consult, "RUNTIME_READ_ONLY", ())
        snapshot = consult._snapshot
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        targets: list[tuple[Path, Path]] = []

        def blocked_snapshot(work: Path, target: Path) -> Any:
            fork = service.runtime_root / ".local" / "claude-code" / "consult-sessions" / "task-r" / target.name
            targets.append((target, fork))
            entered.set()
            try:
                if not release.wait(timeout=5):
                    raise TimeoutError("snapshot was not released")
                result = snapshot(work, target)
                fork.mkdir(parents=True)
                (fork / "session").write_text("fork")
                return result
            finally:
                finished.set()

        monkeypatch.setattr(consult, "_snapshot", blocked_snapshot)
        try:
            await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
            await _until(entered.is_set, "the snapshot thread")
            stopping = asyncio.create_task(service.stop())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(stopping, timeout=10)
            assert finished.is_set(), "shutdown must wait for the snapshot thread before cleaning it"
            assert len(targets) == 1
            target, fork = targets[0]
            assert not target.exists() and not fork.exists()
            listed = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True, check=True)
            assert str(target) not in listed.stdout
            assert not service._running
        finally:
            release.set()
            await service.stop()
            if entered.is_set():
                await _until(finished.is_set, "the released snapshot thread")

    _run(test, monkeypatch)


def test_a_question_joining_a_queued_consult_as_it_ends_also_goes_to_the_planner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        first, second = await _question(pool, "qty?"), await _question(pool, "port?")
        queued = await _queued(pool, [first["id"]])
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        held: list[int] = []
        service._start = held.append
        member = (await room._context("task-r")).member("backend_rd@s1")
        finish = room.finish_consult

        async def finish_after_asking(*args: Any, **kwargs: Any) -> Any:
            assert await service.ask("task-r", member, second) is None
            return await finish(*args, **kwargs)

        monkeypatch.setattr(room, "finish_consult", finish_after_asking)
        await service._give_up(queued["id"], "task-r", "backend_rd@s1", FallbackReason.STOPPED,
                                "its run was stopped")
        assert not held
        assert await pool.fetchval("SELECT questions FROM room_consults") == [first["id"], second["id"]]
        escalations = await pool.fetch("SELECT reply_to FROM room_messages WHERE kind = 'escalation' ORDER BY reply_to")
        assert [row["reply_to"] for row in escalations] == [first["id"], second["id"]]
        notices = await pool.fetch("SELECT reply_to FROM room_messages WHERE kind = 'notice' ORDER BY reply_to")
        assert [row["reply_to"] for row in notices] == [first["id"], second["id"]]
        assert await pool.fetchval("SELECT state FROM room_consults") == "failed"

    _run(test, monkeypatch)


def test_cleanup_removes_the_checkout_and_fork_when_git_times_out(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    from looper.agents import consult

    repo, checkout, fork = tmp_path / "repo", tmp_path / "checkout", tmp_path / "fork"
    checkout.mkdir()
    (checkout / "file").write_text("checkout")
    fork.mkdir()
    (fork / "session").write_text("fork")

    def timeout(*args: Any) -> Any:
        raise subprocess.TimeoutExpired("git", consult.GIT_TIMEOUT_SEC)

    monkeypatch.setattr(consult, "_git", timeout)
    consult._cleanup((repo, checkout, checkout), fork)
    assert not checkout.exists() and not fork.exists()


def test_killing_a_consult_waiting_for_capacity_also_gives_its_question_to_the_planner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        waiting = asyncio.Event()

        async def wait(wp: Any) -> None:
            waiting.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(service.worker_pool, "_take_slot", wait)
        try:
            question = await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
            await asyncio.wait_for(waiting.wait(), timeout=5)
            run = next(iter(service.worker_pool._active))
            assert await service.worker_pool.kill(run)
            consult = await _settled(pool)
            assert (consult["state"], consult["fallback_reason"]) == ("failed", "stopped")
            escalation = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'escalation'")
            assert escalation["reply_to"] == question["id"] and escalation["planner_pending"]
            assert not service.worker_pool._active and not service.worker_pool._tasks
        finally:
            await service.stop()

    _run(test, monkeypatch)


@pytest.mark.parametrize("mode", ["stderr", "no_result", "no_stdin", "wait_exit", "child", "over_budget"])
def test_a_consult_keeps_its_execution_bounds_and_cleans_its_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    import time

    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
    from looper.agents import proc_tree

    monkeypatch.setenv("SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("CONSULT_MODE", mode)
    monkeypatch.setenv("CHILD_PID", str(tmp_path / "child.pid"))
    if mode == "over_budget":
        monkeypatch.setenv("CONSULT_COST", "0.05")
    if mode == "child" and os.getpid() not in proc_tree._snapshot():
        # The sandbox may deny ps. List only this test's processes, using their real groups and liveness.
        roots: list[int] = []
        spawn = proc_tree.spawn

        async def spawning(*args: Any, **kwargs: Any) -> Any:
            process = await spawn(*args, **kwargs)
            roots.append(process.pid)
            return process

        def snapshot() -> Any:
            table = {}
            child_file = tmp_path / "child.pid"
            children = [int(child_file.read_text())] if child_file.exists() else []
            for pid in [*roots, *children]:
                try:
                    group = os.getpgid(pid)
                except ProcessLookupError:
                    continue
                parent = roots[0] if pid in children and roots[0] in table else os.getpid() if pid in roots else 1
                table[pid] = (parent, group)
            return table

        monkeypatch.setattr(proc_tree, "spawn", spawning)
        monkeypatch.setattr(proc_tree, "_snapshot", snapshot)
    write_turn = ClaudeCodeRuntime.write_turn

    async def write(self: Any, stdin: Any, text: str, **kwargs: Any) -> None:
        await write_turn(self, stdin, text + ("x" * 1_000_000 if mode == "no_stdin" else ""), **kwargs)

    monkeypatch.setattr(ClaudeCodeRuntime, "write_turn", write)

    async def test(pool: Any) -> None:
        await _room_task(pool, room_consult_budget_usd=0.01)
        await _ran(pool, tmp_path / "work")
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        execute = service._execute

        async def bounded(*args: Any) -> Any:
            return await execute(*args[:-1], 2)

        monkeypatch.setattr(service, "_execute", bounded)
        began = time.monotonic()
        try:
            question = await room.post("task-r", FE, kind="question", body="qty?", to=["backend_rd@s1"])
            row = await asyncio.wait_for(_settled(pool), timeout=8)
            await service.stop()
            if mode in ("stderr", "child"):
                assert row["state"] == "answered"
            else:
                reason = {"over_budget": "budget", "no_result": "contract"}.get(mode, "timeout")
                assert (row["state"], row["fallback_reason"]) == ("failed", reason)
                assert await pool.fetchval("SELECT count(*) FROM room_messages WHERE kind = 'answer'") == 0
                escalation = await pool.fetchrow("SELECT * FROM room_messages WHERE kind = 'escalation'")
                assert escalation["reply_to"] == question["id"] and escalation["planner_pending"]
            if mode == "over_budget":
                assert "0.0500 USD" in row["detail"] and "answers are dropped" in row["detail"]
            if mode in ("no_stdin", "wait_exit"):
                assert time.monotonic() - began < 7
            if mode == "no_result":
                error = await pool.fetchval("SELECT error FROM llm_records WHERE agent_id LIKE 'consult-%'")
                assert error.endswith("stderr tail") and len(error) < 2100
            if mode == "child":
                pid = int((tmp_path / "child.pid").read_text())
                for _ in range(50):
                    if pid not in proc_tree._snapshot():
                        break
                    await asyncio.sleep(0.02)
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            await service.stop()
            if (tmp_path / "child.pid").exists():
                pid = int((tmp_path / "child.pid").read_text())
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass

    _run(test, monkeypatch)
