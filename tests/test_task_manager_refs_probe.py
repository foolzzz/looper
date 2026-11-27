"""Opt-in, offline real-CLI probes of the ref snapshot (``task_manager_refs``) against a sandboxed in-place session.

Run with ``LOOPER_CLAUDE_PROBE_BIN`` set to a real Claude Code binary (never a terminal shim); no real model is called:
the CLI talks to a local scripted Messages API with a scratch ``HOME`` and ``CLAUDE_CONFIG_DIR``, and the command under
test is the real ``CLAUDE_CODE.operator_resident_command``. Each row scripts Bash calls in the sandbox, with a snapshot
before and ``compare`` after. A skip is not a pass. Verified against Claude Code 2.1.332.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path

import pytest

from looper.agents import git_safe, task_manager_refs as refs
from looper.agents.tiers.task_manager import RefuseReason, Refused
from sandbox_probe_support import SandboxProbe, text, tool_use

pytestmark = pytest.mark.installed
TASK = "probe-task"
IDENTITY = "-c user.name=Probe -c user.email=probe@example.invalid -c commit.gpgsign=false"


@pytest.fixture
def probe(tmp_path, request):
    binary = os.environ.get("LOOPER_CLAUDE_PROBE_BIN")
    if not binary:
        pytest.skip("Set LOOPER_CLAUDE_PROBE_BIN to opt into the offline real-CLI probe; a skip is not a pass")
    assert not str(tmp_path.resolve()).startswith(("/private/tmp/", "/tmp/")), "use a --basetemp outside /tmp"
    instance = SandboxProbe(tmp_path, str(Path(binary).resolve()))
    # Two commits, a tag, and a second branch: what the rows below move, delete and reset.
    (instance.cwd / "b.txt").write_text("b\n")
    instance.git(instance.cwd, "add", "b.txt")
    instance.git(instance.cwd, "commit", "-qm", "Second probe commit")
    instance.git(instance.cwd, "tag", "v1")
    instance.git(instance.cwd, "branch", "other")
    try:
        yield instance
    finally:
        instance.close()
        (tmp_path / "evidence.json").write_text(json.dumps({"check": request.node.name, **instance.evidence()}, indent=2))
        print(f"Evidence: {tmp_path / 'evidence.json'}")


async def _inactive(_: str) -> bool:
    return False


def turn(probe: SandboxProbe, *commands: str):
    """One sandboxed run of ``commands`` (Bash tool calls) between a snapshot and a comparison of the task directory."""
    branch = probe.git(probe.cwd, "symbolic-ref", "--short", "HEAD").strip()
    before = asyncio.run(refs.snapshot(probe.cwd))
    script = [tool_use("Bash", {"command": command}) for command in commands] + [text("done")]
    run = probe.run(script)
    after = asyncio.run(refs.snapshot(probe.cwd))
    report = asyncio.run(refs.compare(probe.cwd, before, after, task_branch=branch, task_id=TASK, active_task=_inactive))
    results = [run.result_of(call["id"]) for call in script if call["type"] == "tool_use"]
    return before, after, report, results, branch


def test_g1_a_commit_on_the_task_branch_is_not_a_change_and_counts_as_the_managers_own(probe):
    _, _, report, results, _ = turn(probe, f"echo x > c.txt && git add c.txt && git {IDENTITY} commit -qm own && echo rc=$?")
    assert "rc=0" in results[0], results
    assert report.changes == () and report.info == () and not report.needs_you and report.own_commits == 1


def test_g2_update_ref_in_the_sandbox_is_reported_and_its_restore_command_undoes_it(probe):
    before, _, report, results, _ = turn(probe, "git update-ref refs/heads/other HEAD~1 && echo rc=$?")
    assert "rc=0" in results[0], results  # the sandbox lets the model write refs in the real .git
    change, = report.changes
    assert change.ref == "refs/heads/other" and change.change is refs.Change.MOVED and report.needs_you
    args = shlex.split(change.restore)
    git_safe.run(probe.cwd, *args[1:], check=True, capture_output=True)  # outside the sandbox, through git_safe
    assert asyncio.run(refs.snapshot(probe.cwd)).refs == before.refs
    probe.observations["g2"] = {"restore": change.restore}


def test_g3_a_new_tag_and_a_deleted_tag_are_reported(probe):
    _, _, report, results, _ = turn(probe, "git tag v9 && git tag -d v1 && echo rc=$?")
    assert "rc=0" in results[0], results
    assert {c.ref: c.change for c in report.changes} == {"refs/tags/v9": refs.Change.CREATED,
                                                          "refs/tags/v1": refs.Change.DELETED}
    assert report.needs_you


def test_g4_a_checkout_is_a_head_change_and_the_next_turn_is_refused(probe, monkeypatch):
    branches: dict[str, str] = {}

    async def get_branch(task_id):
        return branches.get(task_id)

    async def record(task_id, branch):
        return branches.setdefault(task_id, branch) == branch

    monkeypatch.setattr(refs.store, "get_branch", get_branch)
    monkeypatch.setattr(refs.store, "record_first_branch", record)

    class Quiet:
        async def event(self, *_):
            return None

    start = asyncio.run(refs.begin_turn({"id": TASK}, probe.cwd, sink=Quiet()))
    assert start[1] == branches[TASK]
    _, _, report, results, _ = turn(probe, "git checkout other && echo rc=$?")
    assert "rc=0" in results[0], results
    assert report.head_changed and report.head_after == "refs/heads/other" and report.changes == ()
    with pytest.raises(Refused) as refused:
        asyncio.run(refs.begin_turn({"id": TASK}, probe.cwd, sink=Quiet()))
    assert refused.value.reason is RefuseReason.HEAD_MOVED


def test_g5_a_hard_reset_of_the_task_branch_is_non_fast_forward(probe):
    before, _, report, results, branch = turn(probe, "git reset --hard HEAD~1 && echo rc=$?")
    assert "rc=0" in results[0], results
    change, = report.changes
    assert change.ref == f"refs/heads/{branch}" and change.change is refs.Change.NON_FAST_FORWARD
    assert not report.needs_you and change.old == before.refs[change.ref]


def test_g6_gc_and_removing_the_reflogs_in_the_sandbox_are_no_change(probe):
    _, _, report, results, _ = turn(probe, "git gc -q --prune=now; echo gc=$?", "rm -rf .git/logs; echo rm=$?")
    assert "gc=0" in results[0] and "rm=0" in results[1], results
    assert not (probe.cwd / ".git" / "logs").exists()  # the positive control: the removal really happened
    assert not report.worth_saying, report


def test_g7_a_fetch_from_a_local_bare_repository_changes_only_unread_namespaces(probe, tmp_path):
    bare = probe.root / "remote.git"
    probe.git(probe.root, "init", "-q", "--bare", str(bare))
    probe.git(probe.cwd, "remote", "add", "origin", str(bare))
    probe.git(probe.cwd, "push", "-q", "origin", "HEAD")
    probe.git(probe.cwd, "fetch", "-q", "origin")
    other = probe.root / "other-clone"
    probe.git(probe.root, "clone", "-q", str(bare), str(other))
    (other / "n.txt").write_text("n\n")
    probe.git(other, "add", "n.txt")
    probe.git(other, "commit", "-qm", "Upstream commit")
    probe.git(other, "push", "-q", "origin", "HEAD")
    remote_before = probe.git(probe.cwd, "for-each-ref", "refs/remotes")
    _, _, report, results, _ = turn(probe, "git fetch origin && echo rc=$?")
    assert "rc=0" in results[0], results
    assert probe.git(probe.cwd, "for-each-ref", "refs/remotes") != remote_before  # refs/remotes did change ...
    assert not report.worth_saying, report  # ... and is not read
