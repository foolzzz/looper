"""Project-skill usage against PostgreSQL: loaded, invoked, first-pass against a control, and the review reasons.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Records and attempts are synthetic.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def _skill(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "description": f"Use for {name}.", "content": extra.pop("content", f"Reply {name}.\n"), **extra}


class World:
    """A project with one skill, its tasks, attempts and Worker runs."""

    def __init__(self, pool: Any, project_id: str, skill: dict[str, Any], sha: str) -> None:
        self.pool, self.project_id, self.skill, self.sha = pool, project_id, skill, sha
        self.name = skill["name"]
        self.steps = 0

    def items(self, *, loads: bool = True, sha: str | None = None, plugin: str = "project",
              other: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        listed = [{"kind": "skill", "name": n, "sha256": "f" * 64, "plugin": "personal", "scope": "global"} for n in other]
        if loads:
            listed.append({"kind": "skill", "name": self.name, "sha256": sha or self.sha, "plugin": plugin,
                           "scope": "project"})
        return listed

    async def run(self, task: str, *, attempt: int | None, at: datetime, runtime: str = "claude-code",
                  loads: bool = True, sha: str | None = None, plugin: str = "project", other: tuple[str, ...] = (),
                  calls: tuple[str, ...] = (), agent: str = "worker-1", cost: float | None = 0.5,
                  project_id: str | None = None, events: int = 1) -> int:
        metadata = {"library": {"project_id": project_id or self.project_id,
                                "items": self.items(loads=loads, sha=sha, plugin=plugin, other=other)},
                    "profile": {"runtime": {"type": runtime}}}
        record = await self.pool.fetchval(
            "INSERT INTO llm_records (agent_id, task_id, metadata, created_at, attempt_id, cost_usd) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id", agent, task, json.dumps(metadata), at, attempt, cost)
        stream = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "step"}]}}] * events
        stream += [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"toolu_{i}", "name": "Skill", "input": {"skill": call}}]}}
            for i, call in enumerate(calls)]
        await self.pool.executemany(
            "INSERT INTO llm_record_events (record_id, seq, event) VALUES ($1, $2, $3)",
            [(record, seq, json.dumps(event)) for seq, event in enumerate(stream, 1)])
        return record

    async def task(self, name: str | None = None) -> str:
        name = name or f"task-{self.project_id}"
        await self.pool.execute("INSERT INTO tasks (id, requirement, project_id) VALUES ($1, 'Synthetic task', $2) "
                                "ON CONFLICT DO NOTHING", name, self.project_id)
        return name

    async def attempt(self, task: str, key: str, number: int, trigger: str, decision: str | None, at: datetime) -> int:
        decided = None if decision is None else at
        return await self.pool.fetchval(
            "INSERT INTO step_attempts (task_id, step_key, task_run_no, attempt, trigger, decision, decided_at, "
            "started_at, worker_runs) VALUES ($1, $2, 1, $3, $4, $5, $6, $7, 1) RETURNING id",
            task, key, number, trigger, decision, decided, at)

    async def step(self, attempts: list[tuple[str, str | None]], *, loads: bool, at: datetime, **run: Any) -> list[int]:
        """One step with its attempts, each with a Worker run; returns the attempt ids."""
        task = await self.task()
        self.steps += 1
        ids = []
        for number, (trigger, decision) in enumerate(attempts, 1):
            attempt = await self.attempt(task, f"s{self.steps}", number, trigger, decision, at)
            await self.run(task, attempt=attempt, at=at, loads=loads, **run)
            ids.append(attempt)
        return ids

    async def usage(self) -> dict[str, Any]:
        from looper.agents import skill_usage

        rows = await skill_usage.usage(self.project_id, now=NOW)
        return next(row for row in rows if row.name == self.name).view()


async def _world(pool: Any, *, name: str = "deploy-check", project: str = "calc", **skill: Any) -> World:
    from looper.agents import project_skills
    from looper.db import projects

    created = await projects.create_project(name=project, pool=pool)
    added = await project_skills.add_skill(created["id"], _skill(name, **skill))
    sha = await pool.fetchval("SELECT sha256 FROM library_items WHERE id = $1", added["skill"]["skill_id"])
    return World(pool, created["id"], added["skill"], sha)


def test_versions_that_share_a_hash_are_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import project_skills, skill_versions

    async def _test(pool: Any) -> None:
        world = await _world(pool, content="one\n")
        skill_id = world.skill["skill_id"]
        await project_skills.update_skill(world.project_id, skill_id, {"content": "two\n"})
        await skill_versions.rollback(world.project_id, skill_id, 1)
        from looper.agents import skill_usage

        rows = await skill_usage.usage(world.project_id, now=NOW)
        assert [(row.name, row.versions) for row in rows] == [("deploy-check", [1, 3])]
        assert rows[0].sha256 == await pool.fetchval("SELECT sha256 FROM skill_versions WHERE version = 1")

    run_db(_test, monkeypatch)


def test_first_pass_is_first_attempt_accepted_alone_and_joined_by_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    at = NOW - 2 * DAY

    async def _test(pool: Any) -> None:
        world = await _world(pool)
        for _ in range(12):
            await world.step([("first", "accept")], loads=True, at=at)
        for _ in range(4):  # retried: the first attempt did not pass, whatever the second did
            await world.step([("first", "retry"), ("retry", "accept")], loads=True, at=at)
        for _ in range(3):  # reopened afterwards: a second attempt exists, so it is not a first pass
            await world.step([("first", "accept"), ("reopen_verification", "accept")], loads=True, at=at)
        for _ in range(3):
            await world.step([("first", "fail")], loads=True, at=at)
        await world.step([("first", "skip")], loads=True, at=at)  # a skipped step says nothing
        await world.step([("replanned", "accept")], loads=True, at=at)  # not an original-plan step
        for passed in (True,) * 10 + (False,) * 10:
            await world.step([("first", "accept" if passed else "fail")], loads=False, at=at)
        # A Planner run that lists the skill is no attempt of a Worker: it must not make the attempt loaded.
        control = (await world.step([("first", "accept")], loads=False, at=at))[0]
        await world.run(await world.task(), attempt=control, at=at, agent="planner-t", loads=True)
        # An attempt that ran an older version of the skill is neither with it nor a control.
        await world.step([("first", "accept")], loads=True, at=at, sha="a" * 64)

        row = await world.usage()
        assert row["loaded_attempts"] == 12 + 8 + 6 + 3 + 1 + 1
        effect = row["effect"]
        assert (effect["first_pass"]["n"], effect["first_pass"]["successes"], effect["first_pass"]["state"]) == (
            22, 12, "ok")
        assert (effect["control"]["n"], effect["control"]["successes"]) == (21, 11)
        low, high = effect["first_pass"]["low"], effect["first_pass"]["high"]
        assert low < 12 / 22 < high
        assert effect["cost"]["loaded"] == {"avg": 0.5, "n": 12}

    run_db(_test, monkeypatch)


def test_invocation_is_known_only_where_the_stream_can_tell(monkeypatch: pytest.MonkeyPatch) -> None:
    at = NOW - DAY

    async def _test(pool: Any) -> None:
        world = await _world(pool)
        qualified = f"project:{world.name}"
        await world.step([("first", "accept")], loads=True, at=at, calls=(qualified,))
        await world.step([("first", "accept")], loads=True, at=at, calls=(world.name,))  # the bare name resolves too
        await world.step([("first", "accept")], loads=True, at=at, other=(world.name,), calls=(world.name,))  # ambiguous
        await world.step([("first", "accept")], loads=True, at=at)  # Claude Code, no call
        await world.step([("first", "accept")], loads=True, at=at, calls=("something-else",))
        for runtime in ("codex", "grok", "omp"):
            await world.step([("first", "accept")], loads=True, at=at, runtime=runtime, calls=(qualified,))
        row = await world.usage()
        assert row["loaded_attempts"] == 8
        assert row["invoked"] == {"count": 2, "n": 5, "unknown": 3, "rate": None}  # fewer than 20: counts only

    run_db(_test, monkeypatch)


def test_a_skill_of_another_project_or_another_origin_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    at = NOW - DAY

    async def _test(pool: Any) -> None:
        world = await _world(pool)
        elsewhere = await _world(pool, project="other")
        # The same name and content in the other project: its runs belong to its tasks.
        assert elsewhere.sha == world.sha
        await elsewhere.step([("first", "accept")], loads=True, at=at, calls=(f"project:{world.name}",))
        # A catalog skill of the same name and content installed in this project is not its own skill.
        await world.step([("first", "accept")], loads=True, at=at, plugin="personal")
        await world.step([("first", "accept")], loads=True, at=at, sha="b" * 64)
        row = await world.usage()
        assert (row["loaded_runs"], row["loaded_attempts"]) == (0, 0)
        assert (await elsewhere.usage())["loaded_attempts"] == 1

    run_db(_test, monkeypatch)


@pytest.mark.parametrize("loaded,control,state", [(19, 20, "insufficient"), (20, 19, "insufficient"), (20, 20, "ok")])
def test_twenty_attempts_on_each_side_is_the_boundary(
    monkeypatch: pytest.MonkeyPatch, loaded: int, control: int, state: str,
) -> None:
    async def _test(pool: Any) -> None:
        world = await _world(pool)
        for _ in range(loaded):
            await world.step([("first", "accept")], loads=True, at=NOW - DAY)
        for _ in range(control):
            await world.step([("first", "fail")], loads=False, at=NOW - DAY)
        effect = (await world.usage())["effect"]
        assert effect["first_pass"]["state"] == effect["control"]["state"] == state
        assert (effect["first_pass"]["n"], effect["control"]["n"]) == (loaded, control)
        if state == "insufficient":
            assert effect["first_pass"]["value"] is None and effect["cost"]["loaded"]["avg"] is None
        else:
            assert effect["first_pass"]["value"] == 1.0 and effect["control"]["value"] == 0.0

    run_db(_test, monkeypatch)


def test_the_window_edges_are_seven_and_thirty_five_days(monkeypatch: pytest.MonkeyPatch) -> None:
    second = timedelta(seconds=1)

    async def _test(pool: Any) -> None:
        world = await _world(pool)
        for at in (NOW - 7 * DAY, NOW - second):  # in the window
            await world.step([("first", "accept")], loads=True, at=at)
        for at in (NOW - 7 * DAY - second, NOW - 35 * DAY):  # the 28 days before it
            await world.step([("first", "accept")], loads=True, at=at)
        for at in (NOW - 35 * DAY - second, NOW):  # outside both
            await world.step([("first", "accept")], loads=True, at=at)
        row = await world.usage()
        assert row["loaded_attempts"] == 2 and row["comparison"]["loaded_attempts"] == 2
        assert (row["window"]["start"], row["comparison_window"]["start"]) == (
            (NOW - 7 * DAY).isoformat(), (NOW - 35 * DAY).isoformat())

    run_db(_test, monkeypatch)


def test_without_step_attempts_only_loads_and_calls_are_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        world = await _world(pool)
        task = await world.task()
        await world.run(task, attempt=None, at=NOW - DAY, calls=(f"project:{world.name}",))
        await world.run(task, attempt=None, at=NOW - DAY, runtime="codex")
        await pool.execute("DROP TABLE step_attempts CASCADE")
        row = await world.usage()
        assert row["effect"] is None and row["loaded_attempts"] is None and row["coverage_since"] is None
        assert row["loaded_runs"] == 2
        assert row["invoked"] == {"count": 1, "n": 1, "unknown": 1, "rate": None}

    run_db(_test, monkeypatch)


def test_runs_before_attempts_count_as_loaded_runs_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        world = await _world(pool)
        task = await world.task()
        await world.run(task, attempt=None, at=NOW - DAY)
        await world.step([("first", "accept")], loads=True, at=NOW - DAY)
        row = await world.usage()
        assert (row["loaded_runs"], row["loaded_attempts"]) == (2, 1)
        assert row["coverage_since"] == (NOW - DAY).isoformat()

    run_db(_test, monkeypatch)


@pytest.mark.parametrize("attempts,expected", [(30, []), (31, ["never_invoked"])])
def test_never_invoked_needs_more_than_thirty_attempts_that_can_tell(
    monkeypatch: pytest.MonkeyPatch, attempts: int, expected: list[str],
) -> None:
    async def _test(pool: Any) -> None:
        world = await _world(pool)
        for _ in range(attempts):
            await world.step([("first", "accept")], loads=True, at=NOW - DAY)
        assert (await world.usage())["review"] == expected

    run_db(_test, monkeypatch)


def test_unknown_runtimes_are_never_never_invoked(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        world = await _world(pool)
        for _ in range(35):
            await world.step([("first", "accept")], loads=True, at=NOW - DAY, runtime="codex")
        row = await world.usage()
        assert row["review"] == [] and row["invoked"]["unknown"] == 35 and row["invoked"]["n"] == 0

    run_db(_test, monkeypatch)


@pytest.mark.parametrize("called,runtime,old,expected", [
    (False, "claude-code", True, ["stale"]),
    (True, "claude-code", True, []),  # called once in the 90 days
    (False, "codex", True, []),  # a runtime that cannot tell
    (False, "claude-code", False, []),  # too new to have had 90 days
])
def test_stale_needs_ninety_days_of_a_runtime_that_can_tell(
    monkeypatch: pytest.MonkeyPatch, called: bool, runtime: str, old: bool, expected: list[str],
) -> None:
    async def _test(pool: Any) -> None:
        world = await _world(pool)
        if old:
            await pool.execute("UPDATE library_items SET imported_at = $2 WHERE id = $1",
                               world.skill["skill_id"], NOW - 120 * DAY)
        task = await world.task()
        await world.run(task, attempt=None, at=NOW - 60 * DAY, runtime=runtime,
                        calls=(f"project:{world.name}",) if called else ())
        await world.run(task, attempt=None, at=NOW - 3 * DAY, runtime=runtime)
        assert (await world.usage())["review"] == expected

    run_db(_test, monkeypatch)


def test_divergent_intervals_flag_a_review_and_overlapping_ones_do_not(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        world = await _world(pool)
        for _ in range(20):
            await world.step([("first", "accept")], loads=True, at=NOW - DAY, calls=(f"project:{world.name}",))
            await world.step([("first", "fail")], loads=False, at=NOW - DAY)
        row = await world.usage()
        assert row["review"] == ["divergent"]
        assert row["effect"]["first_pass"]["low"] > row["effect"]["control"]["high"]

    run_db(_test, monkeypatch)


def test_overlapping_intervals_and_a_called_skill_have_no_review_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        world = await _world(pool)
        for i in range(20):
            await world.step([("first", "accept" if i % 2 else "fail")], loads=True, at=NOW - DAY,
                             calls=(f"project:{world.name}",))
            await world.step([("first", "accept" if i % 2 else "fail")], loads=False, at=NOW - DAY)
        row = await world.usage()
        assert row["review"] == [] and row["effect"]["first_pass"]["state"] == "ok"
        assert row["invoked"]["count"] == 20 and row["invoked"]["rate"] == 1.0

    run_db(_test, monkeypatch)


def test_the_operation_is_a_read_any_caller_may_make_and_matches_its_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.api_v1 import validate_arguments
    from looper.agents.v1_domains.project_skills import OPERATIONS

    async def _test(pool: Any) -> None:
        world = await _world(pool)
        await world.step([("first", "accept")], loads=True, at=datetime.now(timezone.utc) - DAY,
                         calls=(f"project:{world.name}",))
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("project_skills.usage", {"project_id": world.project_id}, client=True)
            assert status == 200 and answer["result"]["skills"][0]["name"] == world.name  # RFC section 10: a read
            status, answer = await call("project_skills.usage", {"project_id": world.project_id})
            assert status == 200 and answer["status"] == "ok", answer
            result = answer["result"]
            assert result["skills"][0]["loaded_attempts"] == 1 and result["skills"][0]["invoked"]["count"] == 1
            assert "not a controlled experiment" in result["skills"][0]["note"]
            status, answer = await call("project_skills.usage", {"project_id": "missing"})
            assert status == 404, answer
        operation = next(op for op in OPERATIONS if op.operation_id == "project_skills.usage")
        validate_arguments({"type": "object", "properties": {"result": operation.result}, "required": ["result"]},
                           {"result": result})
        assert not operation.operator_only and len(operation.summary) <= 120

    run_db(_test, monkeypatch)


def test_two_thousand_runs_of_fifty_events_are_read_in_a_fraction_of_a_second(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import skill_usage

    async def _test(pool: Any) -> None:
        world = await _world(pool)
        task = await world.task()
        prompt = "x" * 20000  # a run record holds its system prompt: reading must not scan it needlessly
        rows, events = [], []
        for i in range(2000):
            metadata = {"library": {"project_id": world.project_id, "items": world.items()},
                        "profile": {"runtime": {"type": "claude-code"}}, "system_prompt": prompt}
            rows.append((i + 1, "worker-1", task, json.dumps(metadata), NOW - timedelta(minutes=i)))
        await pool.executemany(
            "INSERT INTO llm_records (id, agent_id, task_id, metadata, created_at) VALUES ($1, $2, $3, $4, $5)", rows)
        for i in range(2000):
            for seq in range(1, 51):
                call = [{"type": "tool_use", "id": "t", "name": "Skill", "input": {"skill": f"project:{world.name}"}}]
                events.append((i + 1, seq, json.dumps({"type": "assistant", "message": {
                    "content": call if seq == 25 else [{"type": "text", "text": "step " * 20}]}})))
        await pool.executemany("INSERT INTO llm_record_events (record_id, seq, event) VALUES ($1, $2, $3)", events)
        await pool.execute("ANALYZE llm_records; ANALYZE llm_record_events")
        started = time.perf_counter()
        found = await skill_usage.usage(world.project_id, now=NOW + timedelta(minutes=1))
        elapsed = time.perf_counter() - started
        print(f"usage over 2000 runs x 50 events: {elapsed * 1000:.0f} ms")
        assert found[0].view()["loaded_runs"] > 0 and found[0].view()["invoked"]["count"] == 0
        assert elapsed < 3, elapsed  # target 0.5 s; the margin is for a loaded machine

    run_db(_test, monkeypatch, timeout=300)
