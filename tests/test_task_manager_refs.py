"""Ref snapshots and reports of a direct session's turn: what changed, how to restore it, and what is never reported."""
from __future__ import annotations

import asyncio
import contextlib
import json
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
    assert "symref-delete refs/heads/link refs/heads/main" in change.restore
    _apply_in_order(repo, report)
    assert git(repo, "rev-parse", "--verify", "refs/heads/main")  # the target is still there
    assert asyncio.run(refs.snapshot(repo)) == before


def test_retargeting_a_symbolic_ref_to_the_same_commit_is_a_change_and_restores(repo):
    git(repo, "symbolic-ref", "refs/heads/link", "refs/heads/other")  # other and main are the same commit
    report = run(repo, lambda: git(repo, "symbolic-ref", "refs/heads/link", "refs/heads/main"))
    change, = report.changes
    assert change.change is Change.MOVED and change.old == change.new
    _apply_in_order(repo, report)
    assert git(repo, "symbolic-ref", "refs/heads/link") == "refs/heads/other"


def test_deleting_a_symbolic_ref_restores_the_link_not_a_copy(repo):
    git(repo, "symbolic-ref", "refs/heads/link", "refs/heads/other")
    before = asyncio.run(refs.snapshot(repo))
    report = run(repo, lambda: git(repo, "symbolic-ref", "--delete", "refs/heads/link"))
    change, = report.changes
    assert change.change is Change.DELETED and "symref-create refs/heads/link refs/heads/other" in change.restore
    _apply_in_order(repo, report)
    assert asyncio.run(refs.snapshot(repo)) == before


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
    _apply_in_order(repo, report)
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


def test_the_restore_of_a_link_into_an_uncompared_namespace_refuses_once_a_hop_changed(repo):
    commit(repo, "b.txt")
    tip, first = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD~1")
    git(repo, "update-ref", "refs/hidden/x", tip)
    git(repo, "symbolic-ref", "refs/heads/up", "refs/hidden/x")
    report = run(repo, lambda: git(repo, "update-ref", "refs/hidden/x", first))
    change, = report.changes
    # The hop has since become a symbolic ref that resolves to the very commit the report names.
    git(repo, "update-ref", "refs/hidden/y", first)
    git(repo, "symbolic-ref", "refs/hidden/x", "refs/hidden/y")
    assert _run(repo, change.restore).returncode != 0
    assert git(repo, "rev-parse", "refs/hidden/y") == first
    assert git(repo, "symbolic-ref", "refs/hidden/x") == "refs/hidden/y"
    git(repo, "symbolic-ref", "--delete", "refs/hidden/x")
    git(repo, "update-ref", "refs/hidden/x", first)  # as reported again: the command applies
    _apply_in_order(repo, report)
    assert git(repo, "rev-parse", "refs/hidden/x") == tip


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


def _run(repo, command: str) -> subprocess.CompletedProcess:
    """One restore command, run the way the operator runs it."""
    return subprocess.run(["bash", "--noprofile", "--norc", "-c", command], cwd=repo, env=_shell_env(),
                          capture_output=True, text=True, timeout=30)


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
    link = report.changes[0]
    assert link.ref == "refs/heads/main" and link.change is Change.MOVED and link.new == tip
    # Both entries are tied by the link: one command, listed under each, so none is left as information.
    assert [c.ref for c in report.changes] == ["refs/heads/main", target] and report.info == ()
    assert len({c.restore for c in report.changes}) == 1
    assert report.own_commits == 1
    _apply_in_order(repo, report)
    after = asyncio.run(refs.snapshot(repo))
    assert after.symrefs == {} and after.refs["refs/heads/main"] == tip
    assert after.refs.get(target, "") == existed and after.head == "refs/heads/main"


def test_a_task_branch_retargeted_at_the_same_commit_is_reported_and_restorable(repo):
    head = git(repo, "rev-parse", "HEAD")  # other is on the same commit
    before = asyncio.run(refs.snapshot(repo))
    report = run(repo, lambda: git(repo, "symbolic-ref", "refs/heads/main", "refs/heads/other"))
    change, = report.changes
    assert change.ref == "refs/heads/main" and change.change is Change.MOVED and change.old == change.new == head
    assert report.own_commits == 0 and not report.needs_you
    _apply_in_order(repo, report)
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
    _apply_in_order(repo, report)
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
    assert _run(repo, change.restore).returncode != 0
    assert git(repo, "rev-parse", ref) == later and git(repo, "symbolic-ref", ref) == "refs/hidden/x"
    git(repo, "update-ref", ref, change.new)  # still on the reported commit: the command applies
    _apply_in_order(repo, report)
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
    _apply_in_order(repo, report)
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
    assert [c.ref for c in (*report.changes, *report.info)] == ["refs/heads/main", target]  # the target once
    _apply_in_order(repo, report)
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
    """Every command in the order the report lists them: each one succeeds. Entries that share a command need it
    once; running it again is refused."""
    ran = set()
    for change in (*report.changes, *report.info):
        done = _run(repo, change.restore)
        assert (done.returncode != 0) == (change.restore in ran), f"{change.restore}: {done.stderr}"
        ran.add(change.restore)


def _shell_env() -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _stored(repo: Path) -> dict:
    """What every ref holds, read from the files and ``packed-refs`` without the code under test: a commit id, or
    ``("ref", target)`` for a symbolic ref."""
    found = {}
    packed = repo / ".git" / "packed-refs"
    if packed.exists():
        for line in packed.read_text().splitlines():
            if line[0] not in "#^":
                oid, name = line.split(" ", 1)
                found[name] = oid
    for path in (repo / ".git" / "refs").rglob("*"):
        if path.is_file():
            body = path.read_text().rstrip("\n")
            found[path.relative_to(repo / ".git").as_posix()] = (
                ("ref", body.removeprefix("ref: ")) if body.startswith("ref: ") else body)
    return found


@pytest.mark.parametrize("chain", [
    ("m-inner", "z-outer"),  # innermost first
    ("m-inner", "a-outer"),
    ("z-inner", "a-mid", "m-outer"),
])
def test_nested_new_links_and_the_moved_commit_are_restored_by_one_command(repo, chain):
    commit(repo, "b.txt")
    git(repo, "checkout", "-q", "other")  # leaf and the links are ordinary branches
    for name in ("leaf", *chain):
        git(repo, "branch", name, "main")
    before = _stored(repo)

    def go():
        git(repo, "update-ref", "refs/heads/leaf", "refs/heads/main~1")
        for target, name in zip(("leaf", *chain), chain):
            git(repo, "symbolic-ref", f"refs/heads/{name}", f"refs/heads/{target}")

    report = run(repo, go, branch="other")
    assert sorted(c.ref.removeprefix("refs/heads/") for c in report.changes) == sorted([*chain, "leaf"])
    assert len({c.restore for c in report.changes}) == 1
    _apply_in_order(repo, report)
    assert _stored(repo) == before


@pytest.mark.parametrize("hidden", [False, True])  # the chain goes through compared names only, or leaves them
@pytest.mark.parametrize("what", ["retarget", "commit", "unlink", "delete"])
def test_a_chain_is_restored_hop_by_hop_not_to_its_end(repo, hidden, what):
    commit(repo, "b.txt")
    git(repo, "branch", "-f", "other", "main")
    git(repo, "checkout", "-q", "other")
    mid = "refs/hidden/mid" if hidden else "refs/heads/mid"
    outer, leaf = "refs/heads/outer", "refs/heads/leaf"
    git(repo, "update-ref", leaf, "refs/heads/main")
    git(repo, "symbolic-ref", mid, leaf)
    git(repo, "symbolic-ref", outer, mid)
    before = _stored(repo)

    def go():
        if what == "retarget":  # the same commit, so only the hop differs
            git(repo, "symbolic-ref", mid, "refs/heads/main")
        elif what == "commit":
            git(repo, "update-ref", leaf, "refs/heads/main~1")
        elif what == "unlink":
            git(repo, "update-ref", "--no-deref", mid, "refs/heads/main~1")
        else:
            git(repo, "symbolic-ref", "--delete", mid)

    report = run(repo, go, branch="other")
    assert report.changes and all(c.restore for c in report.changes)
    _apply_in_order(repo, report)
    assert _stored(repo) == before


def test_the_command_of_a_chain_checks_every_hop_it_reads_through(repo):
    git(repo, "checkout", "-q", "other")
    mid, leaf, outer = "refs/heads/mid", "refs/heads/leaf", "refs/heads/outer"
    git(repo, "update-ref", leaf, "refs/heads/main")
    git(repo, "symbolic-ref", mid, leaf)
    report = run(repo, lambda: git(repo, "symbolic-ref", outer, mid), branch="other")
    change, = report.changes
    git(repo, "symbolic-ref", mid, "refs/heads/main")  # an inner hop moved after the report, to the same commit
    refused = _stored(repo)
    assert _run(repo, change.restore).returncode != 0 and _stored(repo) == refused
    git(repo, "symbolic-ref", mid, leaf)
    git(repo, "update-ref", leaf, git(repo, "commit-tree", "-p", leaf, "-m", "later", "HEAD^{tree}"))
    refused = _stored(repo)
    assert _run(repo, change.restore).returncode != 0 and _stored(repo) == refused


def test_a_branch_turned_link_is_not_made_direct_when_its_target_changed_since_the_report(repo):
    git(repo, "branch", "twin")
    report = run(repo, lambda: git(repo, "symbolic-ref", "refs/heads/other", "refs/heads/main"))
    change = by_ref(report)["refs/heads/other"]
    git(repo, "symbolic-ref", "refs/heads/other", "refs/heads/twin")  # it still reads the same commit
    refused = _stored(repo)
    assert _run(repo, change.restore).returncode != 0 and _stored(repo) == refused


@pytest.mark.parametrize("existed", [False, True])
def test_a_link_that_dangles_after_the_turn_is_reported_and_restored(repo, existed):
    link = "refs/heads/dang"
    if existed:
        git(repo, "update-ref", link, "HEAD")
    before = _stored(repo)
    report = run(repo, lambda: git(repo, "symbolic-ref", link, "refs/heads/nothing"))
    change, = report.changes
    assert change.ref == link and change.change is (Change.DELETED if existed else Change.CREATED)
    assert change.new is None and report.needs_you
    _apply_in_order(repo, report)
    assert _stored(repo) == before


def test_a_dangling_link_from_before_the_turn_that_does_not_change_is_not_reported(repo):
    git(repo, "symbolic-ref", "refs/heads/dang", "refs/heads/nothing")
    assert not run(repo, lambda: None).worth_saying


@pytest.mark.parametrize("hidden", [False, True])
def test_a_link_whose_direct_target_changed_while_its_chain_end_did_not_is_reported(repo, hidden):
    mid = "refs/hidden/mid" if hidden else "refs/heads/mid"
    git(repo, "symbolic-ref", mid, "refs/heads/other")
    git(repo, "symbolic-ref", "refs/heads/outer", mid)
    before = _stored(repo)
    report = run(repo, lambda: git(repo, "symbolic-ref", "refs/heads/outer", "refs/heads/other"))
    change, = report.changes
    assert change.ref == "refs/heads/outer" and change.change is Change.MOVED
    _apply_in_order(repo, report)
    assert _stored(repo) == before


def test_a_deleted_ref_outside_branches_and_tags_that_a_link_read_through_is_created_again(repo):
    git(repo, "update-ref", "refs/hidden/x", "HEAD")
    git(repo, "symbolic-ref", "refs/heads/up", "refs/hidden/x")
    before = _stored(repo)
    report = run(repo, lambda: git(repo, "update-ref", "-d", "refs/hidden/x"))
    change, = report.changes
    assert change.ref == "refs/heads/up" and change.change is Change.DELETED
    _apply_in_order(repo, report)
    assert _stored(repo) == before


def test_a_ref_outside_that_the_link_no_longer_reads_is_still_put_back(repo):
    commit(repo, "b.txt")
    git(repo, "update-ref", "refs/hidden/x", "HEAD")
    git(repo, "symbolic-ref", "refs/heads/up", "refs/hidden/x")
    before = _stored(repo)

    def go():
        git(repo, "symbolic-ref", "refs/heads/up", "refs/heads/other")
        git(repo, "update-ref", "refs/hidden/x", "HEAD~1")

    _apply_in_order(repo, run(repo, go))
    assert _stored(repo) == before


@pytest.mark.parametrize("outer", ["a-outer", "z-outer"])
def test_a_link_to_the_task_branch_turned_link_is_restored_with_it(repo, outer):
    commit(repo, "b.txt")
    git(repo, "branch", "leaf")
    git(repo, "branch", outer)
    before = _stored(repo)

    def go():
        git(repo, "update-ref", "refs/heads/leaf", "refs/heads/main~1")
        git(repo, "symbolic-ref", "refs/heads/main", "refs/heads/leaf")
        git(repo, "symbolic-ref", f"refs/heads/{outer}", "refs/heads/main")

    report = run(repo, go)
    assert [c.ref for c in report.changes] == ["refs/heads/main", *sorted(["refs/heads/leaf", f"refs/heads/{outer}"])]
    assert report.changes[0].change is Change.NON_FAST_FORWARD and report.own_commits == 0
    _apply_in_order(repo, report)
    assert _stored(repo) == before


def test_a_command_that_checks_the_task_branch_keeps_the_commits_of_the_turn(repo):
    def go():
        commit(repo, "b.txt")
        git(repo, "symbolic-ref", "refs/heads/other", "refs/heads/main")

    report = run(repo, go)
    tip = git(repo, "rev-parse", "main")
    _apply_in_order(repo, report)
    assert git(repo, "rev-parse", "main") == tip and report.own_commits == 1


@pytest.mark.parametrize("leaf_existed, through", [
    (False, "refs/heads/leaf"),  # the command deletes the leaf
    (True, "refs/heads/leaf"),  # or moves it back
    (False, "refs/hidden/mid"),  # a hop outside the compared refs
])
def test_another_tasks_link_that_shares_a_command_with_a_change_is_listed_with_the_changes(repo, leaf_existed, through):
    commit(repo, "b.txt")
    git(repo, "checkout", "-q", "other")
    step = "refs/heads/looper/task-two/step"
    for ref in (step, "refs/heads/a-outer", "refs/heads/looper/task-two/bystander"):
        git(repo, "update-ref", ref, "refs/heads/main")
    if leaf_existed:
        git(repo, "update-ref", "refs/heads/leaf", "refs/heads/main")
    before = _stored(repo)

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
    assert [c.ref for c in report.changes] == ["refs/heads/a-outer", "refs/heads/leaf", step]
    assert [c.ref for c in report.info] == ["refs/heads/looper/task-two/bystander"]
    assert report.needs_you
    _apply_in_order(repo, report)
    if through != "refs/heads/leaf":  # the turn made this ref outside: it is checked, not removed
        assert _stored(repo).pop(through) == ("ref", "refs/heads/leaf")
    assert {k: v for k, v in _stored(repo).items() if k != "refs/hidden/mid"} == before


def test_moving_the_commit_in_another_tasks_link_alone_does_not_need_you(repo):
    commit(repo, "b.txt")
    step, leaf = "refs/heads/looper/task-two/step", "refs/heads/looper/task-two/leaf"
    git(repo, "update-ref", step, "refs/heads/main")
    before = _stored(repo)

    def go():
        git(repo, "update-ref", leaf, "refs/heads/main~1")
        git(repo, "symbolic-ref", step, leaf)

    async def active(_: str) -> bool:
        return True

    report = run(repo, go, active=active)
    assert report.changes == () and [c.ref for c in report.info] == [leaf, step]
    assert not report.needs_you
    _apply_in_order(repo, report)
    assert _stored(repo) == before


def test_the_snapshot_records_every_hop_of_a_chain_dangling_ones_and_those_outside_included(repo):
    git(repo, "symbolic-ref", "refs/heads/inner", "refs/heads/other")
    git(repo, "symbolic-ref", "refs/heads/outer", "refs/heads/inner")
    git(repo, "symbolic-ref", "refs/hidden/far", "refs/hidden/gone")
    git(repo, "symbolic-ref", "refs/tags/dang", "refs/hidden/far")
    found = asyncio.run(refs.snapshot(repo))
    assert found.symrefs == {"refs/heads/inner": "refs/heads/other", "refs/heads/outer": "refs/heads/other"}
    assert found.hops == {"refs/heads/inner": "refs/heads/other", "refs/heads/outer": "refs/heads/inner",
                          "refs/tags/dang": "refs/hidden/far", "refs/hidden/far": "refs/hidden/gone"}
    assert found.outside == {"refs/hidden/gone": None} and found.gap is None


def test_the_walk_for_dangling_links_reads_no_fifo_and_follows_no_directory_link(repo, tmp_path):
    os.mkfifo(repo / ".git" / "refs" / "heads" / "pipe")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x").write_text("ref: refs/heads/main\n")
    (repo / ".git" / "refs" / "heads" / "far").symlink_to(tmp_path / "elsewhere")
    (repo / ".git" / "refs" / "tags" / "dang").write_text("ref: refs/heads/nothing\n")
    found = asyncio.run(asyncio.wait_for(refs.snapshot(repo), 20))
    assert found.symrefs == {} and found.hops == {"refs/tags/dang": "refs/heads/nothing"}
    assert set(found.unreadable) == {"refs/heads/pipe", "refs/heads/far"}


# --- Nothing outside the git directory is read: swapped directories and linked files ------------------------------


MARKER = "outside-marker"


def _outside(tmp_path, repo: Path, kind: str) -> tuple[Path, list[str]]:
    """A file outside the repository shaped like a ref (a symbolic one, or the id of a commit no branch holds), and
    every spelling of its content that must show nowhere."""
    folder = tmp_path / "elsewhere"
    folder.mkdir(exist_ok=True)
    if kind == "symref":
        body, secrets = f"ref: refs/heads/{MARKER}\n", [MARKER]
    else:
        oid = git(repo, "commit-tree", "-m", MARKER, "HEAD^{tree}")
        body, secrets = oid + "\n", [oid, oid[:8]]
    (folder / "x").write_text(body)
    return folder / "x", secrets


def _leaks_nothing(report, caplog, secrets: list[str]) -> None:
    everything = (*report.changes, *report.info)
    places = {"report": repr(report), "payload": json.dumps(refs.payload(report, 1, "m")),
              "log": "\n".join(caplog.handler.format(record) for record in caplog.records),
              "commands": "\n".join(change.restore for change in everything if change.restore)}
    for secret in secrets:
        for place, text in places.items():
            assert secret not in text, (place, secret)


def _typed(report, name: str) -> None:
    change = by_ref(report)[name]
    assert change.change.value == "unreadable" and change.restore is None
    assert change.to_json()["not_restorable"] == "ref_file_unreadable"


@pytest.mark.parametrize("kind", ["symref", "oid"])
@pytest.mark.parametrize("existed", [False, True])
def test_a_directory_swapped_for_a_link_while_the_walk_runs_shows_nothing_outside(repo, tmp_path, monkeypatch, caplog,
                                                                                  kind, existed):
    outside, secrets = _outside(tmp_path, repo, kind)
    folder = repo / ".git" / "refs" / "heads" / "dir"
    folder.mkdir()
    if existed:
        git(repo, "update-ref", "refs/heads/dir/x", "HEAD")
    real, swapped = os.scandir, []

    def listing(target):
        with real(target) as found:
            entries = list(found)
        if not swapped and any(entry.name == "dir" for entry in entries):  # listed as a directory, not yet opened
            shutil.rmtree(folder)
            folder.symlink_to(outside.parent)
            swapped.append(True)
        return contextlib.nullcontext(entries) if isinstance(target, int) else iter(entries)

    caplog.set_level("DEBUG")
    report = run(repo, lambda: monkeypatch.setattr(os, "scandir", listing))
    assert swapped
    _leaks_nothing(report, caplog, secrets)
    _typed(report, "refs/heads/dir")
    if existed:
        _typed(report, "refs/heads/dir/x")
    assert report.needs_you


@pytest.mark.parametrize("kind", ["symref", "oid"])
@pytest.mark.parametrize("name", ["refs/heads/other", "refs/tags/v1", "refs/heads/fresh"])
def test_a_ref_file_replaced_by_a_link_to_an_outside_file_shows_nothing_outside(repo, tmp_path, caplog, kind, name):
    outside, secrets = _outside(tmp_path, repo, kind)
    path = repo / ".git" / name

    def go():
        path.unlink(missing_ok=True)
        path.symlink_to(outside)

    caplog.set_level("DEBUG")
    report = run(repo, go)
    _leaks_nothing(report, caplog, secrets)
    _typed(report, name)
    assert report.needs_you


@pytest.mark.parametrize("kind", ["symref", "oid"])
@pytest.mark.parametrize("why", [None, "git_too_old"])
@pytest.mark.parametrize("through", ["refs/heads/mid", "refs/hidden/mid"])
def test_a_link_that_reads_through_a_linked_file_shows_nothing_outside(repo, tmp_path, monkeypatch, caplog, kind, why,
                                                                       through):
    outside, secrets = _outside(tmp_path, repo, kind)
    git(repo, "update-ref", through, "HEAD")
    git(repo, "symbolic-ref", "refs/heads/outer", through)
    if why:
        _without_direct_targets(monkeypatch, why)

    def go():
        (repo / ".git" / through).unlink()
        (repo / ".git" / through).symlink_to(outside)

    caplog.set_level("DEBUG")
    report = run(repo, go)
    _leaks_nothing(report, caplog, secrets)
    _typed(report, "refs/heads/outer")
    if through.startswith("refs/heads/"):
        _typed(report, through)


@pytest.mark.parametrize("kind", ["symref", "oid"])
def test_a_ref_file_linked_while_git_reads_shows_nothing_outside(repo, tmp_path, monkeypatch, caplog, kind):
    outside, secrets = _outside(tmp_path, repo, kind)
    path, real = repo / ".git" / "refs" / "heads" / "other", refs._git

    async def swapping(work_dir, *args):
        if args[0] == "for-each-ref" and not path.is_symlink():  # after Looper's first read of the files
            path.unlink()
            path.symlink_to(outside)
        return await real(work_dir, *args)

    caplog.set_level("DEBUG")
    report = run(repo, lambda: monkeypatch.setattr(refs, "_git", swapping))
    _leaks_nothing(report, caplog, secrets)
    _typed(report, "refs/heads/other")


@pytest.mark.parametrize("kind", ["symref", "oid"])
def test_a_hop_only_the_chain_before_the_turn_read_is_checked_before_it_is_read_again(repo, tmp_path, caplog, kind):
    outside, secrets = _outside(tmp_path, repo, kind)
    hidden = repo / ".git" / "refs" / "hidden" / "x"
    git(repo, "update-ref", "refs/hidden/x", "HEAD")
    git(repo, "symbolic-ref", "refs/heads/up", "refs/hidden/x")

    def go():
        git(repo, "symbolic-ref", "refs/heads/up", "refs/heads/other")
        hidden.unlink()
        hidden.symlink_to(outside)

    caplog.set_level("DEBUG")
    report = run(repo, go)
    _leaks_nothing(report, caplog, secrets)
    change = by_ref(report)["refs/heads/up"]
    assert change.restore is None and change.not_restorable.value == "ref_file_unreadable"


@pytest.mark.parametrize("kind", ["symref", "oid"])
def test_a_hop_linked_only_until_the_comparison_has_read_it_shows_nothing_outside(repo, tmp_path, monkeypatch, caplog,
                                                                                 kind):
    outside, secrets = _outside(tmp_path, repo, kind)
    hidden, real = repo / ".git" / "refs" / "hidden" / "x", refs._git
    git(repo, "update-ref", "refs/hidden/x", "HEAD")
    git(repo, "symbolic-ref", "refs/heads/up", "refs/hidden/x")
    body = hidden.read_text()

    async def restoring(work_dir, *args):
        answer = await real(work_dir, *args)
        if hidden.is_symlink() and args[-1] == "refs/hidden/x" and args[0] in ("symbolic-ref", "show-ref"):
            if args[0] == "show-ref" or answer[0] == 0:  # git has the value: the ordinary file comes back
                hidden.unlink()
                hidden.write_text(body)
        return answer

    async def go():
        before = await refs.snapshot(repo)
        git(repo, "symbolic-ref", "refs/heads/up", "refs/heads/other")
        after = await refs.snapshot(repo)  # no chain reads the hop now, so only the comparison does
        hidden.unlink()
        hidden.symlink_to(outside)
        monkeypatch.setattr(refs, "_git", restoring)
        return await refs.compare(repo, before, after, task_branch="main", task_id=TASK, active_task=inactive)

    caplog.set_level("DEBUG")
    report = asyncio.run(go())
    _leaks_nothing(report, caplog, secrets)
    change = by_ref(report)["refs/heads/up"]
    assert change.restore is None and change.not_restorable.value == "ref_file_unreadable"


def test_the_old_symlink_spelling_of_a_symbolic_ref_is_still_read(repo):
    (repo / ".git" / "refs" / "heads" / "old-style").symlink_to("refs/heads/other")
    found = asyncio.run(refs.snapshot(repo))
    assert found.hops == {"refs/heads/old-style": "refs/heads/other"} and not found.unreadable


def test_a_linked_worktree_reads_the_ref_files_of_the_common_directory(repo, tmp_path):
    tree = tmp_path / "tree"
    git(repo, "worktree", "add", "-q", str(tree), "other")
    git(repo, "symbolic-ref", "refs/heads/dang", "refs/heads/nothing")
    found = asyncio.run(refs.snapshot(tree))
    assert found.hops == {"refs/heads/dang": "refs/heads/nothing"} and not found.unreadable
    assert found.head == "refs/heads/other"


@pytest.mark.parametrize("name", ["packed-refs", "HEAD"])
def test_a_linked_packed_refs_or_head_refuses_the_whole_snapshot(repo, tmp_path, caplog, name):
    from looper.agents.tiers.task_manager import Refused
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    oid = git(repo, "rev-parse", "HEAD")
    (folder / name).write_text(f"{oid} refs/heads/{MARKER}\n" if name == "packed-refs"
                               else f"ref: refs/heads/{MARKER}\n")
    (repo / ".git" / name).unlink(missing_ok=True)
    (repo / ".git" / name).symlink_to(folder / name)
    caplog.set_level("DEBUG")
    with pytest.raises(Refused) as refused:
        asyncio.run(refs.snapshot(repo))
    assert MARKER not in str(refused.value) and MARKER not in caplog.text


def _spoil(repo: Path, tmp_path, monkeypatch, how: str) -> None:
    path = repo / ".git" / "refs" / "heads" / "odd"
    path.write_text("ref: refs/heads/nothing\n")
    if how == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif how == "too_large":
        path.write_text("ref: refs/heads/" + "a" * 5000)
    elif how == "hard_link":
        os.link(path, tmp_path / "second-name")
    elif how == "no_permission":
        path.chmod(0)
    else:  # gone, or a link by now, between the listing and the open
        real = os.scandir

        def listing(target):
            with real(target) as found:
                entries = list(found)
            if any(entry.name == "odd" for entry in entries) and path.is_file() and not path.is_symlink():
                path.unlink()
                if how == "link_race":
                    path.symlink_to(tmp_path / "elsewhere-file")
            return contextlib.nullcontext(entries)

        monkeypatch.setattr(os, "scandir", listing)


@pytest.mark.parametrize("how", ["fifo", "too_large", "hard_link", "no_permission", "vanished", "link_race"])
def test_every_read_failure_of_a_ref_file_is_a_typed_entry(repo, tmp_path, monkeypatch, how):
    if how == "no_permission" and os.geteuid() == 0:
        pytest.skip("root reads every file")

    async def go():
        before = await refs.snapshot(repo)
        _spoil(repo, tmp_path, monkeypatch, how)
        after = await asyncio.wait_for(refs.snapshot(repo), 20)
        return await refs.compare(repo, before, after, task_branch="main", task_id=TASK, active_task=inactive)

    report = asyncio.run(go())
    assert [c.ref for c in report.changes] == ["refs/heads/odd"] and report.needs_you
    _typed(report, "refs/heads/odd")


def test_a_linked_refs_directory_is_a_typed_entry_in_every_report(repo, tmp_path):
    heads = repo / ".git" / "refs" / "heads"
    shutil.move(heads, tmp_path / "heads")
    heads.symlink_to(tmp_path / "heads")
    report = run(repo, lambda: None)
    assert [c.ref for c in report.changes] == ["refs/heads"]
    _typed(report, "refs/heads")


def test_a_linked_refs_parent_is_not_skipped_because_of_what_it_points_to(repo, tmp_path):
    refs_dir = repo / ".git" / "refs"
    shutil.move(refs_dir, tmp_path / "refs")
    shutil.rmtree(tmp_path / "refs" / "tags")
    (tmp_path / "refs" / "tags").write_text("not a directory\n")  # looks like a reftable stub, through a link
    refs_dir.symlink_to(tmp_path / "refs")
    files = asyncio.run(refs._ref_files(repo))
    assert set(files.unreadable) == {"refs/heads", "refs/tags"} and files.links == {}


def test_a_reftable_repository_is_still_read(tmp_path):
    path = tmp_path / "table"
    made = subprocess.run(["git", "init", "-q", "-b", "main", "--ref-format=reftable", str(path)],
                          capture_output=True, timeout=30)
    if made.returncode:
        pytest.skip("this git cannot make a reftable repository")
    commit(path, "a.txt")
    report = run(path, lambda: git(path, "branch", "fresh"))
    assert [(c.ref, c.change) for c in report.changes] == [("refs/heads/fresh", Change.CREATED)]


# --- Chains Looper cannot know: the report says why instead of offering a command


def _without_direct_targets(monkeypatch, why: str) -> None:
    real = refs._git

    async def older(work_dir, *args):
        if args == ("version",):
            return 0, "git version 2.45.2\n"
        return await real(work_dir, *args)

    if why == "git_too_old":
        monkeypatch.setattr(refs, "_git", older)
    else:
        monkeypatch.setattr(refs, "SYMREF_LIMIT", 0)


@pytest.mark.parametrize("why", ["git_too_old", "too_many_symrefs"])
def test_without_direct_targets_a_symbolic_ref_is_listed_as_not_restorable(repo, monkeypatch, caplog, why):
    commit(repo, "b.txt")
    git(repo, "symbolic-ref", "refs/heads/inner", "refs/heads/other")
    git(repo, "symbolic-ref", "refs/heads/outer", "refs/heads/inner")
    _without_direct_targets(monkeypatch, why)
    with caplog.at_level("WARNING", logger=refs.__name__):
        found = asyncio.run(refs.snapshot(repo))
    assert found.hops == {} and len(found.symrefs) == 2 and found.gap.value == why and len(caplog.records) == 1

    def go():
        git(repo, "symbolic-ref", "refs/heads/inner", "refs/heads/main")
        git(repo, "symbolic-ref", "refs/heads/dang", "refs/heads/nothing")
        git(repo, "branch", "plain", "main")

    entries = by_ref(run(repo, go))
    for ref in ("refs/heads/inner", "refs/heads/outer", "refs/heads/dang"):
        assert entries[ref].restore is None and entries[ref].not_restorable.value == why
        assert entries[ref].to_json()["not_restorable"] == why
    plain = entries["refs/heads/plain"]  # a ref no chain ties keeps its command
    assert plain.not_restorable is None and "not_restorable" not in plain.to_json()
    assert _run(repo, plain.restore).returncode == 0


def test_a_chain_through_head_is_listed_as_not_restorable(repo):
    report = run(repo, lambda: git(repo, "symbolic-ref", "refs/heads/via", "HEAD"))
    change, = report.changes
    assert change.restore is None and change.not_restorable is refs.NotRestorable.CHAIN_OUTSIDE_REFS


def test_a_hop_git_cannot_read_is_listed_as_not_restorable(repo, monkeypatch):
    git(repo, "update-ref", "refs/hidden/x", "HEAD")
    real = refs._git

    async def failing(work_dir, *args):
        if args[0] == "symbolic-ref" and args[-1] == "refs/hidden/x":
            return 128, ""
        return await real(work_dir, *args)

    monkeypatch.setattr(refs, "_git", failing)
    report = run(repo, lambda: git(repo, "symbolic-ref", "refs/heads/up", "refs/hidden/x"))
    change, = report.changes
    assert change.restore is None and change.not_restorable is refs.NotRestorable.CHAIN_UNREADABLE


# --- Generated ref layouts: the listed commands succeed in order and put back every hop of every chain ---

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
        prefix = self.rng.choices(["refs/heads/", OTHER_TASK, "refs/tags/", "refs/hidden/"], [7, 3, 1, 1])[0]
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
            if chain[-1] != MAIN and len(chain) > 1 and self.rng.random() < 0.1:
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
        if self.reaches(target, name):
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
        every = sorted(self.refs)
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
            name, target = self.name(), rng.choice(every + ["refs/heads/nothing"])
            return f"create link {name} -> {target}" if self.link(name, target) else "none"
        if kind == "delete_link" and links:
            name = rng.choice(links)
            del self.refs[name]
            return f"delete link {name}"
        if kind == "retarget" and every:
            name, target = rng.choice(every), rng.choice(every + ["refs/heads/nothing"])
            return f"link {name} -> {target}" if self.link(name, target) else "none"
        if kind == "delete_target" and targets:
            name = rng.choice(targets)
            del self.refs[name]
            return f"delete {name}"
        if kind == "unlink" and links:
            name = rng.choice(links)
            self.refs[name] = commits[rng.choice(sorted(commits))]
            return f"direct {name}"
        if kind == "nest" and len(every) > 1:  # several names become a chain onto an end that moves
            end = rng.choice(direct) if direct else None
            if end is None:
                return "none"
            others = [n for n in every if n != end]
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


def _path(stored: dict, name: str) -> list[str]:
    """``name`` and every ref it reads through."""
    path = [name]
    while isinstance(stored.get(path[-1]), tuple):
        path.append(stored[path[-1]][1])
    return path


async def _generated_case(repo: Path, commits: dict[str, str], seed: int) -> tuple[int, int, int]:
    """One seeded case: every listed command succeeds in order, then every hop of every chain, before and after the
    turn, is compared. The answer: the number of commands, of hops compared, and of links compared."""
    layout = Layout(random.Random(seed), commits)
    layout.build()
    layout.write(repo)
    initial = _stored(repo)
    before = await refs.snapshot(repo)
    steps = [layout.mutate() for _ in range(layout.rng.randint(1, 5))]
    layout.write(repo)
    final = _stored(repo)
    after = await refs.snapshot(repo)
    report = await refs.compare(repo, before, after, task_branch="main", task_id=TASK, active_task=_only_task_two)
    entries = (*report.changes, *report.info)
    context = (f"seed={seed}\nbefore={initial}\nturn={steps}\nafter={final}\ncommits={commits}\n"
               "commands:\n  " + "\n  ".join(str(change.restore) for change in entries))
    assert all(change.restore for change in entries), context  # nothing here is beyond a command
    script, ran = [], set()
    for index, change in enumerate(entries):
        command = f"{{ {change.restore}; }} 2>/dev/null"
        script.append(f"if {command}; then echo 'repeat {index} applied'; exit 1; fi" if command in ran
                      else f"{command} || {{ echo 'command {index} exit '$?; exit 1; }}")
        ran.add(command)
    done = subprocess.run(["bash", "--noprofile", "--norc", "-c", "\n".join(script) or "true"], cwd=repo,
                          env=_shell_env(), capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, f"{done.stdout}{done.stderr}{context}"
    restored = _stored(repo)
    # The task branch keeps the commits the turn made on it: a fast-forward is the Manager's own work.
    descends = {(commits["base"], commits["tip"]), (commits["base"], commits["next"]),
                (commits["tip"], commits["next"]), (commits["base"], commits["side"])}
    expected = dict(initial)
    old, end = initial.get(MAIN), final.get(_path(final, MAIN)[-1])
    if isinstance(old, str) and (old, end) in descends:
        expected[MAIN] = end
    # Every compared name before or after, and every hop of every chain either snapshot of the turn had: the hops of
    # the chains before the turn hold their old values again. A ref outside branches and tags that only chains after
    # the turn read has no recorded earlier value: it must be as the turn left it.
    names = {name for stored in (initial, final) for ref in stored if ref.startswith(("refs/heads/", "refs/tags/"))
             for name in _path(stored, ref)}
    read_before = {name for ref in initial if ref.startswith(("refs/heads/", "refs/tags/"))
                   for name in _path(initial, ref)}
    wrong = []
    for name in sorted(names):
        compared = name.startswith(("refs/heads/", "refs/tags/"))
        want = expected.get(name) if compared or name in read_before else final.get(name)
        if restored.get(name) != want:
            wrong.append(f"{name}: want {want}, got {restored.get(name)}")
    assert not wrong, "\n".join(wrong) + "\n" + context
    return len(entries), len(names), sum(isinstance(initial.get(n) or final.get(n), tuple) for n in names)


async def _only_task_two(task_id: str) -> bool:
    return task_id == "task-two"


@pytest.mark.parametrize("first", range(0, CASES, CHUNK))
def test_the_listed_commands_restore_every_hop_of_generated_ref_layouts(tmp_path, first):
    repo = tmp_path / "generated"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commits = _history(repo)

    async def go():
        return [await _generated_case(repo, commits, seed) for seed in range(first, first + CHUNK)]

    outcomes = asyncio.run(go())
    print(f"seeds {first}-{first + CHUNK - 1}: {len(outcomes)} layouts compared, "
          f"{sum(o[1] for o in outcomes)} refs, {sum(o[2] for o in outcomes)} of them links")
    # The generator must keep exercising the restore: several commands in many layouts, and chains in most.
    assert sum(o[0] >= 2 for o in outcomes) >= CHUNK // 2 and sum(o[2] >= 2 for o in outcomes) >= CHUNK // 2
