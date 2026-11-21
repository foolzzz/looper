"""Memory candidates' shared vocabulary (memory-write-path RFC, sections 3–5 and 9.1): ids, which sources each kind
accepts, the transitions code may make, the content hash a confirmation binds to, the fingerprint, and the checks of
the ``memory_write`` setting against the knowledge vault's folders."""

import hashlib
import os
from pathlib import Path

import pytest

from looper.agents import memory_model as model
from looper.agents.memory_model import Action, Kind, Reason, SourceClass, State
from looper.agents.vault import VaultConfig


def test_ids_read_and_show_as_m_numbers():
    assert model.format_id(42) == "M-0042" and model.format_id(12345) == "M-12345"
    assert [model.parse_id(text) for text in ("M-0042", "M-42", "m-42", " M-7 ")] == [42, 42, 42, 7]
    for text in ("42", "M-", "M-4x", "L-0042", "I-0042", "M -42", ""):
        with pytest.raises(ValueError):
            model.parse_id(text)


def test_each_kind_accepts_only_its_source_classes():
    # The operator's preferences and corrections come only from the operator; messages never become one.
    for kind in (Kind.PREFERENCE, Kind.FEEDBACK):
        assert [c for c in SourceClass if model.allowed(kind, c)] == [SourceClass.OPERATOR]
    for kind in (Kind.DECISION, Kind.FACT, Kind.COMMITMENT, Kind.PERSON):
        assert not model.allowed(kind, SourceClass.AGENT) and model.allowed(kind, SourceClass.EXTERNAL)
    assert model.allowed(Kind.LESSON, SourceClass.AGENT) and not model.allowed(Kind.LESSON, SourceClass.EXTERNAL)
    assert model.allowed(Kind.PROJECT, SourceClass.EXTERNAL) and model.allowed(Kind.PROJECT, SourceClass.AGENT)
    assert not any(model.allowed(kind, SourceClass.SYSTEM) for kind in Kind)
    assert set(model.ALLOWED_CLASSES) == set(Kind)


def test_a_candidate_is_as_trusted_as_its_least_trusted_source():
    assert model.least_trusted([SourceClass.OPERATOR]) is SourceClass.OPERATOR
    assert model.least_trusted(["operator", "agent"]) is SourceClass.AGENT
    assert model.least_trusted(["agent", "external", "operator"]) is SourceClass.EXTERNAL
    assert model.least_trusted(["operator", "system"]) is SourceClass.SYSTEM
    with pytest.raises(ValueError):
        model.least_trusted([])


@pytest.mark.parametrize("current,to,action", [
    ("drafting", "pending", "draft"), ("drafting", "dropped", "drop"), ("pending", "pending", "edit"),
    ("pending", "drafting", "redraft"), ("pending", "confirmed", "confirm"), ("pending", "rejected", "reject"),
    ("pending", "expired", "expire"), ("expired", "pending", "revive"), ("expired", "rejected", "reject"),
    ("confirmed", "writing", "claim"), ("writing", "written", "write"), ("writing", "conflict", "conflict"),
    ("writing", "blocked", "block"), ("writing", "confirmed", "recover"), ("conflict", "drafting", "redraft"),
    ("blocked", "confirmed", "retry"), ("blocked", "rejected", "reject"),
])
def test_the_transitions_of_the_rfc_are_allowed(current, to, action):
    model.check_transition(current, to, action)


@pytest.mark.parametrize("current,to,action", [
    ("pending", "written", "write"),          # no write without a confirmation and a claim
    ("drafting", "confirmed", "confirm"),     # nothing to confirm before a revision
    ("pending", "confirmed", "edit"),         # an edit is a new revision, never a confirmation
    ("confirmed", "pending", "edit"),         # a confirmed candidate is not edited in place
    ("written", "pending", "edit"),           # written is final
    ("rejected", "pending", "revive"),        # rejected is final
    ("writing", "confirmed", "retry"),        # the writer's own recovery is not the operator's retry
    ("pending", "in_effect", "activate"),     # a lesson's states are no vault row's
])
def test_other_transitions_are_refused(current, to, action):
    with pytest.raises(model.TransitionRefused):
        model.check_transition(current, to, action)


def test_only_a_candidate_whose_drafting_failed_is_drafted_again():
    model.check_transition(State.DROPPED, State.DRAFTING, Action.REDRAFT, reason=Reason.DRAFT_FAILED)
    with pytest.raises(model.TransitionRefused):
        model.check_transition(State.DROPPED, State.DRAFTING, Action.REDRAFT, reason=Reason.DUPLICATE)


def test_the_fingerprint_ignores_case_spaces_punctuation_and_width_but_not_kind_or_words():
    base = model.fingerprint("preference", "Weekly reports: conclusion first.")
    assert model.fingerprint("preference", "  weekly REPORTS conclusion first ") == base
    assert model.fingerprint("preference", "ｗｅｅｋｌｙ　reports，conclusion first。") == base
    assert model.fingerprint("decision", "Weekly reports: conclusion first.") != base
    assert model.fingerprint("preference", "Weekly reports: details first.") != base
    assert model.fingerprint("preference", "周报先写结论，再写细节。") == model.fingerprint("preference", "周报 先写结论 再写细节")
    assert base == hashlib.sha256("preference\nweeklyreportsconclusionfirst".encode()).hexdigest()


_NOTE = {"op": "create_note", "path": "notes/weekly.md", "content": "---\nkind: memory\n---\nConclusion first.\n"}
_INDEX = {"op": "append_line", "path": "notes/_index.md", "under": "## Index", "line": "- [[weekly]] conclusion first"}


def test_the_content_hash_covers_the_candidate_its_subject_and_every_byte_of_its_changes():
    digest = model.content_hash(42, "mem: M-0042 preference weekly", [_NOTE, _INDEX])
    assert digest == model.content_hash(42, "mem: M-0042 preference weekly",
                                         [dict(reversed(list(_NOTE.items()))), _INDEX])  # key order is not content
    variants = [
        (43, "mem: M-0042 preference weekly", [_NOTE, _INDEX]),
        (42, "mem: M-0042 preference weekly!", [_NOTE, _INDEX]),
        (42, "mem: M-0042 preference weekly", [{**_NOTE, "content": _NOTE["content"] + " "}, _INDEX]),
        (42, "mem: M-0042 preference weekly", [_NOTE, {**_INDEX, "under": None}]),
        (42, "mem: M-0042 preference weekly", [_INDEX, _NOTE]),  # the order is applied order
    ]
    assert len({digest, *(model.content_hash(*variant) for variant in variants)}) == 1 + len(variants)
    replace = {"op": "replace_note", "path": "notes/a.md", "base_sha256": "a" * 64, "content": "x"}
    assert model.content_hash(1, "s", [replace]) != model.content_hash(1, "s", [{**replace, "base_sha256": "b" * 64}])


@pytest.mark.parametrize("change", [
    {"op": "delete_note", "path": "notes/a.md"},
    {**_NOTE, "extra": 1},
    {**_NOTE, "path": "../mine/a.md"},
    {**_NOTE, "path": "/notes/a.md"},
    {**_NOTE, "path": "notes/.hidden/a.md"},
    {**_NOTE, "path": "notes/a.txt"},
    {**_NOTE, "content": "x" * (16 * 1024 + 1)},
    {**_NOTE, "content": "nul\0"},
    {"op": "replace_note", "path": "notes/a.md", "base_sha256": "not-a-sha", "content": "x"},
    {**_INDEX, "line": "two\nlines"},
    {**_INDEX, "line": "  "},
    {**_INDEX, "under": "x" * 1001},
    {"op": "replace_line", "path": "notes/a.md", "old": "a", "new": ""},
])
def test_a_change_that_is_not_one_of_the_four_typed_ones_is_refused(change):
    with pytest.raises(ValueError):
        model.content_hash(1, "subject", [change])


def test_a_revision_has_one_to_three_changes_and_a_one_line_subject():
    for changes in ([], [_NOTE] * 4):
        with pytest.raises(ValueError):
            model.content_hash(1, "subject", changes)
    for subject in ("", "two\nlines"):
        with pytest.raises(ValueError):
            model.content_hash(1, subject, [_NOTE])


# --- the memory_write setting ---

@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    for folder in ("mem/prefs", "mem/decisions", "mem/me", "inbox", "mine"):
        (root / folder).mkdir(parents=True)
    for name, text in (("mem/AGENTS.md", "rules"), ("mem/prefs/_index.md", "# Index\n"),
                       ("mem/me/commitments.md", "# Commitments\n"), ("AGENTS.md", "vault rules")):
        (root / name).write_text(text)
    return VaultConfig(root.resolve(), (), "mem/AGENTS.md")


def _raw(**changes):
    raw = {"enabled": True, "root": "mem", "rules_files": ["mem/AGENTS.md", "AGENTS.md"],
           "targets": {"preference": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"},
                       "decision": {"shape": "note", "folders": ["decisions"], "index": None},
                       "commitment": {"shape": "entry", "files": ["me/commitments.md"]}},
           "required_frontmatter": ["created", "kind", "summary"],
           "sources": {"manager": True}, "pending_days": 14, "draft_budget_usd": 0.2, "daily_budget_usd": 2}
    raw.update(changes)
    return {key: value for key, value in raw.items() if value is not ...}


def test_a_full_setting_is_kept_as_given_with_the_defaults_filled_in(vault):
    settings = model.check_settings(_raw(), vault)
    assert settings.to_json() == {
        "enabled": True, "root": "mem", "rules_files": ["mem/AGENTS.md", "AGENTS.md"],
        "targets": {"preference": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"},
                    "decision": {"shape": "note", "folders": ["decisions"], "index": None},
                    "commitment": {"shape": "entry", "files": ["me/commitments.md"]}},
        "required_frontmatter": ["created", "kind", "summary"],
        "sources": {"manager": True, "tasks": False, "leads": False}, "branch": None, "worktree_root": None,
        "commit_prefix": None, "push_channel": None, "pending_days": 14, "draft_budget_usd": 0.2,
        "daily_budget_usd": 2.0}
    assert model.check_settings(settings.to_json(), vault) == settings  # what it stores reads back the same
    minimal = model.check_settings({"root": "mem"}, vault)
    assert (minimal.enabled, minimal.targets, minimal.sources, minimal.pending_days) == (
        False, {}, model.Sources(), model.DEFAULT_PENDING_DAYS)


def test_the_memory_folder_may_be_the_whole_vault_when_nothing_is_writable_without_confirmation(vault):
    whole = model.check_settings(_raw(root="", targets={"preference": {
        "shape": "note", "folders": ["mem/prefs"], "index": "mem/prefs/_index.md"}}), vault)
    assert whole.root == "" and whole.target_paths() == ["mem/prefs", "mem/prefs/_index.md"]
    for root, writable in (("", ("inbox",)), ("mem", ("mem",)), ("mem", ("mem/prefs",)), ("mem/prefs", ("mem",))):
        with pytest.raises(ValueError, match="overlaps the writable folder"):
            model.check_settings(_raw(root=root, targets={}), VaultConfig(vault.path, writable, None))
    assert model.check_settings(_raw(targets={}), VaultConfig(vault.path, ("inbox",), None)).root == "mem"


@pytest.mark.parametrize("changes,problem", [
    ({"root": ...}, "root is required"),
    ({"root": "../outside"}, "root"),
    ({"root": "missing"}, "must exist and be a directory"),
    ({"root": "mem/AGENTS.md"}, "must exist and be a directory"),
    ({"surprise": 1}, "unknown memory_write keys"),
    ({"enabled": "yes"}, "enabled must be true or false"),
    ({"rules_files": ["missing.md"]}, "must exist and be a regular file"),
    ({"rules_files": ["a.md", "b.md", "c.md", "d.md"]}, "more than 3"),
    ({"targets": {"wish": {"shape": "note", "folders": ["prefs"]}}}, "unknown kinds"),
    ({"targets": {"fact": {"shape": "table"}}}, "shape is note or entry"),
    ({"targets": {"fact": {"shape": "note", "folders": []}}}, "at least one folder"),
    ({"targets": {"fact": {"shape": "note", "folders": ["missing"]}}}, "must exist and be a directory"),
    ({"targets": {"fact": {"shape": "note", "folders": ["prefs"], "index": "me/commitments.md"}}},
     "inside one of its folders"),
    ({"targets": {"fact": {"shape": "note", "folders": ["prefs"], "files": ["x.md"]}}}, "unknown keys"),
    ({"targets": {"fact": {"shape": "entry", "files": ["AGENTS.md"]}}}, "is a rules file"),
    ({"targets": {"fact": {"shape": "entry", "files": ["me"]}}}, "Markdown"),
    ({"required_frontmatter": ["kind", "two words"]}, "not a plain name"),
    ({"sources": {"manager": "on"}}, "sources must be"),
    ({"sources": {"inbox": True}}, "sources must be"),
    ({"pending_days": 0}, "pending_days"), ({"pending_days": 91}, "pending_days"), ({"pending_days": True}, "pending"),
    ({"draft_budget_usd": 0}, "draft_budget_usd"), ({"daily_budget_usd": 101}, "daily_budget_usd"),
    ({"worktree_root": "relative/dir"}, "absolute"),
    ({"commit_prefix": "two\nlines"}, "commit_prefix"), ({"commit_prefix": "x" * 41}, "commit_prefix"),
])
def test_a_setting_that_does_not_hold_is_refused_saying_why(vault, changes, problem):
    with pytest.raises(ValueError, match=problem):
        model.check_settings(_raw(**changes), vault)


def test_the_worktree_folder_must_be_outside_the_vault(vault, tmp_path):
    with pytest.raises(ValueError, match="outside the knowledge vault"):
        model.check_settings(_raw(worktree_root=str(vault.path / "mem" / "trees")), vault)
    assert model.check_settings(_raw(worktree_root=str(tmp_path / "trees")), vault).worktree_root == str(
        tmp_path / "trees")


def test_no_memory_path_may_be_or_pass_through_a_symbolic_link(vault, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", vault.path / "mem" / "linked")
    os.symlink(vault.path / "mem" / "prefs", vault.path / "mem" / "alias")
    for changes in ({"root": "mem/linked"},
                    {"targets": {"fact": {"shape": "note", "folders": ["alias"]}}},
                    {"targets": {"fact": {"shape": "note", "folders": ["linked"]}}}):
        with pytest.raises(ValueError, match="symbolic link"):
            model.check_settings(_raw(**changes), vault)


def test_the_enums_stay_in_step_with_each_other():
    assert set(model.VAULT_STATES) | set(model.LOOPER_STATES) == set(State)
    assert {state for pair in model.TRANSITIONS for state in pair} <= model.VAULT_STATES
    assert {reason for reasons in model.REASONS.values() for reason in reasons} == set(Reason)
    assert model.USER_ACTIONS <= set(Action)
    assert set(model.EVIDENCE_CLASS) == set(model.EvidenceKind) == set(model.SOURCE_KIND)
    assert Path(model.__file__).name == "memory_model.py"
