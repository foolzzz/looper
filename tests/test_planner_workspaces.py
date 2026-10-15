"""A Planner in a project with step workspaces: steps that run together work in worktrees of their own, and an
accepted step's branch is merged into the task directory's branch before the steps that depend on it start.

The Planner's Claude is the scripted stand-in and the Workers a stand-in pool
(``test_planner_agent``'s ``Harness``) that writes the step's file in the
directory its dispatch names, the way a Worker would; the task directory is a
real git repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from looper.agents import step_workspaces as ws
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.tiers.planner import PlannerAgent
from test_planner_agent import (
    REVIEW_PASS,
    WORK_OK,
    Harness,
    activity,
    decide,
    drive,
    plan,
    reply,
    step,
)


@pytest.fixture(autouse=True)
def _git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(cwd: Path | str, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.rstrip()


def init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "User")
    git(path, "config", "user.email", "user@example.com")
    (path / "shared.txt").write_text("a\nb\nc\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return git(path, "rev-parse", "HEAD")


def project(**execution: Any) -> dict[str, Any]:
    return {"id": "proj-1", "name": "calc", "description": "", "work_dir": "/srv/calc", "background": "",
            **execution}


def looper_branches(repo: Path) -> list[str]:
    return git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/looper/").split()


def writes(seen: dict[str, Any], *, commit: tuple[str, ...] = (), content: dict[str, str] | None = None) -> Any:
    """A pool whose Worker writes ``<step>.txt`` (or ``content``'s file) where its dispatch says and reports it;
    ``seen`` keeps each dispatch's directory and what it found there."""

    def outcome(envelope: MessageEnvelope) -> dict[str, Any] | None:
        key = envelope.payload["step_key"]
        if envelope.message_type == MessageType.VERIFY_REQUEST:
            seen.setdefault("reviews", []).append(envelope.payload["context"])
            return REVIEW_PASS
        where = Path(envelope.payload["context"]["work_dir"])
        seen.setdefault(key, []).append({"context": envelope.payload["context"],
                                         "files": sorted(p.name for p in where.iterdir() if p.name != ".git")})
        name, text = next(iter((content or {f"{key}.txt": f"{key}\n"}).items()))
        (where / name).write_text(text)
        if key in commit:
            git(where, "add", "-A")
            git(where, "commit", "-qm", f"worker: {key}")
        return {**WORK_OK, "changed_files": {name: "written"}}

    return outcome


@pytest.mark.usefixtures("agent_db")
class TestStepWorkspaces:
    def test_parallel_steps_work_in_worktrees_and_are_merged_before_the_step_that_joins_them(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        agent_db.get_task_project.return_value = project(step_workspaces="worktree")
        claude = planner_claude(plan(step("s1"), step("s2"), step("s3", "s1", "s2")),
                                decide("accept"), decide("accept"), decide("accept"))
        seen: dict[str, Any] = {}
        checks: dict[str, Any] = {}

        async def scenario(h: Harness) -> None:
            start = init_repo(h.work_dir)
            (h.work_dir / "pending.txt").write_text("an earlier change, not committed\n")
            await h.start()
            await h.finished()
            repo = h.work_dir
            checks["log"] = git(repo, "log", "--first-parent", "--format=%an|%s", "main").splitlines()
            checks["status"] = git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
            checks["start"] = start
            checks["branches"] = looper_branches(repo)
            checks["worktrees"] = git(repo, "worktree", "list", "--porcelain").count("worktree ")
            checks["root"] = h.work_dir.parent

        h = drive(claude, writes(seen, commit=("s1",)), scenario)
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        one, two, three = (seen[key][0]["context"] for key in ("s1", "s2", "s3"))
        # s1 and s2 started together, each in its own worktree from the checkpoint; s3, alone, in the task dir.
        assert h.dispatched_at_turn == [1, 1, 3]
        assert one["workspace"]["branch"] == "looper/t1/s1-1" and two["workspace"]["branch"] == "looper/t1/s2-2"
        assert Path(one["work_dir"]) == checks["root"] / "worktrees" / "t1" / "s1-1"
        assert one["workspace"]["base_commit"] == two["workspace"]["base_commit"]
        assert one["workspace"]["target_branch"] == "main"
        assert "pending.txt" in seen["s1"][0]["files"] and "s2.txt" not in seen["s1"][0]["files"]
        assert three == {"work_dir": str(h.work_dir)}
        assert {"s1.txt", "s2.txt"} <= set(seen["s3"][0]["files"])  # both merged before s3 started
        # The branch: the checkpoint, then a merge commit per step; s3's change is the task directory's.
        assert checks["log"][-1] == "User|init"
        assert checks["log"][-2] == "Looper|looper: checkpoint before parallel steps"
        assert sorted(checks["log"][:2]) == ["Looper|looper: merge step s1: do s1", "Looper|looper: merge step s2: do s2"]
        assert checks["status"] == ["?? s3.txt"]
        # Everything the task made is gone once it completed; where it started is recorded.
        assert checks["branches"] == [] and checks["worktrees"] == 1
        assert not (checks["root"] / "worktrees").exists() or not any((checks["root"] / "worktrees").iterdir())
        assert agent_db.store.plans["t1"]["start_commit"] == checks["start"]
        assert agent_db.store.plans["t1"]["start_branch"] == "refs/heads/main"
        notes = activity(agent_db)
        assert any(n.startswith("Checkpoint ") and "1 pending file(s)" in n for n in notes)
        assert sum("merged into main as" in n for n in notes) == 2
        states = [row["state"] for row in agent_db.store.workspaces.values()]
        assert states == ["merged", "merged"]

    def test_a_step_that_conflicts_goes_back_to_the_planner_and_its_retry_starts_from_the_branch(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        agent_db.get_task_project.return_value = project(step_workspaces="worktree")
        claude = planner_claude(
            plan(step("s1"), step("s2")),
            decide("accept"),  # s1
            decide("accept"),  # s2: its merge conflicts
            decide("retry", reason="merge conflict", feedback="keep s1's line and add yours"),
            decide("accept"),  # s2's retry
        )
        seen: dict[str, list[Any]] = {}
        checks: dict[str, Any] = {}

        def outcome(envelope: MessageEnvelope) -> dict[str, Any] | None:
            key, context = envelope.payload["step_key"], envelope.payload["context"]
            seen.setdefault(key, []).append(context)
            where = Path(context["work_dir"])
            if key == "s1":
                (where / "shared.txt").write_text("a\nfrom s1\nc\n")
                return WORK_OK
            if len(seen["s2"]) == 1:
                (where / "shared.txt").write_text("a\nfrom s2\nc\n")
                return None  # held until s1 is merged
            (where / "shared.txt").write_text((where / "shared.txt").read_text() + "from s2\n")
            return WORK_OK

        async def scenario(h: Harness) -> None:
            init_repo(h.work_dir)
            await h.start()
            await h.until(lambda: len(h.held) == 1 and any(
                "s1 merged into main" in n for n in activity(agent_db)))
            await h.release(WORK_OK)
            await h.finished()
            checks["shared"] = (h.work_dir / "shared.txt").read_text()
            checks["log"] = git(h.work_dir, "log", "--first-parent", "--format=%s", "main").splitlines()
            checks["branches"] = looper_branches(h.work_dir)

        h = drive(claude, outcome, scenario)
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        texts = [turn["text"] for turn in claude.turns()]
        conflict = texts[3]
        assert conflict.startswith("[DECISION] Step s2")
        assert "Looper could not merge its branch looper/t1/s2-2 into main" in conflict
        assert "- why: its changes conflict with what the branch has now (conflict)" in conflict
        assert "- files: shared.txt" in conflict
        assert "Allowed decisions now: retry (2 left), replan, fail." in conflict
        # The retry has a new worktree from main as it is now, and is told where the old attempt's commits are.
        first, retry = seen["s2"]
        assert retry["workspace"]["branch"] == "looper/t1/s2-3" and retry["work_dir"] != first["work_dir"]
        assert (retry["workspace"]["previous_branch"], retry["workspace"]["previous_error"],
                retry["workspace"]["previous_files"]) == ("looper/t1/s2-2", "conflict", ["shared.txt"])
        assert retry["workspace"]["base_commit"] != first["workspace"]["base_commit"]
        assert checks["shared"] == "a\nfrom s1\nc\nfrom s2\n"
        assert checks["log"][:2] == ["looper: merge step s2: do s2", "looper: merge step s1: do s1"]
        assert checks["branches"] == []  # the conflicting attempt's branch too, once the task completed
        assert any("could not be merged into main (conflict): conflicts with main in shared.txt" in n
                   for n in activity(agent_db))

    def test_a_reviewer_reviews_the_step_in_its_worktree(self, planner_claude: Any, agent_db: Any) -> None:
        agent_db.get_task_project.return_value = project(step_workspaces="worktree")
        claude = planner_claude(plan(step("s1", review=True), step("s2")),
                                decide("review", reason="check it"), decide("accept"), decide("accept"))
        seen: dict[str, Any] = {}

        async def scenario(h: Harness) -> None:
            init_repo(h.work_dir)
            await h.start()
            await h.finished()

        h = drive(claude, writes(seen), scenario)
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        review = seen["reviews"][0]
        assert review == seen["s1"][0]["context"]  # the step's worktree, and its start for `git diff`

    def test_a_step_a_change_stops_loses_its_worktree(self, planner_claude: Any, agent_db: Any) -> None:
        agent_db.get_task_project.return_value = project(step_workspaces="worktree")
        claude = planner_claude(
            plan(step("s1"), step("s2")),
            reply({"reply": "Stopping s2.", "action": "replan", "stop": ["s2"], "steps": []}),
            decide("accept"),  # s1
        )
        checks: dict[str, Any] = {}

        async def scenario(h: Harness) -> None:
            init_repo(h.work_dir)
            await h.start()
            await h.until(lambda: len(h.held) == 2)
            stopped = Path(h.held[1].payload["context"]["work_dir"])
            assert stopped.is_dir()
            await h.say("Drop s2, it is not needed.")
            checks["gone"] = not stopped.exists()
            checks["branches"] = looper_branches(h.work_dir)
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, lambda envelope: None, scenario)
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        assert checks["gone"] and checks["branches"] == ["looper/t1/s1-1"]
        assert [row["state"] for row in agent_db.store.workspaces.values()] == ["merged", "discarded"]

    def test_a_merge_cut_off_by_a_restart_is_finished_without_deciding_again(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        agent_db.get_task_project.return_value = project(step_workspaces="worktree")
        claude = planner_claude("<hang>")
        checks: dict[str, Any] = {}

        async def scenario(h: Harness) -> None:
            start = init_repo(h.work_dir)
            repo = await ws.inspect(str(h.work_dir))
            assert isinstance(repo, ws.Repo)
            workspace = ws.new_workspace(repo, runtime_root=h.work_dir.parent, workspace_id=1, task_id="t1",
                                         step_id="step-1", step_key="s1", base=start, checkpointed=False)
            await ws.ensure_worktree(workspace)
            (Path(workspace.path) / "one.txt").write_text("one\n")
            states: list[ws.StepWorkspace] = []

            async def save(state: ws.StepWorkspace) -> None:
                states.append(state)

            await ws.merge(workspace, step_key="s1", title="do s1", save=save)
            tip = git(h.work_dir, "rev-parse", "HEAD")
            # The server stopped after the branch moved, before the workspace was saved as merged.
            store = agent_db.store
            store.workspaces[1] = states[-2].row()
            store.tasks["t1"] = {"requirement": "build", "work_dir": str(h.work_dir)}
            store.plans["t1"] = {"plan_id": "plan-1", "status": "executing", "correlation_id": "corr-1",
                                 "origin_channel_id": "", "final_summary": "", "fail_reason": "",
                                 "start_branch": "refs/heads/main", "start_commit": start}
            store.steps["t1"] = {"step-1": {"id": "step-1", "index": 0, "key": "s1", "instruction": "do s1",
                                            "status": "deciding", "result": WORK_OK, "worker_id": "worker-1"}}
            h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
            await h.planner.start(h.bus)
            await h.bus.publish(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_RESUME,
                payload={"task_id": "t1"}, correlation_id="corr-1",
            ))
            await h.finished()
            checks["tip"], checks["now"] = tip, git(h.work_dir, "rev-parse", "HEAD")
            checks["path"] = Path(workspace.path)

        h = drive(claude, writes({}), scenario)
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        assert claude.turns() == []  # no decision is taken again
        assert checks["now"] == checks["tip"]  # and no second merge commit
        assert not checks["path"].exists()
        assert agent_db.store.workspaces[1]["state"] == "merged"
        assert agent_db.store.steps["t1"]["step-1"]["status"] == "verified"

    def test_without_the_setting_steps_share_the_task_directory_as_before(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        agent_db.get_task_project.return_value = project()
        claude = planner_claude(plan(step("s1"), step("s2")), decide("accept"), decide("accept"))
        checks: dict[str, Any] = {}

        async def scenario(h: Harness) -> None:
            checks["start"] = init_repo(h.work_dir)
            await h.start()
            await h.finished()
            checks["now"] = git(h.work_dir, "rev-parse", "HEAD")
            checks["root"] = h.work_dir.parent

        h = drive(claude, writes({}), scenario)
        assert [w.payload["context"] for w in h.work_items] == [{"work_dir": str(h.work_dir)}] * 2
        assert h.dispatched_at_turn == [1, 1]
        assert checks["now"] == checks["start"] and not (checks["root"] / "worktrees").exists()
        assert agent_db.store.workspaces == {} and agent_db.store.plans["t1"]["start_commit"] == ""

    def test_outside_git_steps_share_the_task_directory_even_with_the_setting(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        agent_db.get_task_project.return_value = project(step_workspaces="worktree")
        claude = planner_claude(plan(step("s1"), step("s2")), decide("accept"), decide("accept"))
        h = drive(claude, writes({}))
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        assert [w.payload["context"] for w in h.work_items] == [{"work_dir": str(h.work_dir)}] * 2
        assert h.dispatched_at_turn == [1, 1]
        assert agent_db.store.workspaces == {}

    def test_a_detached_head_runs_the_steps_one_at_a_time(self, planner_claude: Any, agent_db: Any) -> None:
        agent_db.get_task_project.return_value = project(step_workspaces="worktree")
        claude = planner_claude(plan(step("s1"), step("s2")), decide("accept"), decide("accept"))

        async def scenario(h: Harness) -> None:
            init_repo(h.work_dir)
            git(h.work_dir, "switch", "-q", "--detach")
            await h.start()
            await h.finished()

        h = drive(claude, writes({}), scenario)
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        assert h.dispatched_at_turn == [1, 2]
        assert [w.payload["context"] for w in h.work_items] == [{"work_dir": str(h.work_dir)}] * 2
        notes = activity(agent_db)
        assert notes.count("Steps run one at a time: the task directory's HEAD is detached") == 1
