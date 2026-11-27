"""Branch and tag changes of a direct session's turn: recorded outside the task directory, reported, never restored.

In an in-place direct session the model's Bash writes the real checkout, ``.git`` included, so a turn can move a
branch, delete a tag or switch ``HEAD``. Looper cannot prevent that inside the sandbox (RFC 25.5), so it records
``refs/heads`` and ``refs/tags`` and ``HEAD`` before every turn, in memory (the sandbox cannot reach it), compares them
after, and says in the task conversation what changed, with one restore command per ref. It never restores anything:
the operator's checkout is the operator's. A change to a branch or tag other than the task branch also goes to "needs
you"; it is not an invariant violation and raises no alert (RFC 21, 25.10).

The task branch is recorded once, at the first enablement. A turn refuses to start while ``HEAD`` is not on it.
Every git call goes through ``git_safe`` (no repository code runs); ``refs/remotes`` is never read.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import re
import shlex
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git_safe, task_manager_safety
from ..db import task_manager_refs as store

if TYPE_CHECKING:
    from .tiers.task_manager import TurnSink

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SEC = 30
_LOCKLESS = {"GIT_OPTIONAL_LOCKS": "0"}
HEADS, TAGS, LOOPER = "refs/heads/", "refs/tags/", "refs/heads/looper/"
FINISHED_STATUSES = frozenset({"completed", "failed", "cancelled", "stopped"})
FIRST_ENABLE_NOTICE = "first_enable_in_place"
REFS_UNREADABLE_NOTICE = "refs_unreadable"
NOTICES = (FIRST_ENABLE_NOTICE, REFS_UNREADABLE_NOTICE)
REFS_UNREADABLE_DETAIL = ("The branches and tags could not be read again after this turn, so any change this turn made "
                          "to them is not reported. Check them yourself.")
# What ``str.splitlines`` splits on (besides \n).
_LINE_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
WALK_LIMIT = 20000  # commits read when deciding fast-forward
OBJECT_LIMIT = 1 << 20  # a commit object larger than this ends the walk undecided
SYMREF_LIMIT = 200  # symbolic refs asked for their direct target (one git process each) in one snapshot
_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _odd(ch: str) -> bool:
    """A control or format character (bidirectional controls, zero-width characters): invisible or reordering."""
    return unicodedata.category(ch) in ("Cc", "Cf")
FIRST_ENABLE_DETAIL = ("The task Manager edits files directly in your real checkout. v1 only reports changes to "
                       "branches and tags; it never restores them, and deleted objects cannot be recovered. Push or "
                       "back up important branches first.")


class Change(str, Enum):
    CREATED = "created"
    DELETED = "deleted"
    MOVED = "moved"
    NON_FAST_FORWARD = "non_fast_forward"
    FAST_FORWARD_UNVERIFIED = "fast_forward_unverified"  # the bounded walk gave up: neither a reset nor a fast-forward is claimed


class Ancestry(Enum):
    DESCENDS = "descends"
    DIVERGES = "diverges"
    UNDECIDED = "undecided"


@dataclass(frozen=True)
class Snapshot:
    refs: Mapping[str, str]
    head: str | None  # the full name of the branch HEAD is on; None when detached
    symrefs: Mapping[str, str] = field(default_factory=dict)  # ref -> the ref its chain ends at, for symbolic refs
    hops: Mapping[str, str] = field(default_factory=dict)  # ref -> the ref it points to directly, for symbolic refs


@dataclass(frozen=True)
class RefChange:
    ref: str
    change: Change
    old: str | None
    new: str | None
    restore: str

    def to_json(self) -> dict[str, Any]:
        return {"ref": self.ref, "change": self.change.value, "old": self.old, "new": self.new,
                "restore": self.restore}


@dataclass(frozen=True)
class Report:
    changes: tuple[RefChange, ...]
    info: tuple[RefChange, ...]
    head_before: str | None
    head_after: str | None
    own_commits: int
    needs_you: bool

    @property
    def head_changed(self) -> bool:
        return self.head_before != self.head_after

    @property
    def worth_saying(self) -> bool:
        return bool(self.changes or self.info or self.head_changed or self.own_commits)


def display(name: str | None) -> str | None:
    """A ref name for the conversation: control and format characters (bidirectional controls, zero-width
    characters) written as ``\\uXXXX``."""
    if name is None:
        return None
    return "".join(f"\\u{ord(ch):04x}" if _odd(ch) else ch for ch in name)


def _quote(name: str) -> str:
    """``name`` as one shell word; control and format characters are spelled out in a ``$'...'`` word."""
    if not any(_odd(ch) for ch in name):
        return shlex.quote(name)
    # UTF-8 bytes as \\xHH: the one spelling every POSIX shell (bash 3.2 included) reads inside $'...'.
    body = "".join("".join(f"\\x{byte:02x}" for byte in ch.encode()) if _odd(ch)
                   else ("\\" + ch if ch in "'\\" else ch) for ch in name)
    return f"$'{body}'"


def _restore(ref: str, change: Change, old: str | None, new: str | None,
             old_target: str | None = None, new_target: str | None = None, linked: str | None = None,
             direct: str | None = None) -> str:
    name = _quote(ref)
    if direct is not None:  # the task branch became a link: a direct ref on ``direct`` again, if it still reads ``new``
        return f"git update-ref --no-deref {name} {direct} {new}"
    if linked is not None:  # the link is intact: what moved, and what to move back, is its target
        # --no-deref like every other restore: the name was a direct ref when this was written (%(symref) peels to
        # the end of the chain); if it has become a symbolic ref since, only that name changes.
        return f"git update-ref --no-deref {_quote(linked)} {old} {new}"
    if old_target is not None:  # a symbolic ref goes back to what it pointed at
        return f"git symbolic-ref {name} {_quote(old_target)}"
    if new_target is not None:
        return (f"git symbolic-ref --delete {name}" if old is None
                else f"git update-ref --no-deref {name} {old} {new}")
    if change is Change.CREATED:
        return f"git update-ref --no-deref -d {name} {new}"
    if change is Change.DELETED:
        return f"git update-ref --no-deref {name} {old}"
    return f"git update-ref --no-deref {name} {old} {new}"


def _change(ref: str, old: str | None, new: str | None, *, plain_move: bool = False, unverified: bool = False,
            old_target: str | None = None, new_target: str | None = None, linked: str | None = None,
            direct: str | None = None) -> RefChange:
    kind = (Change.CREATED if old is None else Change.DELETED if new is None
            else Change.MOVED if plain_move else Change.FAST_FORWARD_UNVERIFIED if unverified
            else Change.NON_FAST_FORWARD)
    return RefChange(display(ref), kind, old, new,
                     _restore(ref, kind, old, new, old_target, new_target, linked, direct))


def _ordered(changes: list[tuple[str, RefChange]], info: list[tuple[str, RefChange]],
             after: Snapshot) -> tuple[tuple[RefChange, ...], tuple[RefChange, ...]]:
    """``changes`` and ``info`` (each entry with the ref its command writes) as they are listed, which is the order the
    commands run in: ``changes`` first.

    A command's current-value check reads through whatever its ref points to at the end of the turn, so it has to run
    before any command that writes a ref further down that chain: outer link before inner link, every link before the
    delete or move of its target. Otherwise the given order stays. An ``info`` entry that a change has to wait for is
    listed among the changes. Links that form a cycle give no order: both lists then stay as given."""
    below: dict[str, set[str]] = {}
    for targets in (after.hops, after.symrefs):  # the chain's end too: a hop outside the compared refs is not read
        for ref, target in targets.items():
            below.setdefault(ref, set()).add(target)
    entries = changes + info
    written: dict[str, list[int]] = {}
    for index, (ref, _) in enumerate(entries):
        written.setdefault(ref, []).append(index)
    later: list[set[int]] = []  # per entry, the entries whose commands have to run after its own
    for ref, _ in entries:
        seen, queue = {ref}, [ref]
        while queue:
            for target in below.get(queue.pop(), set()) - seen:
                seen.add(target)
                queue.append(target)
        later.append({index for target in seen - {ref} for index in written.get(target, ())})
    waiting = [0] * len(entries)
    for followers in later:
        for index in followers:
            waiting[index] += 1
    ready = [index for index, count in enumerate(waiting) if not count]  # ascending, so already a heap
    order: list[int] = []
    while ready:
        order.append(heapq.heappop(ready))
        for follower in later[order[-1]]:
            waiting[follower] -= 1
            if not waiting[follower]:
                heapq.heappush(ready, follower)
    if len(order) != len(entries):
        logger.warning("task Manager refs: the symbolic refs form a cycle; the restore commands keep the order by name")
        return tuple(c for _, c in changes), tuple(c for _, c in info)
    changed = set(range(len(changes)))
    listed = changed | {index for index in range(len(entries)) if later[index] & changed}
    return (tuple(entries[i][1] for i in order if i in listed),
            tuple(entries[i][1] for i in order if i not in listed))


def _refused(exc: git_safe.RepositoryRefused):
    from .tiers.task_manager import RefuseReason, Refused

    return Refused(RefuseReason.WORKSPACE, str(exc))


async def _git(work_dir: str | Path, *args: str) -> tuple[int, str]:
    """``git args`` in ``work_dir`` through ``git_safe``: the exit code and stdout."""
    try:
        process = await git_safe.spawn(work_dir, *args, extra_env=_LOCKLESS, stdin=asyncio.subprocess.DEVNULL,
                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except git_safe.RepositoryRefused as exc:
        raise _refused(exc) from None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), GIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"git {args[0]} timed out reading the task's refs") from None
    try:
        await git_safe.recheck_async(process)
    except git_safe.RepositoryRefused as exc:
        raise _refused(exc) from None
    return process.returncode, stdout.decode("utf-8", "surrogateescape")


def _unreadable(detail: str):
    from .tiers.task_manager import RefuseReason, Refused

    return Refused(RefuseReason.WORKSPACE, detail)


async def _direct_targets(work_dir: str | Path, symrefs: Mapping[str, str]) -> dict[str, str]:
    """The one-hop target of each symbolic ref (``%(symref)`` and a plain ``symbolic-ref`` both follow the whole
    chain). It only orders the restore commands, so when it cannot be had (more than ``SYMREF_LIMIT`` symbolic refs,
    a git without ``--no-recurse``) the snapshot goes without it and says so in the log."""
    if len(symrefs) > SYMREF_LIMIT:
        logger.warning("task Manager refs: %d symbolic refs, over the limit of %d; their direct targets are not read",
                       len(symrefs), SYMREF_LIMIT)
        return {}
    hops: dict[str, str] = {}
    for ref in symrefs:
        code, target = await _git(work_dir, "symbolic-ref", "-q", "--no-recurse", ref)
        if code:
            logger.warning("task Manager refs: git could not read a direct symbolic-ref target (exit %d); restore "
                           "commands are ordered without direct targets", code)
            return {}
        hops[ref] = target.removesuffix("\n")
    return hops


async def snapshot(work_dir: str | Path) -> Snapshot:
    """The logical value of every branch and tag (and the targets of a symbolic ref), and where ``HEAD`` points.
    Packed refs, ``gc``, reflogs and index changes do not show in these values.

    A listing that cannot be told apart from another (a ref name holding a character ``str.splitlines`` breaks
    lines on, or two records of one name) is a read failure, never a last-write-wins value."""
    code, listing = await _git(work_dir, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)",
                               "refs/heads", "refs/tags")
    if code:
        raise _unreadable(f"{work_dir} is not a git repository Looper can read (git exit {code}).")
    refs: dict[str, str] = {}
    symrefs: dict[str, str] = {}
    for line in listing.split("\n"):
        if not line:
            continue
        parts = line.split("\0")
        if len(parts) != 3 or not parts[0] or not parts[1]:
            raise _unreadable("The branch and tag listing has a record Looper cannot read.")
        ref, sha, target = parts
        if _LINE_BREAKS & set(ref) or ref in refs:
            raise _unreadable("A branch or tag name holds a line-break character or appears twice; "
                              "Looper cannot read the refs safely.")
        refs[ref] = sha
        if target:
            symrefs[ref] = target
    code, head = await _git(work_dir, "symbolic-ref", "-q", "HEAD")
    # Only the trailing newline goes: ref names may end in (or hold) Unicode whitespace.
    head = head.removesuffix("\n") if code == 0 else ""
    return Snapshot(refs, head if head.startswith(HEADS) else None, symrefs,
                    await _direct_targets(work_dir, symrefs))


async def branch_problem(work_dir: str | Path, branch: str) -> str | None:
    """Why ``branch`` cannot be the task branch (git's own branch-name check; a leading ``-`` would read as an
    option), or None."""
    if branch.startswith("-"):
        return "a branch name that starts with '-' cannot be used as the task branch"
    code, _ = await _git(work_dir, "check-ref-format", "--branch", branch)
    return None if code == 0 else "git does not accept this as a branch name"


def _parents(body: bytes) -> list[str] | None:
    """The parents of a raw commit object, read the way git does: the first line is ``tree <oid>`` and the
    ``parent <oid>`` lines are the consecutive ones right after it. None when the header is not that shape."""
    lines = body.split(b"\n\n", 1)[0].decode("utf-8", "replace").split("\n")
    if not lines or not re.fullmatch(r"tree " + _OID.pattern, lines[0]):
        return None
    parents = []
    for line in lines[1:]:
        if not line.startswith("parent "):
            break
        oid = line[len("parent "):]
        if not _OID.fullmatch(oid):
            return None
        parents.append(oid)
    return parents


async def _reaches(work_dir: str | Path, new: str, old: str) -> Ancestry:
    """Whether ``old`` is an ancestor of ``new``, from the raw commit objects (``cat-file --batch`` honours neither
    ``info/grafts`` nor replace refs), breadth first so a merge finds ``old`` among its first parents.
    ``UNDECIDED`` when the walk gave up (a limit, an object that is not a well-formed commit, a timeout)."""
    process = await git_safe.spawn(work_dir, "-c", "core.useReplaceRefs=false", "cat-file", "--batch",
                                   extra_env=_LOCKLESS, stdin=asyncio.subprocess.PIPE,
                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    found = Ancestry.UNDECIDED

    async def read() -> Ancestry:
        seen: set[str] = set()
        queue = deque([new])
        while queue:
            sha = queue.popleft()
            if sha == old:
                return Ancestry.DESCENDS
            if sha in seen:
                continue
            if len(seen) >= WALK_LIMIT:
                return Ancestry.UNDECIDED
            seen.add(sha)
            process.stdin.write(sha.encode() + b"\n")
            await process.stdin.drain()
            header = (await process.stdout.readline()).decode("ascii", "replace").split()
            if len(header) != 3 or header[1] != "commit" or not header[2].isdigit():
                return Ancestry.UNDECIDED
            size = int(header[2])
            if size > OBJECT_LIMIT:
                return Ancestry.UNDECIDED
            parents = _parents(await process.stdout.readexactly(size + 1))
            if parents is None:
                return Ancestry.UNDECIDED
            queue.extend(parents)
        return Ancestry.DIVERGES

    try:
        found = await asyncio.wait_for(read(), GIT_TIMEOUT_SEC)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
        found = Ancestry.UNDECIDED
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass
        process.kill()
        await process.wait()
    return found


async def fast_forward(work_dir: str | Path, old: str, new: str) -> tuple[Ancestry, int]:
    """Whether ``new`` descends from ``old`` and, when it does, the number of commits ``old..new``. The decision
    reads raw parents, so a graft or a replace ref cannot make a reset look like an ancestor; only the count comes
    from ``rev-list`` and a graft could skew that number, never the decision. ``UNDECIDED`` when it cannot be
    decided: that is reported as unverified, never as a reset and never as a fast-forward."""
    try:
        verdict = await _reaches(work_dir, new, old)
        if verdict is not Ancestry.DESCENDS:
            return verdict, 0
        code, count = await _git(work_dir, "-c", "core.useReplaceRefs=false", "rev-list", "--count", f"{old}..{new}")
        return Ancestry.DESCENDS, (int(count.strip()) if code == 0 and count.strip().isdigit() else 0)
    except Exception:
        logger.warning("task Manager refs: could not decide fast-forward", exc_info=True)
        return Ancestry.UNDECIDED, 0


def _short(ref: str | None) -> str:
    return "detached" if ref is None else display(ref.removeprefix(HEADS))


async def begin_turn(task: Mapping[str, Any], work_dir: str | Path, *, sink: TurnSink,
                     phase: str = "turn") -> tuple[Snapshot, str]:
    """Record the refs, and settle the task branch: record it at the first enablement, refuse when ``HEAD`` is not on
    it. Returns the snapshot (kept in memory by the running turn) and the task branch."""
    from .tiers.task_manager import RefuseReason, Refused, TaskManagerEvent

    task_id = str(task["id"])
    before = await snapshot(work_dir)
    recorded = await store.get_branch(task_id)
    if recorded is None:
        if before.head is None:
            task_manager_safety.record_refusal(task_manager_safety.Reason.HEAD_DETACHED, phase)
            raise Refused(RefuseReason.HEAD_DETACHED,
                          "任务目录的 HEAD 是分离的（detached）。先检出一个分支再发消息，管家会把这个分支记为任务分支。")
        branch = before.head.removeprefix(HEADS)
        if (problem := await branch_problem(work_dir, branch)) is not None:
            raise Refused(RefuseReason.WORKSPACE, f"任务分支 {display(branch)} 不能记录：{problem}。换一个分支名再发消息。")
        if await store.record_first_branch(task_id, branch):
            await sink.event(TaskManagerEvent.SANDBOX_NOTICE, {"notice": FIRST_ENABLE_NOTICE, "detail": FIRST_ENABLE_DETAIL})
        recorded = await store.get_branch(task_id) or branch
    if before.head != HEADS + recorded:
        task_manager_safety.record_refusal(task_manager_safety.Reason.HEAD_MOVED, phase)
        raise Refused(RefuseReason.HEAD_MOVED,
                      f"任务分支记录的是 {display(recorded)}，目录现在在 {_short(before.head)}。"
                      f"把 {display(recorded)} 重新检出（git switch -- {_quote(recorded)}），"
                      "或者用 tasks.task_manager_branch_set 把当前分支记为任务分支，再发消息。")
    return before, recorded


async def _active_task(task_id: str) -> bool:
    from ..db import tasks

    row = await tasks.get_task(task_id)
    return row is not None and row["status"] not in FINISHED_STATUSES


async def compare(work_dir: str | Path, before: Snapshot, after: Snapshot, *, task_branch: str, task_id: str,
                  active_task: Callable[[str], Awaitable[bool]] = _active_task) -> Report:
    """What changed between two snapshots, with no side effect."""
    # Each entry with the ref its command writes (an intact link's command writes the link's target).
    changes: list[tuple[str, RefChange]] = []
    info: list[tuple[str, RefChange]] = []
    task_ref, own_commits, needs_you = HEADS + task_branch, 0, False
    for ref in sorted(before.refs.keys() | after.refs.keys()):
        old, new = before.refs.get(ref), after.refs.get(ref)
        old_target, new_target = before.symrefs.get(ref), after.symrefs.get(ref)
        if old == new and old_target == new_target:
            continue
        pointers = {"old_target": old_target, "new_target": new_target}
        compared = old_target in before.refs or old_target in after.refs
        if old_target is not None and old_target == new_target:
            # An intact link follows its target, and the target's own change is reported under its own name, but
            # only when the target is itself compared (``refs/heads``, ``refs/tags``). A target outside them
            # (``refs/remotes/*`` is deliberately never read, RFC 25.4; anything else is not listed) would
            # otherwise hide the link's value change, so it is reported: erring toward reporting. The task branch
            # is judged below either way; undoing what it reads through an intact link means moving the target.
            if compared and ref != task_ref:
                continue
            pointers = {"linked": old_target}
        if ref == task_ref:
            # Ancestry is decided from the two commits whatever the task branch is linked to.
            verdict, count = ((await fast_forward(work_dir, old, new))
                              if old is not None and new is not None else (Ancestry.DIVERGES, 0))
            retargeted = old_target != new_target
            if retargeted and new_target is not None and old is not None:
                # Its commits stay when they descend; otherwise it goes back to where it was.
                pointers["direct"] = new if verdict is Ancestry.DESCENDS else old
            if verdict is Ancestry.DESCENDS:
                own_commits = count
                # A changed link is always its own report; an intact one only into a namespace not compared.
                if not (retargeted or (old_target is not None and not compared)):
                    continue
            # Ahead of the other entries: its command may have to run before one that deletes or moves its target.
            changes.insert(0, (pointers.get("linked", ref), _change(
                ref, old, new, plain_move=verdict is Ancestry.DESCENDS, unverified=verdict is Ancestry.UNDECIDED,
                **pointers)))
            continue
        entry = (pointers.get("linked", ref), _change(ref, old, new, plain_move=True, **pointers))
        owner = ref.removeprefix(LOOPER).split("/", 1) if ref.startswith(LOOPER) else None
        if owner and len(owner) == 2 and owner[0] != task_id and await active_task(owner[0]):
            info.append(entry)
            continue
        changes.append(entry)
        needs_you = True
    # The order to start from: names that became or changed as links first, the task branch first among them.
    # ``_ordered`` then moves only what a link's current-value check needs. Stable.
    links = {display(ref) for ref, target in after.symrefs.items() if before.symrefs.get(ref) != target}
    changes.sort(key=lambda entry: entry[1].ref not in links)
    return Report(*_ordered(changes, info, after), before.head, after.head, own_commits, needs_you)


def payload(report: Report, report_id: int | None, message_id: str | None = None) -> dict[str, Any]:
    """The ``ref_report`` event; ``message_id`` (the turn's) keeps two otherwise identical reports apart."""
    return {"message_id": message_id, "report_id": report_id, "changes": [c.to_json() for c in report.changes],
            "info": [c.to_json() for c in report.info],
            "head": ({"before": display(report.head_before), "after": display(report.head_after)}
                     if report.head_changed else None),
            "own_commits": report.own_commits, "needs_you": report.needs_you}


async def end_turn(task: Mapping[str, Any], work_dir: str | Path, before: Snapshot, branch: str, *, sink: TurnSink,
                   message_id: str | None = None) -> Report:
    """Compare now with ``before`` and say what changed. Nothing is restored and no alert is raised."""
    from .tiers.task_manager import TaskManagerEvent

    task_id = str(task["id"])
    try:
        report = await compare(work_dir, before, await snapshot(work_dir), task_branch=branch, task_id=task_id)
    except Exception:
        await sink.event(TaskManagerEvent.SANDBOX_NOTICE, {"notice": REFS_UNREADABLE_NOTICE,
                                                          "detail": REFS_UNREADABLE_DETAIL, "message_id": message_id})
        raise
    if not report.worth_saying:
        return report
    report_id = None
    if report.changes or report.info:
        body = payload(report, None, message_id)
        report_id = await store.insert_report(
            task_id, message_id, {key: body[key] for key in ("changes", "info", "head", "own_commits")},
            report.needs_you)
    await sink.event(TaskManagerEvent.REF_REPORT, payload(report, report_id, message_id))
    return report
