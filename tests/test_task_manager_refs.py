"""Ref snapshots and reports of a direct session's turn: what changed, how to restore it, and what is never reported."""
from __future__ import annotations

import asyncio
import os
import random
import shlex
import shutil
import string
import subprocess
import time
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


# --- formal review of the second version ------------------------------------------------------------------------


def raw_commit(repo: Path, tree: str, header_lines: list[str], message: str = "rewrite") -> str:
    ident = "A <a@example.invalid> 1700000000 +0000"
    body = "\n".join([f"tree {tree}", f"author {ident}", f"committer {ident}", *header_lines]) + f"\n\n{message}\n"
    return subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "--literally", "-w", "--stdin"],
                          input=body, check=True, capture_output=True, text=True).stdout.strip()


def test_a_parent_line_after_the_other_header_lines_does_not_make_a_rewrite_a_fast_forward(repo):
    commit(repo, "b.txt")
    tip = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD~1^{tree}")
    crafted = raw_commit(repo, tree, [f"parent {tip}"])  # git itself sees a root commit here
    assert git(repo, "rev-list", "--count", crafted) == "1"
    report = run(repo, lambda: git(repo, "reset", "-q", "--hard", crafted))
    change, = report.changes
    assert change.change is Change.NON_FAST_FORWARD and report.own_commits == 0


def test_a_real_parent_line_in_the_right_place_is_still_a_fast_forward(repo):
    tip = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    ident = "A <a@example.invalid> 1700000000 +0000"
    body = f"tree {tree}\nparent {tip}\nauthor {ident}\ncommitter {ident}\n\nchild\n"
    child = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"], input=body,
                           check=True, capture_output=True, text=True).stdout.strip()
    report = run(repo, lambda: git(repo, "reset", "-q", "--hard", child))
    assert report.changes == () and report.own_commits == 1


def test_a_replace_ref_cannot_make_a_reset_look_like_a_fast_forward(repo):
    commit(repo, "b.txt")
    tip = git(repo, "rev-parse", "HEAD")
    first = git(repo, "rev-parse", "HEAD~1")
    fake = git(repo, "commit-tree", "-p", tip, "-m", "fake", f"{first}^{{tree}}")
    git(repo, "replace", first, fake)  # a read that honours replace refs sees `first` as a child of the old tip
    report = run(repo, lambda: git(repo, "reset", "-q", "--hard", first))
    change, = report.changes
    assert change.change is Change.NON_FAST_FORWARD


def test_a_commit_object_over_the_size_limit_leaves_the_decision_open_and_says_so(repo, monkeypatch):
    monkeypatch.setattr(refs, "OBJECT_LIMIT", 10)
    old = git(repo, "rev-parse", "HEAD")
    report = run(repo, lambda: commit(repo, "b.txt"))
    change, = report.changes
    assert change.change is Change.FAST_FORWARD_UNVERIFIED and report.own_commits == 0
    assert change.restore == f"git update-ref --no-deref refs/heads/main {old} {change.new}"  # still restorable
    assert not report.needs_you  # same as a rewrite of the task branch


def test_a_walk_that_hits_its_commit_limit_is_unverified_not_a_rewrite(repo, monkeypatch):
    monkeypatch.setattr(refs, "WALK_LIMIT", 1)
    commit(repo, "b.txt")
    report = run(repo, lambda: (commit(repo, "c.txt"), commit(repo, "d.txt")))
    change, = report.changes
    assert change.change is Change.FAST_FORWARD_UNVERIFIED


def test_a_decided_reset_is_still_a_rewrite_and_only_an_undecided_walk_is_unverified(repo):
    commit(repo, "b.txt")
    report = run(repo, lambda: git(repo, "reset", "-q", "--hard", "HEAD~1"))
    change, = report.changes
    assert change.change is Change.NON_FAST_FORWARD


def test_an_ordinary_merge_in_a_large_history_is_still_a_fast_forward(repo, monkeypatch):
    monkeypatch.setattr(refs, "WALK_LIMIT", 5000)  # less than the history: only a breadth-first walk finds `old`
    stream = []
    for number in range(12000):
        stream.append(f"commit refs/heads/main\nmark :{number + 1}\n"
                      f"committer T <t@example.invalid> {1700000000 + number} +0000\n"
                      f"data 1\nx\n" + (f"from :{number}\n" if number else "") + "\n")
    subprocess.run(["git", "-C", str(repo), "fast-import", "--quiet", "--force"], input="".join(stream),
                   check=True, capture_output=True, text=True, timeout=120)
    empty = git(repo, "rev-parse", "main^{tree}")

    def merge():
        main = git(repo, "rev-parse", "main")
        side = git(repo, "commit-tree", "-p", git(repo, "rev-parse", "main~2"), "-m", "feature", empty)
        merged = git(repo, "commit-tree", "-p", main, "-p", side, "-m", "merge", empty)
        git(repo, "update-ref", "refs/heads/main", merged)
    started = time.monotonic()
    report = run(repo, merge)
    assert report.changes == () and report.own_commits == 2 and time.monotonic() - started < 30


def test_a_link_to_the_same_ref_does_not_report_its_targets_moves(repo):
    git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    report = run(repo, lambda: commit(repo, "b.txt"))
    assert report.changes == () and report.own_commits == 1


def test_zero_width_characters_are_escaped_and_the_restore_command_still_works(repo):
    odd = "refs/heads/ma\u200bin2"
    sha = git(repo, "rev-parse", "HEAD")
    report = run(repo, lambda: git(repo, "update-ref", odd, sha))
    change, = report.changes
    assert change.ref == "refs/heads/ma\\u200bin2" and "\u200b" not in change.restore
    subprocess.run(["bash", "-c", change.restore.replace("git ", f"git -C {shlex.quote(str(repo))} ", 1)], check=True,
                   capture_output=True)
    assert odd not in asyncio.run(refs.snapshot(repo)).refs


@pytest.mark.parametrize("target", ["refs/hidden/x", "refs/remotes/origin/main"])
def test_a_link_into_a_namespace_that_is_not_compared_still_reports_when_its_target_moves(repo, target):
    commit(repo, "b.txt")
    tip, first = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD~1")
    git(repo, "update-ref", target, tip)
    git(repo, "symbolic-ref", "refs/heads/up", target)
    report = run(repo, lambda: git(repo, "update-ref", target, first))
    change, = report.changes
    assert change.ref == "refs/heads/up" and change.change is Change.MOVED and report.needs_you
    assert (change.old, change.new) == (tip, first)
    git_safe.run(repo, *shlex.split(change.restore)[1:], check=True, capture_output=True)
    assert git(repo, "rev-parse", target) == tip and git(repo, "symbolic-ref", "refs/heads/up") == target


def _link_main_to_hidden(repo):
    git(repo, "update-ref", "refs/hidden/x", git(repo, "rev-parse", "HEAD"))
    git(repo, "symbolic-ref", "refs/heads/main", "refs/hidden/x")


def test_a_commit_on_a_task_branch_that_is_a_link_is_not_called_a_rewrite(repo):
    _link_main_to_hidden(repo)
    report = run(repo, lambda: commit(repo, "b.txt"))
    assert report.own_commits == 1
    assert all(c.change is not Change.NON_FAST_FORWARD for c in report.changes)
    change, = report.changes  # the link's value moved with its uncompared target
    assert change.ref == "refs/heads/main" and change.change is Change.MOVED


def test_a_task_branch_turned_into_a_link_after_a_commit_is_not_called_a_rewrite(repo):
    def go():
        commit(repo, "b.txt")
        git(repo, "update-ref", "refs/hidden/x", git(repo, "rev-parse", "HEAD"))
        git(repo, "symbolic-ref", "refs/heads/main", "refs/hidden/x")
    report = run(repo, go)
    assert report.own_commits == 1
    assert all(c.change is not Change.NON_FAST_FORWARD for c in report.changes)
    assert [c.change for c in report.changes] == [Change.MOVED]


def test_a_real_reset_behind_a_link_on_the_task_branch_is_still_a_rewrite(repo):
    commit(repo, "b.txt")
    _link_main_to_hidden(repo)
    report = run(repo, lambda: git(repo, "reset", "-q", "--hard", "HEAD~1"))
    change, = report.changes
    assert change.change is Change.NON_FAST_FORWARD and report.own_commits == 0


def test_the_restore_of_a_link_into_an_uncompared_namespace_does_not_follow_a_symbolic_ref(repo):
    commit(repo, "b.txt")
    tip, first = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD~1")
    git(repo, "update-ref", "refs/hidden/x", tip)
    git(repo, "symbolic-ref", "refs/heads/up", "refs/hidden/x")
    report = run(repo, lambda: git(repo, "update-ref", "refs/hidden/x", first))
    change, = report.changes
    assert "--no-deref" in shlex.split(change.restore)
    git(repo, "symbolic-ref", "refs/hidden/y", "refs/heads/other")  # the name has since become a symbolic ref
    other = git(repo, "rev-parse", "other")
    git_safe.run(repo, *shlex.split(change.restore.replace("refs/hidden/x", "refs/hidden/y"))[1:],
                 capture_output=True)
    assert git(repo, "rev-parse", "other") == other  # nothing it points at moved
    git_safe.run(repo, *shlex.split(change.restore)[1:], check=True, capture_output=True)
    assert git(repo, "rev-parse", "refs/hidden/x") == tip and git(repo, "rev-parse", "other") == other


def _sha_of(repo, *args, stdin=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=stdin, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_a_new_tip_that_is_not_a_commit_is_undecided_not_a_fast_forward(repo):
    old = git(repo, "rev-parse", "HEAD")
    blob = _sha_of(repo, "hash-object", "-w", "--stdin", stdin="x")
    verdict, _ = asyncio.run(refs.fast_forward(repo, old, blob))
    assert verdict is refs.Ancestry.UNDECIDED


def test_a_commit_with_a_malformed_parent_line_is_undecided_not_a_fast_forward(repo):
    old = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    ident = "T <t@example.invalid> 1700000000 +0000"
    body = f"tree {tree}\nparent nothex\nauthor {ident}\ncommitter {ident}\n\nx\n"
    bad = _sha_of(repo, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=body)
    verdict, _ = asyncio.run(refs.fast_forward(repo, old, bad))
    assert verdict is refs.Ancestry.UNDECIDED


def test_a_walk_that_times_out_is_undecided_not_a_fast_forward(repo, monkeypatch):
    old = git(repo, "rev-parse", "HEAD")
    commit(repo, "b.txt")
    monkeypatch.setattr(refs, "GIT_TIMEOUT_SEC", 0)

    async def counted(*_args):  # the count step must not be the one that gives up
        return 0, "1\n"
    monkeypatch.setattr(refs, "_git", counted)
    verdict, _ = asyncio.run(refs.fast_forward(repo, old, git(repo, "rev-parse", "HEAD")))
    assert verdict is refs.Ancestry.UNDECIDED


def test_an_error_while_deciding_is_undecided_not_a_fast_forward(repo, monkeypatch):
    async def boom(*_args):
        raise RuntimeError("boom")
    monkeypatch.setattr(refs, "_reaches", boom)
    verdict, _ = asyncio.run(refs.fast_forward(repo, "a" * 40, "b" * 40))
    assert verdict is refs.Ancestry.UNDECIDED


def test_a_new_tip_that_is_missing_is_undecided_not_a_fast_forward(repo):
    old = git(repo, "rev-parse", "HEAD")
    verdict, _ = asyncio.run(refs.fast_forward(repo, old, "e" * 40))  # cat-file answers "<sha> missing": two fields
    assert verdict is refs.Ancestry.UNDECIDED


def _run_restores(repo, report) -> None:
    """Every restore command in the order the report lists them; the task branch resolves after each one."""
    for change in (*report.changes, *report.info):
        git_safe.run(repo, *shlex.split(change.restore)[1:], check=True, capture_output=True)
        git(repo, "rev-parse", "--verify", "refs/heads/main")  # never a dangling symbolic ref


@pytest.mark.parametrize("target", ["refs/heads/a-fresh", "refs/heads/z-fresh", "refs/heads/other",
                                    "refs/heads/looper/task-two/step"])  # created before or after main, moved, info
def test_a_task_branch_turned_into_a_link_to_a_compared_branch_is_reported_and_restored_first(repo, target):
    existed = git(repo, "for-each-ref", "--format=%(objectname)", target)

    def go():
        commit(repo, "b.txt")
        git(repo, "update-ref", target, git(repo, "rev-parse", "HEAD"))
        git(repo, "symbolic-ref", "refs/heads/main", target)

    async def active(_: str) -> bool:
        return True

    report = run(repo, go, active=active)
    tip = git(repo, "rev-parse", target)
    link = report.changes[0]  # ahead of every command that deletes or moves the target
    assert link.ref == "refs/heads/main" and link.change is Change.MOVED and link.new == tip
    assert link.restore == f"git update-ref --no-deref refs/heads/main {tip} {tip}"
    assert [c.ref for c in (*report.changes, *report.info)] == ["refs/heads/main", target]
    assert report.own_commits == 1
    _run_restores(repo, report)
    after = asyncio.run(refs.snapshot(repo))
    assert after.symrefs == {} and after.refs["refs/heads/main"] == tip
    assert after.refs.get(target, "") == existed and after.head == "refs/heads/main"


def test_a_task_branch_retargeted_at_the_same_commit_is_reported_and_restorable(repo):
    head = git(repo, "rev-parse", "HEAD")  # other is on the same commit
    before = asyncio.run(refs.snapshot(repo))
    report = run(repo, lambda: git(repo, "symbolic-ref", "refs/heads/main", "refs/heads/other"))
    change, = report.changes
    assert change.ref == "refs/heads/main" and change.change is Change.MOVED and change.old == change.new == head
    assert change.restore == f"git update-ref --no-deref refs/heads/main {head} {head}"
    assert report.own_commits == 0 and not report.needs_you
    _run_restores(repo, report)
    assert asyncio.run(refs.snapshot(repo)) == before


def test_a_task_branch_reset_and_then_linked_to_a_new_branch_goes_back_before_the_branch_is_removed(repo):
    commit(repo, "b.txt")
    tip, first = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD~1")
    before = asyncio.run(refs.snapshot(repo))

    def go():
        git(repo, "update-ref", "refs/heads/a-fresh", first)
        git(repo, "symbolic-ref", "refs/heads/main", "refs/heads/a-fresh")

    report = run(repo, go)
    link = report.changes[0]
    assert link.ref == "refs/heads/main" and link.change is Change.NON_FAST_FORWARD
    assert link.restore == f"git update-ref --no-deref refs/heads/main {tip} {first}"
    _run_restores(repo, report)
    assert asyncio.run(refs.snapshot(repo)) == before


@pytest.mark.parametrize("branch, reset", [("main", False), ("main", True), ("other", False)])
def test_the_restore_of_a_branch_turned_into_a_link_refuses_after_a_later_commit(repo, branch, reset):
    commit(repo, "b.txt")
    ref = f"refs/heads/{branch}"

    def go():
        if branch == "main":
            if reset:
                git(repo, "reset", "-q", "--hard", "HEAD~1")
            else:
                commit(repo, "c.txt")
        start = git(repo, "rev-parse", ref)
        if branch == "other":
            start = git(repo, "commit-tree", "-p", start, "-m", "turn", "HEAD^{tree}")
        git(repo, "update-ref", "refs/hidden/x", start)
        git(repo, "symbolic-ref", ref, "refs/hidden/x")

    report = run(repo, go)
    change = by_ref(report)[ref]
    later = git(repo, "commit-tree", "-p", change.new, "-m", "later", "HEAD^{tree}")
    git(repo, "update-ref", ref, later)  # through the link, after the report was written
    assert git(repo, "rev-parse", "refs/hidden/x") == later
    refused = git_safe.run(repo, *shlex.split(change.restore)[1:], capture_output=True)
    assert refused.returncode != 0
    assert git(repo, "rev-parse", ref) == later and git(repo, "symbolic-ref", ref) == "refs/hidden/x"
    assert shlex.split(change.restore)[-1] == change.new
    git(repo, "update-ref", ref, change.new)  # still on the reported commit: the command applies
    git_safe.run(repo, *shlex.split(change.restore)[1:], check=True, capture_output=True)
    assert ref not in asyncio.run(refs.snapshot(repo)).symrefs


@pytest.mark.parametrize("target", ["refs/heads/a-fresh", "refs/heads/main"])  # removed, or moved back
def test_a_new_link_is_restored_before_the_command_that_moves_or_removes_its_target(repo, target):
    commit(repo, "b.txt")
    before = asyncio.run(refs.snapshot(repo))

    def go():
        if target == "refs/heads/main":
            git(repo, "reset", "-q", "--hard", "HEAD~1")
        else:
            git(repo, "update-ref", target, git(repo, "rev-parse", "HEAD"))
        git(repo, "symbolic-ref", "refs/heads/other", target)

    report = run(repo, go)
    _run_restores(repo, report)
    assert asyncio.run(refs.snapshot(repo)) == before


def _link_main_before_the_turn(repo, target: str) -> tuple[str, str]:
    commit(repo, "b.txt")
    tip, first = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD~1")
    git(repo, "update-ref", target, tip)
    git(repo, "symbolic-ref", "refs/heads/main", target)
    return tip, first


@pytest.mark.parametrize("target", ["refs/heads/other", "refs/tags/v1", "refs/heads/looper/task-two/step"])
def test_a_reset_on_a_task_branch_already_linked_to_a_compared_ref_is_a_rewrite(repo, target):
    tip, first = _link_main_before_the_turn(repo, target)
    before = asyncio.run(refs.snapshot(repo))

    async def active(_: str) -> bool:
        return True

    report = run(repo, lambda: git(repo, "reset", "-q", "--hard", "HEAD~1"), active=active)
    change = report.changes[0]
    assert change.ref == "refs/heads/main" and change.change is Change.NON_FAST_FORWARD
    assert (change.old, change.new) == (tip, first) and report.own_commits == 0
    assert change.restore == f"git update-ref --no-deref {target} {tip} {first}"
    assert [c.ref for c in (*report.changes, *report.info)] == ["refs/heads/main", target]  # the target once
    git_safe.run(repo, *shlex.split(change.restore)[1:], check=True, capture_output=True)
    assert asyncio.run(refs.snapshot(repo)) == before


def test_a_commit_on_a_task_branch_already_linked_to_a_compared_ref_is_the_tasks_own(repo):
    _link_main_before_the_turn(repo, "refs/heads/other")
    report = run(repo, lambda: commit(repo, "c.txt"))
    assert report.own_commits == 1
    assert [c.ref for c in report.changes] == ["refs/heads/other"]  # its own change, reported once under its name


def test_an_undecided_move_on_a_task_branch_already_linked_to_a_compared_ref_is_unverified(repo, monkeypatch):
    _link_main_before_the_turn(repo, "refs/heads/other")
    monkeypatch.setattr(refs, "OBJECT_LIMIT", 10)
    report = run(repo, lambda: commit(repo, "c.txt"))
    assert report.changes[0].ref == "refs/heads/main"
    assert report.changes[0].change is Change.FAST_FORWARD_UNVERIFIED


def _apply_in_order(repo, report) -> None:
    """Every command, in the order the report lists them, run the way the operator runs it: each one succeeds."""
    for change in (*report.changes, *report.info):
        done = subprocess.run(["bash", "--noprofile", "--norc", "-c", change.restore], cwd=repo, env=_shell_env(),
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, f"{change.restore}: {done.stderr}"


def _shell_env() -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


@pytest.mark.parametrize("chain", [
    ("m-inner", "z-outer"),  # innermost first; the inner name sorts first
    ("m-inner", "a-outer"),
    ("z-inner", "a-mid", "m-outer"),
])
def test_nested_new_links_are_restored_outermost_first_when_the_commit_moved(repo, chain):
    commit(repo, "b.txt")
    git(repo, "checkout", "-q", "other")  # leaf and the links are ordinary branches
    for name in ("leaf", *chain):
        git(repo, "branch", name, "main")
    before = asyncio.run(refs.snapshot(repo))

    def go():
        git(repo, "update-ref", "refs/heads/leaf", "refs/heads/main~1")
        for target, name in zip(("leaf", *chain), chain):
            git(repo, "symbolic-ref", f"refs/heads/{name}", f"refs/heads/{target}")

    report = run(repo, go, branch="other")
    order = [change.ref.removeprefix("refs/heads/") for change in report.changes]
    assert order == [*reversed(chain), "leaf"]
    _apply_in_order(repo, report)
    assert asyncio.run(refs.snapshot(repo)) == before


@pytest.mark.parametrize("outer", ["a-outer", "z-outer"])  # sorts before and after the task branch
def test_a_link_to_the_task_branch_turned_link_is_restored_before_the_task_branch(repo, outer):
    commit(repo, "b.txt")
    git(repo, "branch", "leaf")
    git(repo, "branch", outer)
    before = asyncio.run(refs.snapshot(repo))

    def go():
        git(repo, "update-ref", "refs/heads/leaf", "refs/heads/main~1")
        git(repo, "symbolic-ref", "refs/heads/main", "refs/heads/leaf")
        git(repo, "symbolic-ref", f"refs/heads/{outer}", "refs/heads/main")

    report = run(repo, go)
    assert [change.ref for change in report.changes] == [f"refs/heads/{outer}", "refs/heads/main", "refs/heads/leaf"]
    assert report.changes[1].change is Change.NON_FAST_FORWARD and report.own_commits == 0
    _apply_in_order(repo, report)
    assert asyncio.run(refs.snapshot(repo)) == before


@pytest.mark.parametrize("leaf_existed, through", [
    (False, "refs/heads/leaf"),  # the command on the leaf deletes it
    (True, "refs/heads/leaf"),  # or moves it back
    (False, "refs/hidden/mid"),  # a hop outside the compared refs: only the chain's end says what lies below
])
def test_another_tasks_new_link_is_restored_before_its_target_is_deleted_or_moved(repo, leaf_existed, through):
    commit(repo, "b.txt")
    git(repo, "checkout", "-q", "other")
    step = "refs/heads/looper/task-two/step"
    for ref in (step, "refs/heads/a-outer", "refs/heads/looper/task-two/bystander"):
        git(repo, "update-ref", ref, "refs/heads/main")
    if leaf_existed:
        git(repo, "update-ref", "refs/heads/leaf", "refs/heads/main")
    before = asyncio.run(refs.snapshot(repo))

    def go():
        git(repo, "update-ref", "refs/heads/leaf", "refs/heads/main~1")
        if through != "refs/heads/leaf":
            git(repo, "symbolic-ref", through, "refs/heads/leaf")
        git(repo, "symbolic-ref", step, through)
        git(repo, "symbolic-ref", "refs/heads/a-outer", step)
        git(repo, "update-ref", "refs/heads/looper/task-two/bystander", "refs/heads/main~1")

    async def active(_: str) -> bool:
        return True

    report = run(repo, go, active=active, branch="other")
    # The other task's link has to run before a change, so it is listed among the changes, ahead of that one; an
    # entry that no change has to wait for stays information.
    assert [change.ref for change in report.changes] == ["refs/heads/a-outer", step, "refs/heads/leaf"]
    assert [change.ref for change in report.info] == ["refs/heads/looper/task-two/bystander"]
    assert report.needs_you
    _apply_in_order(repo, report)
    assert asyncio.run(refs.snapshot(repo)) == before


def test_moving_the_commit_in_another_tasks_link_alone_does_not_need_you(repo):
    commit(repo, "b.txt")
    step, leaf = "refs/heads/looper/task-two/step", "refs/heads/looper/task-two/leaf"
    git(repo, "update-ref", step, "refs/heads/main")
    before = asyncio.run(refs.snapshot(repo))

    def go():
        git(repo, "update-ref", leaf, "refs/heads/main~1")
        git(repo, "symbolic-ref", step, leaf)

    async def active(_: str) -> bool:
        return True

    report = run(repo, go, active=active)
    assert report.changes == () and [change.ref for change in report.info] == [step, leaf]
    assert not report.needs_you
    _apply_in_order(repo, report)
    assert asyncio.run(refs.snapshot(repo)) == before


def test_the_snapshot_records_the_direct_target_of_a_symbolic_ref(repo):
    git(repo, "symbolic-ref", "refs/heads/inner", "refs/heads/other")
    git(repo, "symbolic-ref", "refs/heads/outer", "refs/heads/inner")
    found = asyncio.run(refs.snapshot(repo))
    assert found.symrefs == {"refs/heads/inner": "refs/heads/other", "refs/heads/outer": "refs/heads/other"}
    assert found.hops == {"refs/heads/inner": "refs/heads/other", "refs/heads/outer": "refs/heads/inner"}


@pytest.mark.parametrize("why", ["no_option", "too_many"])
def test_without_direct_targets_the_snapshot_still_reads_and_says_so_once(repo, monkeypatch, caplog, why):
    git(repo, "symbolic-ref", "refs/heads/inner", "refs/heads/other")
    git(repo, "symbolic-ref", "refs/heads/outer", "refs/heads/inner")
    real, asked = refs._git, []

    async def counted(work_dir, *args):
        if "--no-recurse" in args:
            asked.append(args)
            return 129, ""  # a git that does not know the option
        return await real(work_dir, *args)

    monkeypatch.setattr(refs, "_git", counted)
    if why == "too_many":
        monkeypatch.setattr(refs, "SYMREF_LIMIT", 1)
    with caplog.at_level("WARNING", logger=refs.__name__):
        found = asyncio.run(refs.snapshot(repo))
    assert found.hops == {} and len(found.symrefs) == 2
    assert len(asked) == (0 if why == "too_many" else 1) and len(caplog.records) == 1


def test_an_order_that_cannot_be_built_falls_back_and_logs_one_line(repo, caplog):
    head = git(repo, "rev-parse", "HEAD")
    a, b = "refs/heads/a", "refs/heads/b"
    before = refs.Snapshot({a: head, b: head}, "refs/heads/main")
    # git never lists mutually pointing refs; a snapshot that claims them must not end the turn's report.
    after = refs.Snapshot({a: head, b: head}, "refs/heads/main", {a: b, b: a}, {a: b, b: a})
    with caplog.at_level("WARNING", logger=refs.__name__):
        report = asyncio.run(refs.compare(repo, before, after, task_branch="main", task_id=TASK,
                                          active_task=inactive))
    assert [change.ref for change in report.changes] == [a, b] and len(caplog.records) == 1


# --- Generated ref layouts: every command the report lists succeeds in order and puts the refs back ---

OTHER_TASK = "refs/heads/looper/task-two/"
MAIN = "refs/heads/main"
CASES, CHUNK = 200, 50


def _history(repo: Path) -> dict[str, str]:
    """``base`` <- ``tip`` <- ``next``, and ``side`` on ``base``; fixed dates, so a seed replays exactly."""
    env = {**_shell_env(), "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000"}

    def plumb(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], env=env, input="", check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()

    tree = plumb("mktree")
    made = {"base": plumb("commit-tree", "-m", "base", tree)}
    for name, parent in (("tip", "base"), ("next", "tip"), ("side", "base")):
        made[name] = plumb("commit-tree", "-p", made[parent], "-m", name, tree)
    return made


class Layout:
    """The stored refs of a generated case: a commit id, or ``("ref", target)`` for a symbolic ref. Written as loose
    ref files, which is what a turn's own git commands leave behind."""

    def __init__(self, rng: random.Random, commits: dict[str, str]):
        self.rng, self.commits, self.refs, self.count = rng, commits, {}, 0

    def name(self) -> str:
        self.count += 1
        prefix = self.rng.choices(["refs/heads/", OTHER_TASK, "refs/tags/"], [7, 3, 1])[0]
        return f"{prefix}{self.rng.choice(string.ascii_lowercase)}{self.count}"

    def build(self) -> None:
        chains = [[self.name() for _ in range(self.rng.randint(1, 4))] for _ in range(self.rng.randint(1, 3))]
        if self.rng.random() < 0.6:  # the task branch anywhere in a chain, most often as its end
            chain = self.rng.choice(chains)
            at = len(chain) - 1 if self.rng.random() < 0.7 else self.rng.randrange(len(chain))
            chain[at] = MAIN
        else:
            chains.append([MAIN])
        for chain in chains:  # outermost first
            for outer, inner in zip(chain, chain[1:]):
                self.refs[outer] = ("ref", inner)
            end = self.rng.random()
            if chain[-1] != MAIN and end < 0.15:  # the chain ends outside the compared namespaces
                hidden = f"refs/hidden/{chain[-1].rsplit('/', 1)[1]}"
                if len(chain) > 1:
                    self.refs[chain[-2]] = ("ref", hidden)
                    chain[-1] = hidden
            if chain[-1] != MAIN and len(chain) > 1 and 0.15 <= end < 0.25:
                continue  # a dangling chain: its end does not exist
            self.refs[chain[-1]] = self.commits["tip"]

    def reaches(self, start: str, wanted: str) -> bool:
        seen = set()
        while start not in seen:
            if start == wanted:
                return True
            seen.add(start)
            value = self.refs.get(start)
            if not isinstance(value, tuple):
                return False
            start = value[1]
        return True

    def depth(self, name: str) -> int:
        steps = 0
        while isinstance(self.refs.get(name), tuple) and steps < 8:
            name, steps = self.refs[name][1], steps + 1
        return steps

    def link(self, name: str, target: str) -> bool:
        # Symbolic refs are only made among the compared names: Looper never reads another namespace, so a chain
        # that leaves refs/heads and refs/tags and comes back through a symbolic ref there has hops it cannot know.
        if name.startswith("refs/hidden/") or self.reaches(target, name):
            return False
        old = self.refs.get(name)
        self.refs[name] = ("ref", target)
        if max(self.depth(each) for each in self.refs) <= 3:
            return True
        if old is None:
            del self.refs[name]
        else:
            self.refs[name] = old
        return False

    def mutate(self) -> str:
        rng, commits = self.rng, self.commits
        direct = sorted(n for n, v in self.refs.items() if not isinstance(v, tuple))
        links = sorted(n for n, v in self.refs.items() if isinstance(v, tuple))
        compared = sorted(n for n in self.refs if not n.startswith("refs/hidden/"))
        targets = sorted({v[1] for v in self.refs.values() if isinstance(v, tuple) and v[1] in self.refs})
        kind = rng.choices(["reset", "ff", "rewrite", "create_link", "create", "delete_link", "retarget",
                            "delete_target", "unlink", "nest"], [4, 2, 1, 2, 2, 1, 4, 2, 1, 4])[0]
        if kind in ("reset", "ff", "rewrite") and direct:
            name = rng.choice(direct)
            self.refs[name] = commits[{"reset": "base", "ff": "next", "rewrite": "side"}[kind]]
            return f"{kind} {name}"
        if kind == "create":
            name = self.name()
            self.refs[name] = commits[rng.choice(sorted(commits))]
            return f"create {name}"
        if kind == "create_link" and self.refs:
            name, target = self.name(), rng.choice(sorted(self.refs) + ["refs/heads/nothing"])
            return f"create link {name} -> {target}" if self.link(name, target) else "none"
        if kind == "delete_link" and links:
            name = rng.choice(links)
            del self.refs[name]
            return f"delete link {name}"
        if kind == "retarget" and compared:
            name, target = rng.choice(compared), rng.choice(sorted(self.refs))
            return f"link {name} -> {target}" if self.link(name, target) else "none"
        if kind == "delete_target" and targets:
            name = rng.choice(targets)
            del self.refs[name]
            return f"delete {name}"
        if kind == "unlink" and links:
            name = rng.choice(links)
            self.refs[name] = commits[rng.choice(sorted(commits))]
            return f"direct {name}"
        if kind == "nest" and len(compared) > 1:  # several names become a chain onto an end that moves
            end = rng.choice(direct) if direct else None
            if end is None:
                return "none"
            others = [n for n in compared if n != end]
            chain = [end, *rng.sample(others, min(len(others), rng.randint(1, 3)))]
            made = [f"{name} -> {target}" for target, name in zip(chain, chain[1:]) if self.link(name, target)]
            if rng.random() < 0.8:
                self.refs[end] = commits[rng.choice(["base", "next", "side"])]
            return f"nest {made} on {end}"
        return "none"

    def write(self, repo: Path) -> None:
        for folder in ("refs/heads", "refs/tags", "refs/hidden", "logs"):
            shutil.rmtree(repo / ".git" / folder, ignore_errors=True)
        for folder in ("refs/heads", "refs/tags"):
            (repo / ".git" / folder).mkdir(parents=True)
        for name, value in self.refs.items():
            path = repo / ".git" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"ref: {value[1]}\n" if isinstance(value, tuple) else value + "\n")


def _hidden(repo: Path) -> dict[str, str]:
    root = repo / ".git" / "refs" / "hidden"
    return {f"refs/hidden/{path.name}": path.read_text().strip() for path in root.glob("*")}


def _unrestorable(initial: dict, final: dict, before: refs.Snapshot, after: refs.Snapshot) -> str | None:
    """Why no order of the listed commands can put every ref of this layout back, or None."""
    link = before.symrefs.get(MAIN)
    if link is not None and MAIN in after.refs and after.symrefs.get(MAIN) != link:
        # A turn does not start while the task branch is a symbolic ref (HEAD then reads as its target), and the
        # command for a task branch whose link changed makes it a direct ref on purpose.
        return "task branch was a link and changed"
    if any(not target.startswith(("refs/heads/", "refs/tags/")) and target not in final
           for target in before.symrefs.values()):
        # The turn deleted a ref outside the compared namespaces that a link read through. Looper never reads or
        # restores those, so no command can bring the link's value back.
        return "an uncompared target was deleted"
    for name, value in final.items():
        if not isinstance(value, tuple) or initial.get(name) == value:
            continue
        if name not in before.refs and name not in after.refs:
            # git does not list a dangling symbolic ref, so a link this turn made (or changed) that dangles at its
            # end has no entry and no command, and it starts to resolve again when its target is put back.
            return "a new dangling link is not listed"
        if isinstance(initial.get(name), tuple) and before.symrefs.get(name) == after.symrefs.get(name) is not None:
            # The link points somewhere else but its chain ends at the same ref: a change is decided from
            # %(symref), the chain's end, so the name has no command, and restoring the ref it now goes through
            # takes it along.
            return "a link changed only its direct target"
    return None


async def _generated_case(repo: Path, commits: dict[str, str], seed: int) -> tuple[str, int]:
    """One seeded case. Every listed command must succeed in order. The answer is "restored" once the refs are
    compared too, or why the listed commands cannot put every ref back (``_unrestorable``), with their number."""
    layout = Layout(random.Random(seed), commits)
    layout.build()
    initial = dict(layout.refs)
    layout.write(repo)
    before, hidden_before = await refs.snapshot(repo), _hidden(repo)
    steps = [layout.mutate() for _ in range(layout.rng.randint(1, 5))]
    layout.write(repo)
    after = await refs.snapshot(repo)
    report = await refs.compare(repo, before, after, task_branch="main", task_id=TASK, active_task=_only_task_two)
    commands = [change.restore for change in (*report.changes, *report.info)]
    context = (f"seed={seed}\nbefore={initial}\nturn={steps}\nafter={layout.refs}\ncommits={commits}\n"
               "commands:\n  " + "\n  ".join(commands))
    # An intact link's command is its target's: it is listed under each name that reads through it. The first run
    # applies it; a repeat must be refused by its current-value check.
    script, ran = [], set()
    for index, command in enumerate(commands):
        script.append(f"if {command} 2>/dev/null; then echo 'repeat {index} applied'; exit 1; fi"
                      if command in ran else f"{command} || {{ echo 'command {index} exit '$?; exit 1; }}")
        ran.add(command)
    done = subprocess.run(["bash", "--noprofile", "--norc", "-c", "\n".join(script) or "true"], cwd=repo,
                          env=_shell_env(), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, f"{done.stdout}{done.stderr}{context}"
    if reason := _unrestorable(initial, layout.refs, before, after):
        return reason, len(commands)
    restored, hidden_after = await refs.snapshot(repo), _hidden(repo)
    # The task branch keeps the commits the turn made on it: a fast-forward is the Manager's own work.
    descends = {(commits["base"], commits["tip"]), (commits["base"], commits["next"]),
                (commits["tip"], commits["next"]), (commits["base"], commits["side"])}
    old, new = before.refs.get(MAIN), after.refs.get(MAIN)
    kept = MAIN not in before.symrefs and (old, new) in descends
    wrong = []
    for name in sorted(before.refs.keys() | after.refs.keys() | restored.refs.keys()):
        if name in before.symrefs:
            want, got = ("ref", before.symrefs[name]), ("ref", restored.symrefs.get(name))
        else:
            want = new if name == MAIN and kept else before.refs.get(name)
            got = ("ref", restored.symrefs[name]) if name in restored.symrefs else restored.refs.get(name)
        if want != got:
            wrong.append(f"{name}: want {want}, got {got}")
    for change in (*report.changes, *report.info):  # a command on an uncompared target puts that target back
        written = shlex.split(change.restore)
        if written[:2] == ["git", "update-ref"] and written[-3].startswith("refs/hidden/"):
            if hidden_after.get(written[-3]) != hidden_before.get(written[-3]):
                wrong.append(f"{written[-3]}: want {hidden_before.get(written[-3])}, got {hidden_after.get(written[-3])}")
    assert not wrong, "\n".join(wrong) + "\n" + context
    return "restored", len(commands)


async def _only_task_two(task_id: str) -> bool:
    return task_id == "task-two"


@pytest.mark.parametrize("first", range(0, CASES, CHUNK))
def test_the_listed_commands_restore_generated_ref_layouts_in_order(tmp_path, first):
    repo = tmp_path / "generated"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commits = _history(repo)

    async def go():
        return [await _generated_case(repo, commits, seed) for seed in range(first, first + CHUNK)]

    outcomes = asyncio.run(go())
    restored = [count for outcome, count in outcomes if outcome == "restored"]
    # The generator must keep exercising the restore: most layouts compared ref by ref, many with several commands.
    assert len(restored) >= CHUNK * 3 // 4 and sum(count >= 3 for count in restored) >= CHUNK // 4, outcomes
