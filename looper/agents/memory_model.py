"""The shared vocabulary of memory candidates (memory-write-path RFC, sections 3–5 and 9; memory-tiers RFC, section 4.1):
their kinds, states, reasons and events, how their ids are shown and read, which source classes each kind accepts,
the transitions code may make, the content hash a confirmation binds to, the fingerprint, and the ``memory_write``
setting.

Every part of the feature imports these names; none defines its own copy. A ``looper`` row (a lesson in effect in
PostgreSQL only) has its states and reasons here, but no transition: nothing writes one yet.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

SETTING_KEY = "memory_write"
STATEMENT_MAX_CHARS = WHY_MAX_CHARS = 300
KEYWORDS_MAX = 6
KEYWORD_MAX_CHARS = 40
TITLE_MAX_CHARS = 60
NOTE_MAX_CHARS = 300
QUOTE_MAX_CHARS = 500
LINE_MAX_CHARS = 1_000
CONTENT_MAX_BYTES = 16 * 1024
CHANGES_MAX = 3


class Kind(StrEnum):
    PREFERENCE = "preference"
    FEEDBACK = "feedback"
    DECISION = "decision"
    LESSON = "lesson"
    FACT = "fact"
    COMMITMENT = "commitment"
    PERSON = "person"
    PROJECT = "project"


class SourceKind(StrEnum):
    """Where a candidate came from first (``memory_candidates.source_kind``)."""

    MANAGER_TURN = "manager_turn"
    TASK = "task"
    LEAD = "lead"


class EvidenceKind(StrEnum):
    """One source of a candidate (``memory_candidate_sources.kind``)."""

    MANAGER_TURN = "manager_turn"
    JOURNAL = "journal"
    LEAD = "lead"


class SourceClass(StrEnum):
    OPERATOR = "operator"  # the operator's own Manager turn, its quote checked by code
    AGENT = "agent"  # a task's journal
    EXTERNAL = "external"  # messages (a lead)
    SYSTEM = "system"  # Looper's own code; nothing produces one yet


# Most trusted first; a candidate's class is the least trusted of its sources' (``least_trusted``).
TRUST_ORDER = (SourceClass.OPERATOR, SourceClass.SYSTEM, SourceClass.AGENT, SourceClass.EXTERNAL)
# The class of each kind of source, as the schema's generated column computes it.
EVIDENCE_CLASS: Mapping[EvidenceKind, SourceClass] = {
    EvidenceKind.MANAGER_TURN: SourceClass.OPERATOR,
    EvidenceKind.JOURNAL: SourceClass.AGENT,
    EvidenceKind.LEAD: SourceClass.EXTERNAL,
}
# The first source's kind as the candidate records it.
SOURCE_KIND: Mapping[EvidenceKind, SourceKind] = {
    EvidenceKind.MANAGER_TURN: SourceKind.MANAGER_TURN,
    EvidenceKind.JOURNAL: SourceKind.TASK,
    EvidenceKind.LEAD: SourceKind.LEAD,
}

# The source classes each kind accepts (RFC D9): the operator's preferences and corrections only from the operator;
# what messages say never becomes one; a task proposes lessons and project facts only. No kind accepts ``system``.
ALLOWED_CLASSES: Mapping[Kind, frozenset[SourceClass]] = {
    Kind.PREFERENCE: frozenset({SourceClass.OPERATOR}),
    Kind.FEEDBACK: frozenset({SourceClass.OPERATOR}),
    Kind.DECISION: frozenset({SourceClass.OPERATOR, SourceClass.EXTERNAL}),
    Kind.LESSON: frozenset({SourceClass.OPERATOR, SourceClass.AGENT}),
    Kind.FACT: frozenset({SourceClass.OPERATOR, SourceClass.EXTERNAL}),
    Kind.COMMITMENT: frozenset({SourceClass.OPERATOR, SourceClass.EXTERNAL}),
    Kind.PERSON: frozenset({SourceClass.OPERATOR, SourceClass.EXTERNAL}),
    Kind.PROJECT: frozenset({SourceClass.OPERATOR, SourceClass.AGENT, SourceClass.EXTERNAL}),
}


def least_trusted(classes: Iterable[SourceClass | str]) -> SourceClass:
    """The least trusted of ``classes`` (at least one)."""
    found = [SourceClass(value) for value in classes]
    if not found:
        raise ValueError("a candidate needs at least one source")
    return max(found, key=TRUST_ORDER.index)


def allowed(kind: Kind | str, source_class: SourceClass | str) -> bool:
    return SourceClass(source_class) in ALLOWED_CLASSES[Kind(kind)]


class Destination(StrEnum):
    VAULT = "vault"  # written to the knowledge vault after the operator confirms its exact content
    LOOPER = "looper"  # a lesson in effect in PostgreSQL only (memory-tiers RFC; nothing writes one yet)


class State(StrEnum):
    DRAFTING = "drafting"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    WRITING = "writing"
    WRITTEN = "written"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONFLICT = "conflict"
    BLOCKED = "blocked"
    DROPPED = "dropped"
    # A 'looper' row's only (memory-tiers RFC, section 4.3).
    HELD = "held"
    IN_EFFECT = "in_effect"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


VAULT_STATES = frozenset({State.DRAFTING, State.PENDING, State.CONFIRMED, State.WRITING, State.WRITTEN,
                          State.REJECTED, State.EXPIRED, State.CONFLICT, State.BLOCKED, State.DROPPED})
LOOPER_STATES = frozenset({State.HELD, State.IN_EFFECT, State.REVOKED, State.SUPERSEDED, State.EXPIRED,
                           State.REJECTED})
# The states a confirmation holds in: the content hash is the one the operator confirmed.
CONFIRMED_STATES = frozenset({State.CONFIRMED, State.WRITING, State.WRITTEN, State.BLOCKED})
# The states with nothing left to do (a dropped candidate whose drafting failed can be drafted again).
CLOSED_STATES = frozenset({State.WRITTEN, State.REJECTED, State.DROPPED})


class DraftWaitReason(StrEnum):
    TARGET_BUSY = "target_busy"  # The target's confirmed revision must finish before another can be drafted.


DRAFT_BLOCKING_STATES = frozenset({State.CONFIRMED, State.WRITING, State.BLOCKED, State.CONFLICT})


class Reason(StrEnum):
    # dropped
    DUPLICATE = "duplicate"
    REJECTED_BEFORE = "rejected_before"
    SAME_AS = "same_as"
    INVALID = "invalid"
    SECRET = "secret"
    DRAFT_FAILED = "draft_failed"
    # conflict
    BASE_CHANGED = "base_changed"
    ANCHOR_MISSING = "anchor_missing"
    TARGET_CHANGED = "target_changed"
    # blocked
    VAULT_PROBLEM = "vault_problem"
    NOT_GIT = "not_git"
    DETACHED_HEAD = "detached_head"
    BRANCH_MISMATCH = "branch_mismatch"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    DIRTY_TARGET = "dirty_target"
    HOOK_REJECTED = "hook_rejected"
    GIT_FAILED = "git_failed"
    GIT_TIMEOUT = "git_timeout"
    COMMIT_MISMATCH = "commit_mismatch"
    WORKTREE_FAILED = "worktree_failed"
    INTERRUPTED = "interrupted"
    # held (a 'looper' row)
    OVERWRITE_GATE = "overwrite_gate"
    EXTERNAL_SOURCE = "external_source"
    SHADOW = "shadow"
    PROJECT_CAP = "project_cap"
    FLAGGED_WRONG = "flagged_wrong"


# The reasons each state that needs one may give.
REASONS: Mapping[State, frozenset[Reason]] = {
    State.DROPPED: frozenset({Reason.DUPLICATE, Reason.REJECTED_BEFORE, Reason.SAME_AS, Reason.INVALID,
                              Reason.SECRET, Reason.DRAFT_FAILED}),
    State.CONFLICT: frozenset({Reason.BASE_CHANGED, Reason.ANCHOR_MISSING, Reason.TARGET_CHANGED}),
    State.BLOCKED: frozenset({Reason.VAULT_PROBLEM, Reason.NOT_GIT, Reason.DETACHED_HEAD, Reason.BRANCH_MISMATCH,
                              Reason.OPERATION_IN_PROGRESS, Reason.DIRTY_TARGET, Reason.HOOK_REJECTED,
                              Reason.GIT_FAILED, Reason.GIT_TIMEOUT, Reason.COMMIT_MISMATCH, Reason.WORKTREE_FAILED,
                              Reason.INTERRUPTED}),
    State.HELD: frozenset({Reason.OVERWRITE_GATE, Reason.EXTERNAL_SOURCE, Reason.SHADOW, Reason.PROJECT_CAP,
                           Reason.FLAGGED_WRONG}),
}


class Action(StrEnum):
    """What an event records (``memory_candidate_events.action``)."""

    PROPOSE = "propose"
    DRAFT = "draft"
    EDIT = "edit"
    PUSH = "push"
    CONFIRM = "confirm"
    REJECT = "reject"
    EXPIRE = "expire"
    REVIVE = "revive"
    CLAIM = "claim"
    WRITE = "write"
    CONFLICT = "conflict"
    BLOCK = "block"
    RETRY = "retry"
    RECOVER = "recover"  # the writer found its own write cut off by a restart (system)
    REDRAFT = "redraft"
    DROP = "drop"
    MERGE = "merge"
    # A 'looper' row's (memory-tiers RFC).
    ACTIVATE = "activate"
    HOLD = "hold"
    REVOKE = "revoke"
    RENEW = "renew"
    REINFORCE = "reinforce"
    SUPERSEDE = "supersede"
    PROMOTE = "promote"


# Only the operator does these (the schema's memory_events_user).
USER_ACTIONS = frozenset({Action.CONFIRM, Action.REJECT, Action.EDIT, Action.REVIVE, Action.RETRY, Action.REVOKE,
                          Action.RENEW, Action.PROMOTE})


class Actor(StrEnum):
    USER = "user"
    MANAGER = "manager"
    PLANNER = "planner"
    SYSTEM = "system"


class Via(StrEnum):
    DASHBOARD = "dashboard"
    LARK = "lark"
    TELEGRAM = "telegram"
    CLI = "cli"
    SYSTEM = "system"


# Where a confirmation may come from (RFC D5): never the command line, never a model.
CONFIRM_VIAS = frozenset({Via.DASHBOARD, Via.LARK, Via.TELEGRAM})


class MadeBy(StrEnum):
    DRAFT = "draft"
    REDRAFT = "redraft"
    OPERATOR = "operator"


# A 'vault' row's transitions (RFC section 4.2): (from, to) -> the actions that may make it. A new revision is
# (from, pending) with draft, edit, redraft or merge.
TRANSITIONS: Mapping[tuple[State, State], frozenset[Action]] = {
    (State.DRAFTING, State.PENDING): frozenset({Action.DRAFT}),
    (State.DRAFTING, State.DROPPED): frozenset({Action.DROP, Action.MERGE}),
    (State.PENDING, State.PENDING): frozenset({Action.EDIT, Action.REDRAFT, Action.MERGE}),
    (State.PENDING, State.DRAFTING): frozenset({Action.PROPOSE}),
    (State.PENDING, State.CONFIRMED): frozenset({Action.CONFIRM}),
    (State.PENDING, State.REJECTED): frozenset({Action.REJECT}),
    (State.PENDING, State.EXPIRED): frozenset({Action.EXPIRE}),
    (State.EXPIRED, State.PENDING): frozenset({Action.REVIVE}),
    (State.EXPIRED, State.DRAFTING): frozenset({Action.REVIVE}),
    (State.EXPIRED, State.REJECTED): frozenset({Action.REJECT}),
    (State.CONFIRMED, State.WRITING): frozenset({Action.CLAIM}),
    (State.WRITING, State.WRITTEN): frozenset({Action.WRITE}),
    (State.WRITING, State.CONFLICT): frozenset({Action.CONFLICT}),
    (State.WRITING, State.DROPPED): frozenset({Action.DROP}),
    (State.WRITING, State.BLOCKED): frozenset({Action.BLOCK}),
    (State.WRITING, State.CONFIRMED): frozenset({Action.RECOVER}),
    (State.CONFLICT, State.DRAFTING): frozenset({Action.REDRAFT}),
    (State.CONFLICT, State.REJECTED): frozenset({Action.REJECT}),
    (State.BLOCKED, State.CONFIRMED): frozenset({Action.RETRY}),
    (State.BLOCKED, State.REJECTED): frozenset({Action.REJECT}),
    (State.DROPPED, State.DRAFTING): frozenset({Action.REDRAFT}),  # only one whose drafting failed
}


class TransitionRefused(ValueError):
    """A transition the table does not allow; ``state`` is the candidate's."""

    def __init__(self, message: str, *, state: str | None = None) -> None:
        super().__init__(message)
        self.state = state


def check_transition(current: State | str, to: State | str, action: Action | str, *,
                     reason: Reason | str | None = None) -> None:
    """Raise ``TransitionRefused`` unless ``action`` may take a 'vault' row from ``current`` to ``to``; ``reason`` is
    the candidate's (a dropped one may be drafted again only after its drafting failed)."""
    current, to, action = State(current), State(to), Action(action)
    actions = TRANSITIONS.get((current, to))
    if actions is None or action not in actions:
        raise TransitionRefused(f"a {current} candidate cannot go to {to} by {action}", state=current)
    if current is State.DROPPED and reason != Reason.DRAFT_FAILED:
        raise TransitionRefused("only a candidate whose drafting failed can be drafted again", state=current)


_ID = re.compile(r"M-(\d{1,18})", re.IGNORECASE)


def format_id(number: int) -> str:
    return f"M-{number:04d}"


def parse_id(text: str) -> int:
    """``M-0042``, ``M-42`` or ``m-42``; ``ValueError`` for anything else."""
    found = _ID.fullmatch(text.strip()) if isinstance(text, str) else None
    if found is None:
        raise ValueError(f"a memory candidate is named M-<number>, not {text!r}")
    return int(found.group(1))


def normalized_statement(statement: str) -> str:
    """What the fingerprint hashes (RFC section 7): NFKC, lower case, every space, separator and punctuation gone."""
    text = unicodedata.normalize("NFKC", statement).lower()
    return "".join(char for char in text
                   if not char.isspace() and unicodedata.category(char)[0] not in ("Z", "P"))


def fingerprint(kind: Kind | str, statement: str) -> str:
    return hashlib.sha256(f"{Kind(kind)}\n{normalized_statement(statement)}".encode()).hexdigest()


class ChangeOp(StrEnum):
    CREATE_NOTE = "create_note"
    REPLACE_NOTE = "replace_note"
    APPEND_LINE = "append_line"
    REPLACE_LINE = "replace_line"


_SHA = re.compile(r"[0-9a-f]{64}")
_FIELDS: Mapping[ChangeOp, tuple[str, ...]] = {
    ChangeOp.CREATE_NOTE: ("op", "path", "content"),
    ChangeOp.REPLACE_NOTE: ("op", "path", "base_sha256", "content"),
    ChangeOp.APPEND_LINE: ("op", "path", "under", "line"),
    ChangeOp.REPLACE_LINE: ("op", "path", "old", "new"),
}


def _relative_markdown(value: Any) -> str:
    if not isinstance(value, str) or (problem := path_problem(value)):
        raise ValueError(f"a change's path {value!r} {problem if isinstance(value, str) else 'must be a string'}")
    if not value.endswith(".md"):
        raise ValueError(f"a change's path {value} is not a Markdown note (.md)")
    return value


def _line(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value or "\0" in value:
        raise ValueError(f"a change's {what} must be one non-empty line")
    if len(value) > LINE_MAX_CHARS:
        raise ValueError(f"a change's {what} has more than {LINE_MAX_CHARS} characters")
    return value


def parse_change(raw: Any) -> dict[str, Any]:
    """One stored change (RFC section 3.5, with the path and base sha256 code resolved), checked; ``ValueError`` for
    anything else."""
    if not isinstance(raw, Mapping) or raw.get("op") not in set(ChangeOp):
        raise ValueError(f"a change's op must be one of {', '.join(ChangeOp)}")
    op = ChangeOp(raw["op"])
    if set(raw) != set(_FIELDS[op]):
        raise ValueError(f"a {op} change has exactly the fields {', '.join(_FIELDS[op])}")
    change: dict[str, Any] = {"op": op.value, "path": _relative_markdown(raw["path"])}
    if op in (ChangeOp.CREATE_NOTE, ChangeOp.REPLACE_NOTE):
        content = raw["content"]
        if not isinstance(content, str) or "\0" in content:
            raise ValueError("a change's content must be text with no NUL")
        if len(content.encode("utf-8")) > CONTENT_MAX_BYTES:
            raise ValueError(f"a change's content is larger than {CONTENT_MAX_BYTES // 1024} KiB")
        if op is ChangeOp.REPLACE_NOTE:
            if not isinstance(raw["base_sha256"], str) or not _SHA.fullmatch(raw["base_sha256"]):
                raise ValueError("a replace_note change's base_sha256 must be a sha256 in hex")
            change["base_sha256"] = raw["base_sha256"]
        change["content"] = content
    elif op is ChangeOp.APPEND_LINE:
        under = raw["under"]
        change["under"] = None if under is None else _line(under, "heading")
        change["line"] = _line(raw["line"], "line")
    else:
        change["old"] = _line(raw["old"], "old line")
        change["new"] = _line(raw["new"], "new line")
    return change


def parse_changes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not 1 <= len(raw) <= CHANGES_MAX:
        raise ValueError(f"a revision has 1 to {CHANGES_MAX} changes")
    return [parse_change(item) for item in raw]


def _hashed(change: Mapping[str, Any]) -> dict[str, Any]:
    """A change as the content hash covers it: a note's text by its sha256."""
    if "content" not in change:
        return dict(change)
    form = {key: value for key, value in change.items() if key != "content"}
    form["content_sha256"] = hashlib.sha256(change["content"].encode("utf-8")).hexdigest()
    return form


def content_hash(candidate_id: int, subject: str, changes: Sequence[Mapping[str, Any]]) -> str:
    """What a confirmation binds to (RFC section 5.1): the candidate, its commit subject and its changes, each note's
    text by its sha256, as canonical JSON."""
    if not isinstance(subject, str) or not subject.strip() or "\n" in subject:
        raise ValueError("a revision's commit subject must be one non-empty line")
    document = {"v": 1, "candidate": format_id(candidate_id), "subject": subject,
                "changes": [_hashed(change) for change in parse_changes(changes)]}
    return hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def path_problem(raw: str) -> str | None:
    """Why ``raw`` is not a plain relative path inside the vault (no empty, ``.``, ``..`` or hidden parts); None when
    it is one. The knowledge vault's own rule (``vault._path_problem``)."""
    from .vault import _path_problem

    return _path_problem(raw)


# --- The memory_write setting (RFC section 9.1) ---

class Shape(StrEnum):
    NOTE = "note"  # one note per item, in folders, with an index line
    ENTRY = "entry"  # one line in an existing page


@dataclass(frozen=True)
class Target:
    shape: Shape
    folders: tuple[str, ...] = ()  # note: relative to the memory root
    index: str | None = None  # note: the folders' index file, relative to the memory root
    files: tuple[str, ...] = ()  # entry: the pages, relative to the memory root

    def to_json(self) -> dict[str, Any]:
        if self.shape is Shape.NOTE:
            return {"shape": self.shape.value, "folders": list(self.folders), "index": self.index}
        return {"shape": self.shape.value, "files": list(self.files)}


@dataclass(frozen=True)
class Sources:
    manager: bool = False
    tasks: bool = False
    leads: bool = False

    def to_json(self) -> dict[str, bool]:
        return {"manager": self.manager, "tasks": self.tasks, "leads": self.leads}


DEFAULT_PENDING_DAYS = 7
PENDING_DAYS_RANGE = (1, 90)
DEFAULT_DRAFT_BUDGET_USD = 0.15
DEFAULT_DAILY_BUDGET_USD = 1.00
DRAFT_BUDGET_MAX_USD = 10.0
DAILY_BUDGET_MAX_USD = 100.0
RULES_FILES_MAX = 3
REQUIRED_FRONTMATTER_MAX = 20
TARGET_PATHS_MAX = 5
COMMIT_PREFIX_MAX_CHARS = 40
_FRONTMATTER_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,39}")
_KEYS = ("enabled", "root", "rules_files", "targets", "required_frontmatter", "sources", "branch", "worktree_root",
         "commit_prefix", "push_channel", "pending_days", "draft_budget_usd", "daily_budget_usd")


@dataclass(frozen=True)
class MemorySettings:
    root: str  # relative to the knowledge vault; '' is the vault itself
    enabled: bool = False
    rules_files: tuple[str, ...] = ()  # relative to the knowledge vault
    targets: Mapping[Kind, Target] = field(default_factory=dict)
    required_frontmatter: tuple[str, ...] = ()
    sources: Sources = Sources()
    branch: str | None = None  # the main checkout's branch when the setting was saved
    worktree_root: str | None = None  # None: <runtime root>/vault-worktrees
    commit_prefix: str | None = None  # None until saved: the memory root's first folder below the repository
    push_channel: str | None = None
    pending_days: int = DEFAULT_PENDING_DAYS
    draft_budget_usd: float = DEFAULT_DRAFT_BUDGET_USD
    daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD

    def to_json(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "root": self.root, "rules_files": list(self.rules_files),
                "targets": {kind.value: self.targets[kind].to_json() for kind in Kind if kind in self.targets},
                "required_frontmatter": list(self.required_frontmatter), "sources": self.sources.to_json(),
                "branch": self.branch, "worktree_root": self.worktree_root, "commit_prefix": self.commit_prefix,
                "push_channel": self.push_channel, "pending_days": self.pending_days,
                "draft_budget_usd": self.draft_budget_usd, "daily_budget_usd": self.daily_budget_usd}

    def target_paths(self) -> list[str]:
        """Every folder, index and page the targets name, relative to the memory root."""
        return [path for target in self.targets.values()
                for path in (*target.folders, *([target.index] if target.index else []), *target.files)]


def _passes_link(base: Path, relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return any(base.joinpath(*parts[:depth]).is_symlink() for depth in range(1, len(parts) + 1))


def _inside(base: Path, relative: str, *, what: str, kind: str, allow_empty: bool = False) -> Path:
    """``base / relative`` checked: a plain relative path, no link on the way, inside ``base`` once resolved, and an
    existing ``kind`` ('dir' or 'file')."""
    if not isinstance(relative, str):
        raise ValueError(f"{what} must be a string")
    relative = relative.strip().rstrip("/")
    if relative == "" and allow_empty:
        return base
    if problem := path_problem(relative):
        raise ValueError(f"{what} {relative!r} {problem}")
    if _passes_link(base, relative):
        raise ValueError(f"{what} {relative} is or passes through a symbolic link; Looper writes only to real paths")
    target = base / relative
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"{what} {relative} leaves the knowledge vault")
    if kind == "dir" and not target.is_dir():
        raise ValueError(f"{what} {relative} must exist and be a directory")
    if kind == "file" and not target.is_file():
        raise ValueError(f"{what} {relative} must exist and be a regular file")
    return target


def _string_list(raw: Any, what: str, *, most: int) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f"{what} must be a list of strings")
    if len(raw) > most:
        raise ValueError(f"{what} has more than {most} entries")
    return list(raw)


def _target(kind: Kind, raw: Any, root: Path, rules: set[Path]) -> Target:
    what = f"targets.{kind.value}"
    if not isinstance(raw, Mapping) or raw.get("shape") not in set(Shape):
        raise ValueError(f"{what} must be an object whose shape is note or entry")
    shape = Shape(raw["shape"])
    allowed_keys = {"shape", "folders", "index"} if shape is Shape.NOTE else {"shape", "files"}
    if unknown := sorted(set(raw) - allowed_keys):
        raise ValueError(f"{what} has unknown keys: {', '.join(unknown)}")
    if shape is Shape.NOTE:
        folders = [item.strip().rstrip("/") for item in _string_list(raw.get("folders"), f"{what}.folders",
                                                                    most=TARGET_PATHS_MAX)]
        if not folders:
            raise ValueError(f"{what}.folders must name at least one folder")
        if len(set(folders)) != len(folders):
            raise ValueError(f"{what}.folders repeats a folder")
        for folder in folders:
            _inside(root, folder, what=f"{what} folder", kind="dir")
        index = raw.get("index")
        if index is not None:
            path = _inside(root, index, what=f"{what}.index", kind="file")
            index = index.strip()
            if not any(PurePosixPath(index).is_relative_to(folder) for folder in folders):
                raise ValueError(f"{what}.index {index} must be inside one of its folders")
            if path.resolve() in rules:
                raise ValueError(f"{what}.index {index} is a rules file; Looper never writes one")
        return Target(shape, folders=tuple(folders), index=index)
    files = [item.strip() for item in _string_list(raw.get("files"), f"{what}.files", most=TARGET_PATHS_MAX)]
    if not files:
        raise ValueError(f"{what}.files must name at least one page")
    if len(set(files)) != len(files):
        raise ValueError(f"{what}.files repeats a page")
    for name in files:
        if not name.endswith(".md"):
            raise ValueError(f"{what} page {name} is not a Markdown note (.md)")
        if _inside(root, name, what=f"{what} page", kind="file").resolve() in rules:
            raise ValueError(f"{what} page {name} is a rules file; Looper never writes one")
    return Target(shape, files=tuple(files))


def _number(raw: Mapping[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low < value <= high:
        raise ValueError(f"{key} must be a number above {low:g} and at most {high:g}")
    return float(value)


def _optional_line(raw: Mapping[str, Any], key: str, *, most: int) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > most or any(
            unicodedata.category(char)[0] == "C" for char in value):
        raise ValueError(f"{key} must be one line of 1 to {most} characters")
    return value.strip()


def check_settings(raw: Any, vault: Any) -> MemorySettings:
    """The ``memory_write`` value ``raw`` checked against the knowledge vault ``vault`` (``vault.VaultConfig``) and the
    directories as they are now; ``ValueError`` names the first problem. What needs git or the channels (the branch,
    ignored paths, the worktree folder's repository, the push channel) is ``memory_settings``'s."""
    if not isinstance(raw, Mapping):
        raise ValueError("the memory_write setting must be an object")
    if unknown := sorted(set(raw) - set(_KEYS)):
        raise ValueError(f"unknown memory_write keys: {', '.join(unknown)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    base: Path = vault.path
    if "root" not in raw:
        raise ValueError("root is required: the memory folder, relative to the knowledge vault ('' for all of it)")
    root_text = raw["root"].strip().rstrip("/") if isinstance(raw["root"], str) else raw["root"]
    root = _inside(base, root_text, what="root", kind="dir", allow_empty=True)
    for folder in vault.writable:
        if root_text == "" or PurePosixPath(folder).is_relative_to(root_text) or PurePosixPath(
                root_text).is_relative_to(folder):
            raise ValueError(f"root {root_text or '(the whole vault)'} overlaps the writable folder {folder}: what is "
                             "written there without confirmation must stay out of the memory folder")
    rules_files = [item.strip() for item in _string_list(raw.get("rules_files"), "rules_files", most=RULES_FILES_MAX)]
    if len(set(rules_files)) != len(rules_files):
        raise ValueError("rules_files repeats a file")
    rules = {_inside(base, item, what="rules file", kind="file").resolve() for item in rules_files}
    targets_raw = raw.get("targets", {})
    if not isinstance(targets_raw, Mapping):
        raise ValueError("targets must be an object of kind: target")
    if unknown := sorted(set(targets_raw) - set(Kind)):
        raise ValueError(f"targets has unknown kinds: {', '.join(unknown)}")
    targets = {Kind(key): _target(Kind(key), value, root, rules) for key, value in targets_raw.items()}
    required = _string_list(raw.get("required_frontmatter"), "required_frontmatter", most=REQUIRED_FRONTMATTER_MAX)
    if bad := [key for key in required if not _FRONTMATTER_KEY.fullmatch(key)]:
        raise ValueError(f"required_frontmatter has a key that is not a plain name: {bad[0]!r}")
    if len(set(required)) != len(required):
        raise ValueError("required_frontmatter repeats a key")
    sources_raw = raw.get("sources", {})
    if not isinstance(sources_raw, Mapping) or set(sources_raw) - {"manager", "tasks", "leads"} or any(
            not isinstance(value, bool) for value in sources_raw.values()):
        raise ValueError("sources must be an object of manager, tasks and leads, each true or false")
    worktree_root = raw.get("worktree_root")
    if worktree_root is not None:
        if not isinstance(worktree_root, str) or "\0" in worktree_root or not Path(worktree_root).is_absolute():
            raise ValueError("worktree_root must be an absolute path")
        if Path(worktree_root).resolve().is_relative_to(base.resolve()):
            raise ValueError("worktree_root must be outside the knowledge vault")
    branch = _optional_line(raw, "branch", most=255)
    pending_days = raw.get("pending_days", DEFAULT_PENDING_DAYS)
    if isinstance(pending_days, bool) or not isinstance(pending_days, int) or not (
            PENDING_DAYS_RANGE[0] <= pending_days <= PENDING_DAYS_RANGE[1]):
        raise ValueError(f"pending_days must be a whole number from {PENDING_DAYS_RANGE[0]} to {PENDING_DAYS_RANGE[1]}")
    return MemorySettings(
        root=root_text, enabled=enabled, rules_files=tuple(rules_files), targets=targets,
        required_frontmatter=tuple(required), sources=Sources(**sources_raw), branch=branch,
        worktree_root=worktree_root, commit_prefix=_optional_line(raw, "commit_prefix", most=COMMIT_PREFIX_MAX_CHARS),
        push_channel=_optional_line(raw, "push_channel", most=200), pending_days=pending_days,
        draft_budget_usd=_number(raw, "draft_budget_usd", DEFAULT_DRAFT_BUDGET_USD, 0, DRAFT_BUDGET_MAX_USD),
        daily_budget_usd=_number(raw, "daily_budget_usd", DEFAULT_DAILY_BUDGET_USD, 0, DAILY_BUDGET_MAX_USD))
