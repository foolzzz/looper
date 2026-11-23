"""Consults of the advisor on a real PostgreSQL (``looper/agents/advisor.py``, the advisor RFC): the advisor's and the
judge's roles and built-in profiles, a request's three checks and returns, the advisor's rounds, the consult's states
and records, with a Claude Code stand-in that answers as the judge or the advisor.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from looper import child_env
from tests.db_support import DSN, fresh_connection, run_db
from tests.fake_claude import fake_claude
from tests.test_advice import _request
from tests.subagents_schema_support import ensure_before_platform_subagents

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# A Claude Code that answers as the judge (its system prompt is the judge's) or as the advisor, writing down each
# start; JUDGE_REPLY is the judge's verdict, ADVISOR_REPLIES the advisor's answers in turn (one per line), and
# ADVISOR_MODE makes the advisor refuse, end its first run in error (error_once), or be rate-limited.
_STAND_IN = r'''
import json, os, sys
turn = json.loads(sys.stdin.readline())["message"]["content"]
system = open(sys.argv[sys.argv.index("--system-prompt-file") + 1]).read()
judge = "You check a request" in system
model = sys.argv[sys.argv.index("--model") + 1]
with open(os.environ["ADVISOR_SEEN"], "a") as seen:
    seen.write(json.dumps({"as": "judge" if judge else "advisor", "argv": sys.argv[1:], "turn": turn,
                           "cwd": os.getcwd()}) + "\n")
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "s", "model": model, "tools": [], "mcp_servers": []})
if judge and os.environ.get("JUDGE_MODE") == "hang":
    import time
    time.sleep(120)
if judge:
    text, cost, stop = os.environ.get("JUDGE_REPLY", ""), 0.001, "end_turn"
else:
    counter = os.environ["ADVISOR_SEEN"] + ".n"
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    replies = open(os.environ["ADVISOR_REPLIES"]).read().splitlines()
    mode = os.environ.get("ADVISOR_MODE")
    if mode == "error_then_hang" and n > 0:
        import time
        time.sleep(120)
    if mode == "error_then_hang":
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "s",
                          "result": "", "total_cost_usd": 0.2, "modelUsage": {model: {"costUSD": 0.2}}}), flush=True)
        sys.exit(0)
    if mode == "error_once" and n == 0 or mode == "rate_limited":
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "s",
                          "result": "", "total_cost_usd": 0.01, "modelUsage": {model: {"costUSD": 0.01}},
                          **({"api_error_status": 429} if mode == "rate_limited" else {})}), flush=True)
        sys.exit(0)
    n = n - 1 if mode == "error_once" else n
    text, cost = replies[min(n, len(replies) - 1)], 0.05
    stop = "refusal" if mode == "refuse" else "end_turn"
usage = {"input_tokens": 30, "output_tokens": 9, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
emit({"type": "assistant", "session_id": "s", "message": {"id": "m", "role": "assistant", "model": model,
      "content": [{"type": "text", "text": text}], "usage": usage}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": "s",
      "stop_reason": stop, "total_cost_usd": cost, "usage": usage, "modelUsage": {model: {"costUSD": cost}}})
'''
PASS = json.dumps({"standalone": True, "leaks": [], "answerable": 0.9, "missing": []})
ANSWER = json.dumps({"status": "answered", "answers": [{"question_id": "q1", "conclusion": "Cache hot keys first.",
                                                        "basis": ["reads repeat"], "confidence": "med"}],
                     "next_steps": [{"id": "s1", "action": "cache for 1 s", "verify": "P99 < 50 ms"}]})
NEEDS = json.dumps({"status": "needs_info", "answers": [],
                    "needs_info": [{"id": "n1", "question": "What is the cache hit rate?", "why": "sizing"}]})


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADVISOR_SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setenv("ADVISOR_REPLIES", str(tmp_path / "replies.txt"))
    monkeypatch.setenv("JUDGE_REPLY", PASS)
    (tmp_path / "replies.txt").write_text(ANSWER + "\n")
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"ADVISOR_SEEN", "ADVISOR_REPLIES", "JUDGE_REPLY",
                                                                    "ADVISOR_MODE", "JUDGE_MODE"})


def _replies(tmp_path: Path, *replies: str) -> None:
    (tmp_path / "replies.txt").write_text("\n".join(replies) + "\n")


def _seen(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "seen.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def _setup(pool: Any) -> None:
    from looper.agents.serve import seed_role_configs

    await seed_role_configs(pool)


async def _consult(pool: Any, *, source: str = "optional", budget: float = 1.0,
                   task: str = "task-a") -> tuple[int, int]:
    from looper.agents import advisor
    from looper.db import llm_records, projects

    project = (await projects.create_project(name=f"Import {task}", pool=pool))["id"]
    await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                       "VALUES ($1, $2, 'Speed up the import.', 'running')", task, project)
    await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) "
                       "VALUES ($1, $2, 0, 's1', 'Rewrite the loop.')", f"{task}-s1", task)
    planner = await llm_records.start_record(agent_id=f"planner-{task}", task_id=task)
    spawn = await advisor.open_consult(advisor.Brief(task, f"{task}-s1", 2, advisor.Source(source), planner),
                                       budget_usd=budget)
    return spawn, planner


async def _submit(tmp_path: Path, spawn: int, raw: Any, **kwargs: Any) -> Any:
    from looper.agents import advisor

    return await advisor.submit(spawn, raw, runtime_root=tmp_path / "runtime",
                                claude_bin=fake_claude(tmp_path / "claude", _STAND_IN), **kwargs)


def test_the_advisor_and_its_judge_run_their_own_built_in_profiles_unless_their_role_chooses_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import agent_settings
    from looper.agents.agent_settings import AgentType, SettingsError, load_agent_settings
    from looper.agents.system_prompt_mode import SystemPromptMode

    async def test(pool: Any) -> None:
        advisor = await load_agent_settings(AgentType.ADVISOR)
        judge = await load_agent_settings(AgentType.ADVICE_CHECK)
        assert (advisor.profile.id, advisor.model, advisor.effort) == ("agent-claude-code-fable-5-2",
                                                                       "claude-fable-5-2", "high")
        assert (judge.profile.id, judge.model) == ("agent-claude-code-sonnet-5-5", "claude-sonnet-5-5")
        assert advisor.profile_error is None and advisor.system_prompt_mode is SystemPromptMode.REPLACE
        # The shared defaults are for the roles with tools: they choose nothing for these two.
        await agent_settings.update_system({"profile_id": "agent-claude-code-opus", "run_budget_usd": 3})
        advisor = await load_agent_settings(AgentType.ADVISOR)
        assert advisor.profile.id == "agent-claude-code-fable-5-2" and advisor.max_budget_usd is None
        # Their role layer does (Settings → Roles), and clearing it brings the built-in back.
        await agent_settings.update_role("advisor", {"profile_id": "agent-claude-code-opus"})
        assert (await load_agent_settings(AgentType.ADVISOR)).profile.id == "agent-claude-code-opus"
        await agent_settings.update_role("advisor", {"profile_id": None})
        assert (await load_agent_settings(AgentType.ADVISOR)).profile.id == "agent-claude-code-fable-5-2"
        with pytest.raises(SettingsError, match="no harness"):
            await agent_settings.update_role("advice_check", {"harness_prompt": "Be careful."})
        with pytest.raises(SettingsError, match="always replaces"):
            await agent_settings.update_role("advisor", {"system_prompt_mode": "append"})
        roles = {row["role"]: row for row in await pool.fetch("SELECT role, display_name, timeout_seconds "
                                                                "FROM role_configs")}
        assert (roles["advisor"]["display_name"], roles["advisor"]["timeout_seconds"]) == ("Advisor", 300)
        assert roles["advice_check"]["timeout_seconds"] == 120
        # Without its built-in profile a role does not run in another's place.
        await pool.execute("DELETE FROM agent_profiles WHERE id = 'agent-claude-code-sonnet-5-5'")
        assert "is missing" in (await load_agent_settings(AgentType.ADVICE_CHECK)).profile_error

    run_db(test, monkeypatch, setup=_setup)


def test_a_request_that_passes_its_checks_is_answered_and_both_runs_are_the_planners(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.advisor import Ended
    from looper.db import llm_records

    async def test(pool: Any) -> None:
        spawn, planner = await _consult(pool)
        ended = await _submit(tmp_path, spawn, _request())
        assert isinstance(ended, Ended) and ended.state.value == "answered"
        assert ended.response.fields["answers"][0]["conclusion"] == "Cache hot keys first."
        judge, advisor = _seen(tmp_path)
        assert (judge["as"], advisor["as"]) == ("judge", "advisor")
        assert judge["argv"][judge["argv"].index("--model") + 1] == "claude-sonnet-5-5"
        argv = advisor["argv"]
        assert argv[argv.index("--model") + 1] == "claude-fable-5-2" and argv[argv.index("--effort") + 1] == "high"
        assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--max-turns") + 1] == "2"
        assert "advice_request/1" in advisor["turn"] and "advice_bindings" not in advisor["turn"]
        row = dict(await pool.fetchrow("SELECT * FROM agent_spawns WHERE id = $1", spawn))
        result = json.loads(row["result"])
        assert (row["state"], row["rounds"], row["returns"]) == ("answered", 1, 0)
        assert float(row["spent_usd"]) == pytest.approx(0.051)
        assert result["judges"][0]["passed"] and result["rounds"][0]["cost_usd"] == 0.05
        advice = dict(await pool.fetchrow("SELECT * FROM advisor_advice WHERE spawn_id = $1", spawn))
        assert (advice["attempt"], advice["source"], advice["kind"]) == (2, "optional", "diagnose")
        assert json.loads(advice["question"])["problem"] == _request()["problem"]
        assert json.loads(advice["advice"])["status"] == "answered" and advice["adoption"] is None
        runs = {r["run_kind"]: dict(r) for r in await pool.fetch(
            "SELECT run_kind, agent_id, parent_record_id, task_id FROM llm_records WHERE parent_record_id IS NOT NULL")}
        assert set(runs) == {"advisor", "advice_check"}
        assert all(r["parent_record_id"] == planner and r["task_id"] == "task-a" for r in runs.values())
        assert runs["advice_check"]["agent_id"].startswith("advice_check-")
        cost = (await llm_records.task_costs(["task-a"]))["task-a"]
        assert cost["cost_usd"] == pytest.approx(0.051) and cost["by_role"]["planner"]["runs"] == 3

    run_db(test, monkeypatch, setup=_setup)


def test_a_request_failing_its_checks_goes_back_twice_then_the_consult_is_blocked_without_sending_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.advisor import Ended, Returned

    async def test(pool: Any) -> None:
        spawn, _ = await _consult(pool)
        first = await _submit(tmp_path, spawn, {**_request(), "kind": "explain"})
        assert isinstance(first, Returned) and first.returns_left == 1
        assert any("kind must be one of" in p for p in first.problems)
        leaky = _request(problem="It fails in /Users/someone/project/sync.go at 80 ms P99, with no fallback.")
        second = await _submit(tmp_path, spawn, leaky)
        assert isinstance(second, Returned) and second.returns_left == 0
        assert any("local path" in p for p in second.problems)
        assert not any("/Users/someone" in p for p in second.problems)
        third = await _submit(tmp_path, spawn, leaky)
        assert isinstance(third, Ended) and third.state.value == "blocked"
        assert _seen(tmp_path) == []  # no model saw any of them
        row = dict(await pool.fetchrow("SELECT * FROM agent_spawns WHERE id = $1", spawn))
        checks = json.loads(row["result"])["checks"]
        assert [c["category"] for c in checks] == ["format", "local_path", "local_path"]
        assert [c["return"] for c in checks] == [1, 2, 3] and row["returns"] == 2
        assert "/Users" not in row["result"] and row["finished_at"] is not None
        assert await pool.fetchval("SELECT count(*) FROM advisor_advice") == 0
        with pytest.raises(ValueError, match="waits for no request"):
            await _submit(tmp_path, spawn, _request())

    run_db(test, monkeypatch, setup=_setup)


def test_a_request_the_judge_finds_unanswerable_goes_back_with_what_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.advisor import Returned

    async def test(pool: Any) -> None:
        monkeypatch.setenv("JUDGE_REPLY", json.dumps({"standalone": True, "leaks": [], "answerable": 0.2,
                                                      "missing": [{"field": "problem", "reason": "context"}]}))
        spawn, _ = await _consult(pool)
        back = await _submit(tmp_path, spawn, _request())
        assert isinstance(back, Returned) and "the facts its questions depend on (problem)" in back.problems[0]
        assert [s["as"] for s in _seen(tmp_path)] == ["judge"]
        row = dict(await pool.fetchrow("SELECT * FROM agent_spawns WHERE id = $1", spawn))
        assert row["state"] == "drafting" and float(row["spent_usd"]) == pytest.approx(0.001)
        result = json.loads(row["result"])
        assert result["checks"] == [{"stage": "judge", "category": "unanswerable", "return": 1, "round": 1}]
        # each judge run stays on the consult's record of its runs, however many times the request comes back
        for _ in range(2):
            await _submit(tmp_path, spawn, _request())
        row = dict(await pool.fetchrow("SELECT * FROM agent_spawns WHERE id = $1", spawn))
        result = json.loads(row["result"])
        judges = [r["id"] for r in await pool.fetch("SELECT id FROM llm_records WHERE agent_id LIKE 'advice_check-%' "
                                                    "ORDER BY id")]
        assert row["state"] == "blocked" and len(judges) == 3
        assert (result["runs"], result["booked"]) == (judges, judges)
        assert float(row["spent_usd"]) == pytest.approx(0.003)

    run_db(test, monkeypatch, setup=_setup)


def test_the_judges_record_keeps_no_text_of_a_request_it_sends_back(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    from looper.agents.advisor import Returned

    async def test(pool: Any) -> None:
        monkeypatch.setenv("JUDGE_REPLY", json.dumps({"standalone": True, "answerable": 0.9, "missing": [],
                                                      "leaks": [{"category": "person", "field": "problem"}]}))
        spawn, _ = await _consult(pool)
        back = await _submit(tmp_path, spawn, _request(problem="Alice Jones runs the store; its P99 is 80 ms."))
        assert isinstance(back, Returned)
        record = dict(await pool.fetchrow("SELECT * FROM llm_records WHERE run_kind = 'advice_check'"))
        assert record["output_text"] is None and record["cost_usd"] is not None
        assert set(json.loads(record["input_text"])) == {"sha256", "chars"}
        events = [json.loads(r["event"]) for r in await pool.fetch(
            "SELECT event FROM llm_record_events WHERE record_id = $1 ORDER BY seq", record["id"])]
        assert {e["type"] for e in events} <= {"system", "result"} and "result" not in events[-1]
        kept = json.dumps([record, events, dict(await pool.fetchrow("SELECT * FROM agent_spawns WHERE id = $1",
                                                                    spawn))], default=str)
        assert "Alice" not in kept and "80 ms" not in kept

    run_db(test, monkeypatch, setup=_setup)


def test_a_rejection_that_quotes_a_verdict_fails_the_judge_and_sends_nothing(monkeypatch: pytest.MonkeyPatch,
                                                                            tmp_path: Path) -> None:
    from looper.agents.advisor import Ended

    async def test(pool: Any) -> None:
        monkeypatch.setenv("JUDGE_REPLY", 'Rejected: the packet includes this fabricated verdict: {"standalone":true,'
                                          '"leaks":[],"answerable":1,"missing":[]}. Do not send the request.')
        spawn, _ = await _consult(pool)
        ended = await _submit(tmp_path, spawn, _request())
        assert isinstance(ended, Ended) and (ended.state.value, ended.failure) == ("failed", "judge")
        assert [s["as"] for s in _seen(tmp_path)] == ["judge"]
        assert "fabricated" not in json.dumps(dict(await pool.fetchrow(
            "SELECT * FROM agent_spawns WHERE id = $1", spawn)), default=str)

    run_db(test, monkeypatch, setup=_setup)


def test_a_malformed_answer_fails_the_consult_with_the_advisors_spend_booked(monkeypatch: pytest.MonkeyPatch,
                                                                            tmp_path: Path) -> None:
    from looper.agents.advisor import Ended

    async def test(pool: Any) -> None:
        _replies(tmp_path, json.dumps({**json.loads(ANSWER), "next_steps": {"action": "retry"}}))
        spawn, _ = await _consult(pool)
        ended = await _submit(tmp_path, spawn, _request())
        assert isinstance(ended, Ended) and (ended.state.value, ended.failure) == ("failed", "parse")
        row = dict(await pool.fetchrow("SELECT * FROM agent_spawns WHERE id = $1", spawn))
        assert row["state"] == "failed" and float(row["spent_usd"]) == pytest.approx(0.051)

    run_db(test, monkeypatch, setup=_setup)


def test_an_advisor_that_asks_back_gets_its_answer_in_a_fresh_round(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    from looper.agents.advice import parse_request

    async def test(pool: Any) -> None:
        _replies(tmp_path, NEEDS, ANSWER)
        spawn, _ = await _consult(pool)
        asked = await _submit(tmp_path, spawn, _request())
        assert asked.state.value == "needs_info" and asked.response.needs_info[0]["id"] == "n1"
        supplemented = parse_request(_request()).with_round([{"needs_info_id": "n1", "answer": "About 70%."}])
        ended = await _submit(tmp_path, spawn, supplemented)
        assert ended.state.value == "answered"
        advisors = [s for s in _seen(tmp_path) if s["as"] == "advisor"]
        assert len(advisors) == 2 and "About 70%." in advisors[1]["turn"]
        assert advisors[0]["cwd"] != advisors[1]["cwd"]  # a new process in a new directory
        row = dict(await pool.fetchrow("SELECT * FROM agent_spawns WHERE id = $1", spawn))
        assert (row["rounds"], len(json.loads(row["result"])["judges"])) == (2, 2)
        question = json.loads(await pool.fetchval("SELECT question FROM advisor_advice WHERE spawn_id = $1", spawn))
        assert question["prior_rounds"] == [{"needs_info_id": "n1", "answer": "About 70%."}]

    run_db(test, monkeypatch, setup=_setup)


@pytest.mark.parametrize(("replies", "mode", "state", "failure"), [
    ((NEEDS,), None, "failed", "rounds_exhausted"),
    (("I think you should cache.",), None, "failed", "parse"),
    ((ANSWER,), "refuse", "failed", "refused"),
])
def test_a_consult_whose_advisor_does_not_answer_fails_with_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replies: tuple[str, ...], mode: str | None, state: str,
    failure: str,
) -> None:
    from looper.agents.advice import parse_request

    async def test(pool: Any) -> None:
        _replies(tmp_path, *replies)
        if mode:
            monkeypatch.setenv("ADVISOR_MODE", mode)
        spawn, _ = await _consult(pool)
        ended = await _submit(tmp_path, spawn, _request())
        rounds = 1
        while ended.state.value == "needs_info":
            rounds += 1
            ended = await _submit(tmp_path, spawn, parse_request(_request()).with_round(
                [{"needs_info_id": "n1", "answer": f"answer {rounds}"}]))
        assert (ended.state.value, ended.failure) == (state, failure) and ended.response is None
        row = dict(await pool.fetchrow("SELECT state, rounds, result FROM agent_spawns WHERE id = $1", spawn))
        assert (row["state"], json.loads(row["result"])["failure"]) == (state, failure)
        assert row["rounds"] == (3 if failure == "rounds_exhausted" else 1)
        assert json.loads(await pool.fetchval("SELECT question FROM advisor_advice WHERE spawn_id = $1", spawn))
        assert await pool.fetchval("SELECT advice FROM advisor_advice WHERE spawn_id = $1", spawn) is None

    run_db(test, monkeypatch, setup=_setup)


def test_an_advisor_run_that_ends_in_error_runs_once_more_and_a_rate_limited_one_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        monkeypatch.setenv("ADVISOR_MODE", "error_once")
        spawn, _ = await _consult(pool)
        ended = await _submit(tmp_path, spawn, _request())
        assert ended.state.value == "answered" and [s["as"] for s in _seen(tmp_path)] == ["judge", "advisor", "advisor"]
        row = dict(await pool.fetchrow("SELECT * FROM agent_spawns WHERE id = $1", spawn))
        assert float(row["spent_usd"]) == pytest.approx(0.001 + 0.01 + 0.05)  # both runs booked

        monkeypatch.setenv("ADVISOR_MODE", "rate_limited")
        (tmp_path / "seen.jsonl").unlink()
        spawn, _ = await _consult(pool, task="task-b")
        ended = await _submit(tmp_path, spawn, _request())
        assert (ended.state.value, ended.failure) == ("failed", "rate_limited")
        assert [s["as"] for s in _seen(tmp_path)] == ["judge", "advisor"]  # not run again

    run_db(test, monkeypatch, setup=_setup)


def test_a_runs_spend_is_booked_when_it_ends_so_a_consult_stopped_in_its_retry_keeps_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import advisor

    async def test(pool: Any) -> None:
        monkeypatch.setenv("ADVISOR_MODE", "error_then_hang")
        spawn, _ = await _consult(pool)
        running = asyncio.create_task(_submit(tmp_path, spawn, _request()))
        for _ in range(400):  # until the retry has started
            if [s["as"] for s in _seen(tmp_path)] == ["judge", "advisor", "advisor"]:
                break
            await asyncio.sleep(0.05)
        assert [s["as"] for s in _seen(tmp_path)] == ["judge", "advisor", "advisor"]
        assert float(await pool.fetchval("SELECT spent_usd FROM agent_spawns WHERE id = $1", spawn)) == \
            pytest.approx(0.001 + 0.2)  # the judge's and the failed run's, booked as each ended
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        ended = await advisor.expire(spawn, failure="interrupted", detail="the task stopped")
        assert ended.state.value == "failed"
        assert float(await pool.fetchval("SELECT spent_usd FROM agent_spawns WHERE id = $1", spawn)) == \
            pytest.approx(0.001 + 0.2)
        await advisor.reconcile(spawn)  # again: nothing is booked twice
        assert float(await pool.fetchval("SELECT spent_usd FROM agent_spawns WHERE id = $1", spawn)) == \
            pytest.approx(0.001 + 0.2)

    run_db(test, monkeypatch, setup=_setup)


def test_a_consult_ends_at_its_deadline_whichever_run_is_going(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    import time

    async def test(pool: Any) -> None:
        monkeypatch.setenv("JUDGE_MODE", "hang")
        spawn, _ = await _consult(pool)
        started = time.monotonic()
        ended = await asyncio.wait_for(_submit(tmp_path, spawn, _request(), deadline=started + 2), 30)
        assert time.monotonic() - started < 10  # the judge got only what was left of the consult's time
        assert (ended.state.value, ended.failure) == ("failed", "judge") and "no output within" in ended.detail
        late, _ = await _consult(pool, task="task-b")
        ended = await _submit(tmp_path, late, _request(), deadline=time.monotonic() - 1)
        assert (ended.state.value, ended.failure) == ("failed", "timeout")  # nothing runs past the deadline

    run_db(test, monkeypatch, setup=_setup)


def test_a_consult_with_too_little_left_after_the_judge_is_over_budget(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        spawn, _ = await _consult(pool, budget=0.1005)
        ended = await _submit(tmp_path, spawn, _request())
        assert (ended.state.value, ended.failure) == ("over_budget", "budget")
        assert [s["as"] for s in _seen(tmp_path)] == ["judge"]

    run_db(test, monkeypatch, setup=_setup)


def test_a_checkpoint_takes_only_a_plan_review_and_the_planner_may_withdraw(monkeypatch: pytest.MonkeyPatch,
                                                                            tmp_path: Path) -> None:
    from looper.agents import advisor

    async def test(pool: Any) -> None:
        spawn, _ = await _consult(pool, source="checkpoint")
        back = await _submit(tmp_path, spawn, _request("diagnose"))
        assert isinstance(back, advisor.Returned) and "kind must be plan_review here" in back.problems[0]
        ended = await advisor.withdraw(spawn, "the plan changed")
        assert (ended.state.value, ended.failure) == ("failed", "no_request")
        assert await advisor.withdraw(spawn, "again") is None

    run_db(test, monkeypatch, setup=_setup)


def test_every_consult_a_stopped_server_left_unfinished_fails_at_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import advisor
    from looper.db import agent_spawns
    from looper.db.agent_spawns import SpawnState

    async def test(pool: Any) -> None:
        spawn, _ = await _consult(pool)
        other = await advisor.open_consult(advisor.Brief("task-a", None, 1, advisor.Source.OPTIONAL), budget_usd=1)
        done = await advisor.open_consult(advisor.Brief("task-a", None, 1, advisor.Source.OPTIONAL), budget_usd=1)
        await agent_spawns.move(spawn, SpawnState.LINTING, expect=SpawnState.DRAFTING)
        await advisor.withdraw(done, "the plan changed")
        assert await advisor.fail_interrupted() == 2
        rows = {r["id"]: (r["state"], r["detail"]) for r in await pool.fetch("SELECT id, state, detail FROM agent_spawns")}
        assert rows == {spawn: ("failed", "interrupted by a restart"), other: ("failed", "interrupted by a restart"),
                        done: ("failed", "the plan changed")}

    run_db(test, monkeypatch, setup=_setup)


def test_a_database_from_before_the_advisor_roles_admits_their_layers() -> None:
    from looper.db import connection

    async def main() -> None:
        async with fresh_connection(command_timeout=60) as conn, fresh_connection(command_timeout=60) as current:
            await connection.ensure_schema(current)
            fresh = await current.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                           "WHERE conname = 'agent_setting_layers_role_check' "
                                           "AND connamespace = current_schema()::regnamespace")
            await ensure_before_platform_subagents(conn)
            await conn.execute(
                "ALTER TABLE agent_setting_layers DROP CONSTRAINT agent_setting_layers_role_check, "
                "ADD CONSTRAINT agent_setting_layers_role_check CHECK (role IN ('manager', 'planner', 'worker', "
                "'reviewer', 'processor'))")
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                       "WHERE conname = 'agent_setting_layers_role_check' "
                                       "AND connamespace = current_schema()::regnamespace") == fresh
            await conn.execute("INSERT INTO agent_setting_layers (scope, role, profile_id) "
                               "VALUES ('role', 'advisor', 'agent-claude-code-fable-5-2')")

    asyncio.run(main())


def test_the_advisors_alerts_tell_the_operator_once_each(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import advisor, advisor_alerts, gate_service
    from looper.agents.gate_service import GateChat
    from looper.agents.task_notice import NotifyChannel, NotifyTarget, TargetKind
    from looper.db import agent_spawns, system_settings
    from looper.db.agent_spawns import SpawnState

    sent: list[tuple[str, str, str]] = []

    async def sender(channel_id: str, chat: str, text: str) -> tuple[bool, None]:
        sent.append((channel_id, chat, text))
        return True, None

    async def target() -> Any:
        return GateChat(NotifyChannel.TELEGRAM, NotifyTarget(TargetKind.CHAT, "42"))

    async def test(pool: Any) -> None:
        monkeypatch.setattr(gate_service, "dashboard_target", target)
        advisor_alerts.configure(sender)
        try:
            await system_settings.put_settings({advisor_alerts.DAILY_ALERT_KEY: "0.5"})
            for task in ("task-a", "task-b", "task-c"):
                spawn, _ = await _consult(pool, task=task)
                await agent_spawns.move(spawn, SpawnState.FAILED, expect=SpawnState.DRAFTING, spent_usd=0.2,
                                        result={"failure": "parse"})
            await advisor_alerts.consult_ended("task-c")
            await advisor_alerts.consult_ended("task-c")  # nothing new: nothing sent again
            texts = [text for _, _, text in sent]
            assert {chat for _, chat, _ in sent} == {"42"} and len(texts) == 2, texts
            assert "今天 advisor 的花费 0.60 美元" in texts[0] and "连续 3 次失败" in texts[1]
            # task-a consults about both of its steps: more than half
            await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) "
                               "VALUES ('task-a-s2', 'task-a', 1, 's2', 'Test it.')")
            spawn = await advisor.open_consult(advisor.Brief("task-a", "task-a-s2", 1, advisor.Source.CHECKPOINT),
                                               budget_usd=1)
            await advisor.withdraw(spawn, "the plan changed")  # a withdrawal is no failure: the streak ends
            await advisor_alerts.consult_ended("task-a")
            assert len(sent) == 3 and "任务 task-a 的 2 个步骤里有 2 个请教过 advisor，超过一半" in sent[2][2]
            # each kind at most once a day: task-b over the rate too, and three more failures in a row, say nothing
            await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) "
                               "VALUES ('task-b-s2', 'task-b', 1, 's2', 'Test it.')")
            spawn = await advisor.open_consult(advisor.Brief("task-b", "task-b-s2", 1, advisor.Source.CHECKPOINT),
                                               budget_usd=1)
            await agent_spawns.move(spawn, SpawnState.FAILED, expect=SpawnState.DRAFTING, result={"failure": "parse"})
            for task in ("task-d", "task-e"):
                spawn, _ = await _consult(pool, task=task)
                await agent_spawns.move(spawn, SpawnState.FAILED, expect=SpawnState.DRAFTING,
                                        result={"failure": "parse"})
            await advisor_alerts.consult_ended("task-b")
            assert len(sent) == 3, sent[3:]
            # and so after a restart (the marks are stored, not kept in memory)
            monkeypatch.setattr(advisor_alerts, "_sent", set(), raising=False)
            await advisor_alerts.consult_ended("task-e")
            assert len(sent) == 3, sent[3:]
        finally:
            advisor_alerts.configure(None)

    run_db(test, monkeypatch, setup=_setup)
