"""A task's changes reach its Planner: supplements and requirement edits, each in a turn, never lost.

The changes are stored first (``db.task_changes``, here the in-memory store
of ``agent_db``); the Planner takes every change waiting into one CHAT turn,
after the turn it was in, and records them delivered only once that turn's
reply is done. A failed turn or a restart leaves them for the next turn.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

from looper.agents import step_workspaces as runner
from looper.agents import task_changes
from looper.agents import task_workspaces
from looper.db import tasks, task_worktrees
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, planner_address
from looper.agents.tiers.planner import PlannerAgent
from looper.db.task_changes import RequirementEdit
from tests.test_planner_agent import WORK_OK, Harness, activity, chat, decide, drive, hold_first, plan, step

SUPPLEMENT = "导出改用 CSV，不要 JSON。"


def _texts(claude: Any) -> list[str]:
    return [turn["text"] for turn in claude.turns()]


def _pending(agent_db: Any) -> list[str]:
    return [entry["summary"] for entry in agent_db.store.journal.get("t1", [])
            if entry["kind"] == "supplement" and not entry["delivered"]]


async def _change(h: Harness, agent_db: Any, *, text: str = SUPPLEMENT, nudge: bool = True) -> None:
    """Store a supplement the way the platform does, then (``nudge``) wake the Planner."""
    await agent_db.store.add_supplement("t1", text, via="manager:web")
    if nudge:
        await h.bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=planner_address("t1"), message_type=MessageType.TASK_CHANGE,
            payload={"task_id": "t1"}))


@pytest.mark.usefixtures("agent_db")
class TestDelivery:
    def test_a_change_waiting_when_a_turn_ends_is_given_in_the_next_turn(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        """Stored while s1 ran, with no wake-up at all: the turn after s1's decision gives it, and s2, started in
        that event, already lists it."""
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), decide("accept"), chat("好的，改用 CSV。"),
                                decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db, nudge=False)
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, hold_first(), scenario)
        texts = _texts(claude)
        assert texts[0].startswith("[PLAN]") and texts[1].startswith("[DECISION] Step s1")
        assert texts[2].startswith("[CHAT] The task changed.")
        assert SUPPLEMENT in texts[2] and "Allowed actions: none, add_steps, replan" in texts[2]
        assert _pending(agent_db) == []
        (s2,) = [w.payload for w in h.work_items if w.payload["step_key"] == "s2"]
        assert s2["supplements"] == [SUPPLEMENT]

    def test_a_change_during_a_turn_waits_for_it_and_a_failed_turn_leaves_it_for_the_next(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), "<error>", decide("accept"),
                                chat("好的，改用 CSV。"), decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db)
            await h.until(lambda: len(claude.turns()) == 2)  # the change turn, which fails
            await h.until(lambda: any("could not take a change" in line for line in activity(agent_db)))
            assert _pending(agent_db) == [SUPPLEMENT]
            await h.release(WORK_OK)
            await h.finished()

        drive(claude, hold_first(), scenario)
        texts = _texts(claude)
        assert texts[1].startswith("[CHAT] The task changed.") and texts[2].startswith("[DECISION] Step s1")
        assert texts[3].startswith("[CHAT] The task changed.") and SUPPLEMENT in texts[3]
        assert _pending(agent_db) == []

    def test_a_restarted_planner_gives_the_change_it_had_not(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(plan(step("s1")), "<error>", chat("好的，改用 CSV。"), decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db)
            await h.until(lambda: any("could not take a change" in line for line in activity(agent_db)))
            await h.planner.stop()  # the server stops before the change was delivered
            h.held.clear()
            agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
            h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
            await h.planner.start(h.bus)
            await h.bus.publish(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_RESUME,
                payload={"task_id": "t1"}, correlation_id="corr-1"))
            await h.until(lambda: len(claude.turns()) == 3 and h.held)
            await h.release(WORK_OK)
            await h.finished()

        drive(claude, hold_first(None), scenario)  # type: ignore[arg-type]
        texts = _texts(claude)
        assert texts[2].startswith("[CHAT] The task changed.") and SUPPLEMENT in texts[2]
        assert _pending(agent_db) == []

    def test_changes_waiting_together_go_in_one_turn_and_the_new_requirement_reaches_later_steps(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(plan(step("s1"), step("s2", "s1")), chat("明白。"), decide("accept"),
                                decide("accept"))

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            agent_db.store.requirements["t1"] = RequirementEdit(2, "build the calc package, with a CLI", 1,
                                                                "build the calc package")
            await agent_db.store.add_supplement("t1", "也要支持减法。", via="chat")
            await _change(h, agent_db)
            await h.until(lambda: len(claude.turns()) == 2)
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, hold_first(), scenario)
        change = _texts(claude)[1]
        assert change.count("[CHAT] The task changed.") == 1
        assert "也要支持减法。" in change and SUPPLEMENT in change
        assert "The requirement was edited (version 1 -> 2)" in change and "+build the calc package, with a CLI" in change
        # The new text is the system prompt's requirement from that turn on, and the later Worker's.
        assert "build the calc package, with a CLI" in claude.prompts()[-1]
        (s2,) = [w.payload for w in h.work_items if w.payload["step_key"] == "s2"]
        assert s2["requirement"] == "build the calc package, with a CLI"
        assert s2["supplements"] == ["也要支持减法。", SUPPLEMENT]
        assert _pending(agent_db) == [] and "t1" not in agent_db.store.requirements

    def test_the_plan_turn_gets_the_changes_made_before_it(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(plan(step("s1")), decide("accept"))

        async def scenario(h: Harness) -> None:
            await agent_db.store.add_supplement("t1", SUPPLEMENT, via="manager:web")
            agent_db.store.requirements["t1"] = RequirementEdit(2, "build calc, CSV export", 1, "build calc")
            await h.start()
            await h.finished()

        h = drive(claude, always_ok(), scenario)
        (planned, decided) = _texts(claude)
        assert planned.startswith("[PLAN]") and SUPPLEMENT in planned
        assert "build calc, CSV export" in claude.prompts()[0]
        assert h.work_items[0].payload["requirement"] == "build calc, CSV export"
        assert _pending(agent_db) == [] and "t1" not in agent_db.store.requirements
        assert not any(text.startswith("[CHAT]") for text in _texts(claude))


@pytest.mark.usefixtures("agent_db")
class TestChatReplan:
    def test_replan_stops_the_running_steps_it_names_and_replaces_the_ones_not_started(
        self, planner_claude: Any, agent_db: Any,
    ) -> None:
        claude = planner_claude(
            plan(step("s1"), step("s2", "s1")),
            chat("改用 CSV：停掉 s1，改成 s3。", "replan", steps=[step("s3")], stop=["s1"]),
            decide("accept"),
        )

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db)
            await h.finished()

        h = drive(claude, hold_first(), scenario)
        assert h.final().payload["status"] == "completed"  # type: ignore[union-attr]
        (killed,) = h.killed
        assert killed.target == h.work_items[0].target  # s1's Worker
        assert [w.payload["step_key"] for w in h.work_items] == ["s1", "s3"]
        saved = {s["key"]: s["status"] for s in agent_db.store.steps["t1"].values()}
        assert saved == {"s1": "skipped", "s2": "skipped", "s3": "verified"}
        assert any(e["kind"] == "decision" and "replan at a change to the task" in e["summary"]
                   for e in agent_db.store.journal["t1"])

    def test_replan_keeps_running_steps_it_does_not_name(self, planner_claude: Any, agent_db: Any) -> None:
        claude = planner_claude(
            plan(step("s1"), step("s2", "s1")),
            chat("s1 保留，s2 改成 s3。", "replan", steps=[step("s3", "s1")]),
            decide("accept"), decide("accept"),
        )

        async def scenario(h: Harness) -> None:
            await h.start()
            await h.until(lambda: h.held)
            await _change(h, agent_db)
            await h.until(lambda: len(claude.turns()) == 2)
            await h.release(WORK_OK)
            await h.finished()

        h = drive(claude, hold_first(), scenario)
        assert h.killed == []
        assert [w.payload["step_key"] for w in h.work_items] == ["s1", "s3"]
        saved = {s["key"]: s["status"] for s in agent_db.store.steps["t1"].values()}
        assert saved == {"s1": "verified", "s2": "skipped", "s3": "verified"}


def always_ok() -> Any:
    return lambda envelope: WORK_OK


def git(cwd, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
                          cwd=cwd, check=True, capture_output=True).stdout.decode().strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "task")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test")
    for name in ("modified", "deleted", "rename", "committed", "sub/inside", "sibling/outside"):
        path = repo / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(name + "\n" * 20)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Base")
    return repo


@pytest.fixture
def workspace(repository, monkeypatch):
    repo = repository
    row = {"task_id": "t", "repo_dir": str(repo), "subdir": "", "path": str(repo), "branch": "task",
           "base_commit": git(repo, "rev-parse", "HEAD"), "state": "active"}
    task = {"id": "t", "workspace_dir": str(repo), "work_dir": str(repo), "workspace_mode": "worktree"}

    async def get_task(task_id):
        return task if task_id == "t" else None

    async def get_tree(task_id):
        return row if task["workspace_mode"] == "worktree" else None

    monkeypatch.setattr(tasks, "get_task", get_task)
    monkeypatch.setattr(task_worktrees, "get", get_tree)
    # No preparation/reopening belongs on a read path.
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("unexpected workspace write")

    monkeypatch.setattr(task_workspaces, "prepare", forbidden)
    yield repo, task, row
    assert calls == []


def read(limit=100):
    return asyncio.run(task_changes.read("t", limit=limit))


def test_net_changes_names_rename_and_readonly(workspace, tmp_path, monkeypatch):
    repo, _, _ = workspace
    (repo / "committed").write_text("new commit")
    git(repo, "commit", "-am", "Task commit")
    (repo / "modified").write_text("staged")
    git(repo, "add", "modified")
    (repo / "modified").write_text("unstaged too")
    (repo / "deleted").unlink()
    git(repo, "mv", "rename", "renamed file\nnext")
    for name in ("with space", "with\nnewline", "-leading"):
        (repo / name).write_text("new")
    (repo / ".gitignore").write_text("ignored\n")
    (repo / "ignored").write_text("ignored")
    git(repo, "add", ".gitignore")
    marker = tmp_path / "sentinel"
    script = tmp_path / "sentinel.sh"
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    script.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(tmp_path))
    for name in ("post-index-change", "reference-transaction"):
        (tmp_path / name).symlink_to(script)
    git(repo, "config", "diff.external", str(script))
    git(repo, "config", "diff.sentinel.textconv", str(script))
    (repo / ".gitattributes").write_text("* diff=sentinel\n")
    git(repo, "add", ".gitattributes")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    for name, value in {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                        "GIT_INDEX_FILE": str(other / "index"), "GIT_COMMON_DIR": str(other / ".git")}.items():
        monkeypatch.setenv(name, value)
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    result = read()
    assert result["kind"] == "ok" and result["scope"] == "task_branch" and result["reason"] is None
    files = {f["path"]: f for f in result["files"]}
    assert files["committed"]["status"] == files["modified"]["status"] == "modified"
    assert files["deleted"]["status"] == "deleted"
    assert files["renamed file\nnext"] == {"path": "renamed file\nnext", "status": "renamed", "old_path": "rename"}
    assert all(files[n]["status"] == "untracked" for n in ("with space", "with\nnewline", "-leading"))
    assert "ignored" not in files and "rename" not in files
    assert len(files) == len(result["files"]) and not result["truncated"]
    assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert not marker.exists() and not (other / "index").exists()


@pytest.mark.parametrize("count,limit,truncated", [(0, 100, False), (1, 1, False), (2, 1, True),
                                                    (101, 100, True), (200, 200, False), (201, 200, True)])
@pytest.mark.parametrize("mode", ["worktree", "in_place"])
def test_entry_bound(workspace, count, limit, truncated, mode):
    repo, task, _ = workspace
    task["workspace_mode"] = mode
    for i in range(count):
        (repo / f"new-{i:03}").write_text("new")
    result = read(limit)
    assert len(result["files"]) == min(count, limit)
    assert result["truncated"] is truncated and "total" not in result


def test_active_branch_identity_and_detached(workspace):
    repo, _, row = workspace
    git(repo, "checkout", "-b", "other")
    assert read()["reason"] == "branch_changed"
    git(repo, "checkout", "--detach", "task")
    assert read()["kind"] == "ok"
    git(repo, "checkout", "other")
    (repo / "committed").write_text("different tip")
    git(repo, "commit", "-am", "Other")
    git(repo, "checkout", "--detach")
    assert read()["reason"] == "branch_changed"
    git(repo, "branch", "-D", row["branch"])
    assert read()["reason"] == "branch_missing"


def test_linked_worktree_missing_does_not_fall_back(workspace, tmp_path):
    repo, _, row = workspace
    checkout = tmp_path / "task-checkout"
    git(repo, "checkout", "-b", "operator")
    git(repo, "worktree", "add", str(checkout), "task")
    row["path"] = str(checkout)
    (checkout / "committed").write_text("task commit")
    git(checkout, "commit", "-am", "Task")
    (repo / "modified").write_text("operator change")
    assert read()["files"] == [{"path": "committed", "status": "modified", "old_path": None}]
    git(repo, "worktree", "remove", str(checkout))
    assert read()["reason"] == "workspace_missing"
    row["state"] = "removed"
    assert read()["files"] == [{"path": "committed", "status": "modified", "old_path": None}]


def test_conflict_is_one_file(workspace):
    repo, task, row = workspace
    git(repo, "checkout", "-b", "other")
    (repo / "modified").write_text("other branch")
    git(repo, "commit", "-am", "Other")
    git(repo, "checkout", "task")
    (repo / "modified").write_text("task branch")
    git(repo, "commit", "-am", "Task")
    with pytest.raises(subprocess.CalledProcessError):
        git(repo, "merge", "other")
    assert read()["files"] == [{"path": "modified", "status": "conflicted", "old_path": None}]
    task["workspace_mode"] = "in_place"
    assert read()["files"] == [{"path": "modified", "status": "conflicted", "old_path": None}]


def test_shared_runner_timeout_is_typed(workspace, monkeypatch):
    monkeypatch.setattr(runner, "GIT_TIMEOUT_SEC", 0)
    assert read() == {"task_id": "t", "kind": "unavailable", "scope": None, "files": [],
                      "truncated": False, "reason": "git_error"}


def test_bad_base_is_git_error(workspace):
    _, _, row = workspace
    row["base_commit"] = "missing-base"
    assert read()["reason"] == "git_error"


def test_removed_ignores_checkout_and_in_place_ignores_commits(workspace):
    repo, task, row = workspace
    (repo / "committed").write_text("task commit")
    git(repo, "commit", "-am", "Task")
    row.update(state="removed", path=str(repo / "gone"))
    git(repo, "checkout", "-b", "other", row["base_commit"])
    (repo / "modified").write_text("other checkout")
    (repo / "untracked").write_text("other checkout")
    assert read()["files"] == [{"path": "committed", "status": "modified", "old_path": None}]
    task["workspace_mode"] = "in_place"
    git(repo, "checkout", "task")
    result = read()
    assert result["scope"] == "working_tree"
    assert {f["path"] for f in result["files"]} == {"modified", "untracked"}
    task["workspace_mode"] = "worktree"
    git(repo, "branch", "-D", "other")
    git(repo, "checkout", "-b", "other", row["base_commit"])
    git(repo, "branch", "-D", "task")
    assert read()["reason"] == "branch_missing"


@pytest.mark.parametrize("mode", ["active", "removed", "in_place"])
@pytest.mark.parametrize("direction", ["inside", "out", "in"])
def test_subdirectory_renames(workspace, mode, direction):
    repo, task, row = workspace
    row["subdir"] = "sub"
    task["workspace_dir"] = str(repo / "sub")
    src, dst = {"inside": ("sub/inside", "sub/new"), "out": ("sub/inside", "sibling/new"),
                "in": ("sibling/outside", "sub/new")}[direction]
    git(repo, "mv", src, dst)
    (repo / "sibling/private").write_text("not in task")
    if mode == "removed":
        git(repo, "commit", "-m", "Move")
        row.update(state="removed", path=str(repo / "gone"))
    elif mode == "in_place":
        task["workspace_mode"] = "in_place"
    result = read()
    expected = {"inside": {"path": "new", "status": "renamed", "old_path": "inside"},
                "out": {"path": "inside", "status": "deleted", "old_path": None},
                "in": {"path": "new", "status": "added", "old_path": None}}[direction]
    assert result["files"] == [expected] and result["reason"] is None


@pytest.mark.parametrize("mode", ["worktree", "in_place"])
def test_literal_scope_and_work_dir_fallback(workspace, mode):
    repo, task, row = workspace
    git(repo, "mv", "sub", "sub[task]")
    git(repo, "commit", "-m", "Workspace with literal brackets")
    row.update(subdir="sub[task]", base_commit=git(repo, "rev-parse", "HEAD"))
    task.update(workspace_mode=mode, workspace_dir="", work_dir=str(repo / "sub[task]"))
    (repo / "sub[task]/inside").write_text("changed")
    (repo / "sibling/outside").write_text("sibling")
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert read()["files"] == [{"path": "inside", "status": "modified", "old_path": None}]
    assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


@pytest.mark.parametrize("reason", ["not_git", "workspace_missing", "git_error", "timeout"])
def test_unavailable_never_success_empty(workspace, monkeypatch, tmp_path, reason):
    repo, task, row = workspace
    if reason == "not_git":
        row["path"] = str(tmp_path)
    elif reason == "workspace_missing":
        row["path"] = str(tmp_path / "missing")
    else:
        original = runner._git

        async def fail(cwd, *args, **kwargs):
            if args[0] == "diff":
                raise runner.GitError("private stderr" if reason == "git_error" else "timed out")
            return await original(cwd, *args, **kwargs)

        monkeypatch.setattr(runner, "_git", fail)
    result = read()
    assert result == {"task_id": "t", "kind": "unavailable", "scope": None, "files": [],
                      "truncated": False, "reason": "git_error" if reason == "timeout" else reason}


def test_nul_parser_copy_type_conflict_dedup_and_boundary():
    raw = b"R100\0sub/old\0sub/new\0C100\0sibling/private\0sub/copy\0T\0sub/type\0U\0sub/conflict\0"
    assert list(task_changes._entries(raw, "sub")) == [
        {"path": "new", "status": "renamed", "old_path": "old"},
        {"path": "copy", "status": "added", "old_path": None},
        {"path": "type", "status": "type_changed", "old_path": None},
        {"path": "conflict", "status": "conflicted", "old_path": None}]
    porcelain = b"UU sub/conflict\0?? sub/new\nfile\0R  sub/new\0sub/old\0"
    assert [f["status"] for f in task_changes._entries(porcelain, "sub", porcelain=True)] == [
        "conflicted", "untracked", "renamed"]
    assert list(task_changes._entries(b"C100\0sub/old\0sub/copy\0X\0sub/unknown\0", "sub")) == [
        {"path": "copy", "status": "copied", "old_path": "old"},
        {"path": "unknown", "status": "unknown", "old_path": None}]
    assert list(task_changes._entries(b"R100\0sub/old\0sibling/new\0", "sub")) == [
        {"path": "old", "status": "deleted", "old_path": None}]
    assert list(task_changes._entries(b"A\0../private\0A\0/absolute\0A\0sibling/private\0", "sub")) == []


@pytest.mark.parametrize("data", [b"M\0", b"R100\0old\0", b"M\0unterminated"])
def test_incomplete_git_output_is_an_error(data):
    with pytest.raises(runner.GitError):
        list(task_changes._entries(data, ""))
