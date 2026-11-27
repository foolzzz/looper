"""Ref snapshots and reports of a direct session's turn: what changed, how to restore it, and what is never reported."""
from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path

import pytest

from looper.agents import git_safe, task_manager_refs as refs
from looper.agents.task_manager_refs import Change

TASK = "task-one"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=Tester", "-c", "user.email=tester@example.invalid",
                           "-c", "commit.gpgsign=false", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n")
    git(repo, "add", "--", name)
    git(repo, "commit", "-qm", f"add {name}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, "a.txt")
    git(path, "branch", "other")
    git(path, "tag", "v1")
    return path


async def inactive(_: str) -> bool:
    return False


def run(repo: Path, change, *, active=inactive, branch="main"):
    async def go():
        before = await refs.snapshot(repo)
        change()
        after = await refs.snapshot(repo)
        return await refs.compare(repo, before, after, task_branch=branch, task_id=TASK, active_task=active)
    return asyncio.run(go())


def by_ref(report) -> dict[str, refs.RefChange]:
    return {change.ref: change for change in report.changes}


def test_a_fast_forward_of_the_task_branch_is_not_reported_and_counts_the_commits(repo):
    def two_commits():
        commit(repo, "b.txt")
        commit(repo, "c.txt")
    report = run(repo, two_commits)
    assert report.changes == () and report.info == () and not report.needs_you
    assert report.own_commits == 2 and report.head_before == report.head_after == "refs/heads/main"
    assert not run(repo, lambda: None).worth_saying


def test_other_branches_and_tags_are_reported_with_a_restore_command_that_undoes_them(repo):
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "doomed")
    git(repo, "tag", "v3")
    before = asyncio.run(refs.snapshot(repo))
    elsewhere = git(repo, "commit-tree", "-m", "elsewhere", "HEAD^{tree}")
    git(repo, "update-ref", "refs/heads/other", git(repo, "commit-tree", "-p", head, "-m", "x", "HEAD^{tree}"))
    git(repo, "branch", "fresh")
    git(repo, "branch", "-D", "doomed")
    git(repo, "tag", "v2")
    git(repo, "tag", "-d", "v1")
    git(repo, "tag", "-f", "v3", elsewhere)
    after = asyncio.run(refs.snapshot(repo))
    report = asyncio.run(refs.compare(repo, before, after, task_branch="main", task_id=TASK, active_task=inactive))
    found = by_ref(report)
    assert {ref: change.change for ref, change in found.items()} == {
        "refs/heads/other": Change.MOVED, "refs/heads/fresh": Change.CREATED, "refs/heads/doomed": Change.DELETED,
        "refs/tags/v2": Change.CREATED, "refs/tags/v1": Change.DELETED, "refs/tags/v3": Change.MOVED}
    assert report.needs_you and report.own_commits == 0
    for change in found.values():  # every restore command, run through git_safe, returns the ref to its old state
        args = shlex.split(change.restore)
        assert args[:3] == ["git", "update-ref", "--no-deref"]
        git_safe.run(repo, *args[1:], check=True, capture_output=True)
    assert asyncio.run(refs.snapshot(repo)).refs == before.refs


def test_restore_commands_quote_the_ref(repo):
    odd = "refs/heads/a$b;c"
    sha = git(repo, "rev-parse", "HEAD")
    report = run(repo, lambda: git(repo, "update-ref", odd, sha))
    assert report.changes[0].restore == f"git update-ref --no-deref -d {shlex.quote(odd)} {sha}"
    assert shlex.split(report.changes[0].restore)[4] == odd


def test_resetting_the_task_branch_is_non_fast_forward_and_not_needs_you_by_itself(repo):
    commit(repo, "b.txt")
    report = run(repo, lambda: git(repo, "reset", "--hard", "HEAD~1"))
    change, = report.changes
    assert change.ref == "refs/heads/main" and change.change is Change.NON_FAST_FORWARD
    assert change.restore.startswith("git update-ref --no-deref refs/heads/main ") and not report.needs_you
    assert report.own_commits == 0


def test_deleting_the_task_branch_is_reported_and_not_needs_you(repo):
    git(repo, "checkout", "-q", "other")
    report = run(repo, lambda: git(repo, "branch", "-D", "main"))
    change, = report.changes
    assert change.change is Change.DELETED and not report.needs_you


@pytest.mark.parametrize("active, target", [(True, "info"), (False, "needs_you")])
def test_another_looper_tasks_branch_is_information_only_while_that_task_is_active(repo, active, target):
    git(repo, "branch", "looper/task-two/task")
    asked = []

    async def is_active(task_id: str) -> bool:
        asked.append(task_id)
        return active

    report = run(repo, lambda: git(repo, "update-ref", "refs/heads/looper/task-two/task",
                                   git(repo, "commit-tree", "-m", "x", "HEAD^{tree}")), active=is_active)
    assert asked == ["task-two"]
    if target == "info":
        assert [c.ref for c in report.info] == ["refs/heads/looper/task-two/task"]
        assert report.changes == () and not report.needs_you
    else:
        assert [c.ref for c in report.changes] == ["refs/heads/looper/task-two/task"] and report.needs_you and not report.info


def test_the_tasks_own_looper_branches_and_looper_tags_still_need_you(repo):
    git(repo, "branch", f"looper/{TASK}/other")

    async def always(_: str) -> bool:
        return True
    report = run(repo, lambda: (git(repo, "branch", "-D", f"looper/{TASK}/other"), git(repo, "tag", "looper/task-two/x")),
                 active=always)
    assert report.needs_you and report.info == () and len(report.changes) == 2


def test_remote_tracking_refs_are_never_read(repo, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    report = run(repo, lambda: (commit(repo, "n.txt"), git(repo, "push", "-q", "origin", "main"), git(repo, "fetch", "-q", "origin")))
    assert report.changes == () and report.info == () and not report.needs_you and report.own_commits == 1
    assert all(ref.startswith(("refs/heads/", "refs/tags/")) for ref in asyncio.run(refs.snapshot(repo)).refs)


def test_packing_refs_removing_reflogs_and_gc_are_not_changes(repo):
    def housekeeping():
        git(repo, "pack-refs", "--all")
        subprocess.run(["rm", "-rf", str(repo / ".git" / "logs")], check=True)
        git(repo, "gc", "-q", "--prune=now")
        git(repo, "reflog", "expire", "--expire=now", "--all")
    report = run(repo, housekeeping)
    assert not report.worth_saying


def test_a_head_switch_is_reported_without_a_restore(repo):
    report = run(repo, lambda: git(repo, "checkout", "-q", "other"))
    assert (report.head_before, report.head_after) == ("refs/heads/main", "refs/heads/other")
    assert report.head_changed and report.worth_saying and report.changes == ()
    detached = run(repo, lambda: git(repo, "checkout", "-q", "--detach"))
    assert detached.head_after is None and detached.head_changed


def test_a_directory_that_is_not_a_repository_fails_loudly(tmp_path):
    from looper.agents.tiers.task_manager import Refused
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises((Refused, RuntimeError)):
        asyncio.run(refs.snapshot(plain))


# --- review findings of the first version -----------------------------------------------------------------------


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_a_ref_name_with_a_line_separator_cannot_overwrite_another_ref(repo, separator):
    from looper.agents.tiers.task_manager import Refused
    old = git(repo, "rev-parse", "HEAD")
    commit(repo, "b.txt")
    git(repo, "update-ref", f"refs/heads/z{separator}refs/heads/main", old)  # a line split would restore main to old
    with pytest.raises(Refused, match="line-break"):
        asyncio.run(refs.snapshot(repo))


def test_a_task_branch_reset_is_not_hidden_by_a_graft(repo):
    commit(repo, "b.txt")
    first = git(repo, "rev-parse", "HEAD~1")
    tip = git(repo, "rev-parse", "HEAD")

    def reset_with_graft():
        git(repo, "reset", "-q", "--hard", first)
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "grafts").write_text(f"{first} {tip}\n")  # makes tip look like first's parent
    report = run(repo, reset_with_graft)
    change, = report.changes
    assert change.change is Change.NON_FAST_FORWARD and change.old == tip and change.new == first
    assert report.own_commits == 0


def test_a_merge_of_an_older_branch_into_the_task_branch_is_a_fast_forward_with_the_right_count(repo):
    git(repo, "checkout", "-q", "-b", "topic")
    commit(repo, "t.txt")
    git(repo, "checkout", "-q", "main")
    commit(repo, "m.txt")

    def merge():
        git(repo, "merge", "-q", "--no-ff", "-m", "merge topic", "topic")
        commit(repo, "after.txt")
    report = run(repo, merge)
    assert report.changes == () and report.own_commits == 3  # the merge, topic's commit and the one after


def test_a_symbolic_ref_is_restored_without_touching_its_target(repo):
    before = asyncio.run(refs.snapshot(repo))
    report = run(repo, lambda: git(repo, "symbolic-ref", "refs/heads/link", "refs/heads/main"))
    change, = report.changes
    assert change.ref == "refs/heads/link" and change.change is Change.CREATED
    assert change.restore == "git symbolic-ref --delete refs/heads/link"
    git_safe.run(repo, *shlex.split(change.restore)[1:], check=True, capture_output=True)
    assert git(repo, "rev-parse", "--verify", "refs/heads/main")  # the target is still there
    assert asyncio.run(refs.snapshot(repo)).refs == before.refs


def test_retargeting_a_symbolic_ref_to_the_same_commit_is_a_change_and_restores(repo):
    git(repo, "symbolic-ref", "refs/heads/link", "refs/heads/other")  # other and main are the same commit
    report = run(repo, lambda: git(repo, "symbolic-ref", "refs/heads/link", "refs/heads/main"))
    change, = report.changes
    assert change.change is Change.MOVED and change.old == change.new
    assert change.restore == "git symbolic-ref refs/heads/link refs/heads/other"
    git_safe.run(repo, *shlex.split(change.restore)[1:], check=True, capture_output=True)
    assert git(repo, "symbolic-ref", "refs/heads/link") == "refs/heads/other"


def test_deleting_a_symbolic_ref_restores_the_link_not_a_copy(repo):
    git(repo, "symbolic-ref", "refs/heads/link", "refs/heads/other")
    report = run(repo, lambda: git(repo, "symbolic-ref", "--delete", "refs/heads/link"))
    change, = report.changes
    assert change.change is Change.DELETED and change.restore == "git symbolic-ref refs/heads/link refs/heads/other"


def test_bidirectional_controls_are_escaped_in_the_name_and_the_restore_command_still_works(repo):
    odd = "refs/heads/a\u202eb"
    sha = git(repo, "rev-parse", "HEAD")
    report = run(repo, lambda: git(repo, "update-ref", odd, sha))
    change, = report.changes
    assert change.ref == "refs/heads/a\\u202eb" and "\u202e" not in change.restore and "\u202e" not in change.ref
    subprocess.run(["bash", "-c", change.restore.replace("git ", f"git -C {shlex.quote(str(repo))} ", 1)], check=True,
                   capture_output=True)
    assert odd not in asyncio.run(refs.snapshot(repo)).refs


@pytest.mark.parametrize("name, ok", [("main", True), ("feature/x", True), ("--detach", False), ("-x", False),
                                       ("a..b", False), ("a b", False)])
def test_only_names_git_accepts_and_that_are_not_options_can_be_the_task_branch(repo, name, ok):
    assert (asyncio.run(refs.branch_problem(repo, name)) is None) is ok
