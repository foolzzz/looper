"""The agent room's consults on a real PostgreSQL: a question for a seat whose run ended is answered by a fork of its
last session, read only, and posted as the seat; when that cannot be, the Planner gets the question with the reason.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from looper.agents.consult import CONSULT_CONTRACT, consult_answers, consult_prompt
from looper.agents.room import RoomService
from looper.agents.system_prompt import REVIEW_CONTRACT, WORK_CONTRACT
from tests.fake_claude import fake_claude
from tests.test_room_db import FE, PLANNER, _Pool, _room_task
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# A Claude Code that answers a consult: it records how it was started, then answers every question its prompt names
# (#<id>), or, as the test asks, fails to find the session, writes no JSON, or leaves one unanswered.
_CONSULTED = """
import json, os, re, sys
prompt = json.loads(sys.stdin.readline())["message"]["content"]
system = open(sys.argv[sys.argv.index("--append-system-prompt-file") + 1]).read()
open(os.environ["SEEN"], "a").write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "system": system}) + "\\n")
mode = os.environ.get("CONSULT_MODE", "answer")
if mode == "gone":
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
                      "errors": ["No conversation found with session ID: s-1"]}), flush=True)
    sys.exit(1)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-fork"}), flush=True)
ids = [int(i) for i in re.findall(r"^- #(\\d+) from", prompt, re.MULTILINE)]
if mode == "prose":
    text = "I think it is qty."
else:
    answers = [{"reply_to": i, "body": "the field is qty"} for i in ids[: 1 if mode == "one" else None]]
    text = "Here.\\n" + json.dumps({"answers": answers, "unsure": "the rest" if mode == "one" else ""})
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                  "permission_denials": [{"tool_name": "Bash"}], "total_cost_usd": 0.01}), flush=True)
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
    room.consults = service
    return service


async def _settled(pool: Any) -> dict[str, Any]:
    for _ in range(750):
        row = await pool.fetchrow("SELECT * FROM room_consults ORDER BY id DESC LIMIT 1")
        if row is not None and row["state"] not in ("queued", "running"):
            return dict(row)
        await asyncio.sleep(0.02)
    raise AssertionError("the consult did not end")


def test_a_question_for_a_seat_whose_run_ended_is_answered_by_a_fork_of_its_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen = tmp_path / "seen.jsonl"
    monkeypatch.setenv("SEEN", str(seen))

    async def test(pool: Any) -> None:
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
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
        assert argv[argv.index("--disallowedTools") + 1: argv.index("--disallowedTools") + 4] == \
            ["Edit", "Write", "NotebookEdit"]
        assert record["system"].endswith(CONSULT_CONTRACT) and WORK_CONTRACT not in record["system"]
        assert record["prompt"].startswith("[CONSULT] You are backend_rd@s1") and "qty or quantity?" in record["prompt"]
        run = await pool.fetchrow("SELECT agent_id, cost_usd FROM llm_records WHERE agent_id LIKE 'consult-%'")
        assert run is not None and run["agent_id"] == consult["consult_run"]
        # The consult was a run of a Worker's kind under the task's limit, and it is gone now.
        assert not room.worker_pool._active

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


# --- Without a database: the contract, the answers, the throwaway checkout ---


def test_the_consult_contract_replaces_the_runs_and_its_answers_are_read_by_question() -> None:
    assert consult_prompt("x\n\n" + WORK_CONTRACT) == "x\n\n" + CONSULT_CONTRACT
    assert consult_prompt("x\n\n" + REVIEW_CONTRACT) == "x\n\n" + CONSULT_CONTRACT
    assert consult_prompt("no contract") == "no contract\n\n" + CONSULT_CONTRACT
    text = 'ok {"answers": [{"reply_to": 3, "body": "qty"}, {"reply_to": 9, "body": "not asked"}, ' \
           '{"reply_to": 4, "body": " "}], "unsure": "the port"}'
    assert consult_answers(text, [3, 4]) == ({3: "qty"}, "the port")
    with pytest.raises(ValueError):
        consult_answers("I think qty.", [3])


def test_a_throwaway_checkout_shows_what_the_consult_changed_and_goes(tmp_path: Path) -> None:
    from looper.agents.consult import _changed, _cleanup, _snapshot

    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.py").write_text("x = 1\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    import os

    for command in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(command, cwd=repo, check=True, env={**os.environ, **env})
    snapshot = _snapshot(repo / "app", tmp_path / "consults" / "consult-1")
    assert snapshot is not None
    _, target, inside = snapshot
    assert inside == target / "app" and (inside / "a.py").read_text() == "x = 1\n"
    assert _changed(inside) == []
    (inside / "b.py").write_text("y = 2\n")
    assert _changed(inside) == ["app/b.py"]
    _cleanup(snapshot, tmp_path / "fork")
    assert not target.exists()
    assert "consult-1" not in subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True,
                                             text=True).stdout
    assert _snapshot(tmp_path / "plain", tmp_path / "consults" / "consult-2") is None


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
        consult = await _settled(pool)
        assert (consult["state"], consult["detail"]) == ("failed", "its task ended first")
        assert not seen.exists()  # nothing ran

    _run(test, monkeypatch)
