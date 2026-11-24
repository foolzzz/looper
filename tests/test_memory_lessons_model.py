"""The project lesson lifecycle, replacement gate and independent settings."""

from copy import deepcopy

import pytest

from looper.agents import memory_lessons_model as model
from looper.agents.memory_model import Action, Actor, Reason, SourceClass, State, TransitionRefused, Via


@pytest.mark.parametrize("token", [
    "30", "2.5%", "2026-11-20", "09:30", "`make gen`", "https://example.test/docs",
    "src/worker.py", "C:/cache/file.log", "PORT_A", "v2.15", "retry-count", "CamelCase",
])
def test_protected_tokens_block_an_incomplete_overwrite(token):
    old = f"Keep {token} while retrying the operation."
    assert token in model.protected_tokens(old)
    assert not model.overwrite_allowed(old, "", "Retry the operation with care.", "")
    assert model.overwrite_allowed(old, "", f"Retry carefully; Keep {token}.", "")


def test_protected_tokens_normalize_width_and_keep_case_and_why():
    assert model.overwrite_allowed("端口 ８０８０", "配置 PORT_A", "端口 8080", "配置 PORT_A")
    assert not model.overwrite_allowed("PORT_A", "", "port_a", "")
    assert not model.overwrite_allowed("retry safely", "use `make gen`", "retry cautiously", "use another command")
    assert model.overwrite_allowed("retry safely", "check first", "retry cautiously", "verify first")
    assert model.protected_tokens("timeout 30ms") >= {"30", "30ms"}


_LEGAL = [
    (State.HELD, State.IN_EFFECT, Action.ACTIVATE, Actor.USER, Via.DASHBOARD, None),
    (State.HELD, State.REJECTED, Action.REJECT, Actor.USER, Via.CLI, None),
    (State.IN_EFFECT, State.HELD, Action.HOLD, Actor.SYSTEM, Via.SYSTEM, Reason.FLAGGED_WRONG),
    (State.IN_EFFECT, State.REVOKED, Action.REVOKE, Actor.USER, Via.LARK, None),
    (State.IN_EFFECT, State.IN_EFFECT, Action.RENEW, Actor.USER, Via.TELEGRAM, None),
    (State.IN_EFFECT, State.IN_EFFECT, Action.REINFORCE, Actor.SYSTEM, Via.SYSTEM, None),
    (State.IN_EFFECT, State.SUPERSEDED, Action.SUPERSEDE, Actor.SYSTEM, Via.SYSTEM, None),
    (State.IN_EFFECT, State.EXPIRED, Action.EXPIRE, Actor.SYSTEM, Via.SYSTEM, None),
    (State.HELD, State.EXPIRED, Action.EXPIRE, Actor.SYSTEM, Via.SYSTEM, None),
]


def test_lesson_transition_table_keeps_terminal_states_terminal():
    for current, to, action, actor, via, reason in _LEGAL:
        model.check_lesson_transition(current, to, action, actor=actor, via=via, reason=reason)
    for current in (State.REJECTED, State.REVOKED, State.SUPERSEDED, State.EXPIRED, State.PENDING):
        with pytest.raises(TransitionRefused):
            model.check_lesson_transition(current, State.IN_EFFECT, Action.ACTIVATE,
                                          actor=Actor.USER, via=Via.DASHBOARD)
    for (current, to), actions in model.LESSON_TRANSITIONS.items():
        for action in set(Action) - actions:
            with pytest.raises(TransitionRefused):
                model.check_lesson_transition(current, to, action)


@pytest.mark.parametrize("action,actor,via,to,reason,source_class", [
    (Action.ACTIVATE, Actor.USER, Via.CLI, State.IN_EFFECT, None, SourceClass.AGENT),
    (Action.ACTIVATE, Actor.MANAGER, Via.DASHBOARD, State.IN_EFFECT, None, SourceClass.AGENT),
    (Action.ACTIVATE, Actor.SYSTEM, Via.SYSTEM, State.IN_EFFECT, None, SourceClass.AGENT),
    (Action.ACTIVATE, Actor.USER, Via.DASHBOARD, State.IN_EFFECT, None, SourceClass.EXTERNAL),
    (Action.ACTIVATE, Actor.USER, Via.DASHBOARD, State.IN_EFFECT, None, SourceClass.SYSTEM),
    (Action.HOLD, Actor.USER, Via.DASHBOARD, State.HELD, Reason.FLAGGED_WRONG, SourceClass.AGENT),
    (Action.HOLD, Actor.SYSTEM, Via.SYSTEM, State.HELD, None, SourceClass.AGENT),
    (Action.HOLD, Actor.SYSTEM, Via.SYSTEM, State.HELD, Reason.SECRET, SourceClass.AGENT),
    (Action.REVOKE, Actor.SYSTEM, Via.SYSTEM, State.REVOKED, None, SourceClass.AGENT),
    (Action.RENEW, Actor.SYSTEM, Via.SYSTEM, State.IN_EFFECT, None, SourceClass.AGENT),
    (Action.PROMOTE, Actor.SYSTEM, Via.SYSTEM, State.SUPERSEDED, None, SourceClass.AGENT),
    (Action.PROMOTE, Actor.USER, Via.DASHBOARD, State.SUPERSEDED, None, SourceClass.AGENT),
    (Action.REJECT, Actor.SYSTEM, Via.SYSTEM, State.REJECTED, None, SourceClass.AGENT),
])
def test_lesson_transition_checks_actor_source_and_held_reason(action, actor, via, to, reason, source_class):
    current = State.HELD if action in (Action.ACTIVATE, Action.REJECT) else State.IN_EFFECT
    with pytest.raises(TransitionRefused):
        model.check_lesson_transition(current, to, action, actor=actor, via=via, reason=reason,
                                      source_class=source_class)


_DEFAULT = {
    "enabled": False, "auto_activate": False,
    "inject": {"planner": True, "worker": True, "reviewer": False},
    "max_lessons": 12, "max_chars": 3000, "project_cap": 60, "valid_days": 90,
    "wrong_threshold": 2, "min_journal_entries": 3, "retro_budget_usd": 0.10, "daily_budget_usd": 0.5,
    "weekly": {"enabled": True, "weekday": 1, "time": "09:30", "push_channel": None},
}


def test_lesson_settings_defaults_and_invalid_values():
    assert model.parse_settings(None).to_json() == _DEFAULT
    assert model.parse_settings({}).to_json() == _DEFAULT
    raw = {"enabled": True, "inject": {"reviewer": True}, "weekly": {"time": "23:59"}}
    before = deepcopy(raw)
    result = model.parse_settings(raw)
    assert raw == before
    assert result.enabled and result.inject.reviewer and result.inject.worker
    assert result.weekly.time == "23:59"
    assert model.parse_settings(result.to_json()) == result
    exported = result.to_json()
    exported["inject"]["worker"] = False
    assert result.inject.worker


@pytest.mark.parametrize("raw", [
    [], False, {"unknown": True}, {"enabled": 1}, {"auto_activate": "true"},
    {"inject": None}, {"inject": {"unknown": True}}, {"inject": {"planner": 1}},
    {"max_lessons": 51}, {"max_lessons": True}, {"max_chars": 1.5}, {"project_cap": 0},
    {"valid_days": -1}, {"wrong_threshold": True}, {"min_journal_entries": 0},
    {"retro_budget_usd": True}, {"retro_budget_usd": float("nan")},
    {"daily_budget_usd": float("inf")}, {"daily_budget_usd": 10 ** 1000},
    {"daily_budget_usd": 0}, {"retro_budget_usd": "0.5"},
    {"weekly": None}, {"weekly": {"extra": 1}}, {"weekly": {"weekday": True}},
    {"weekly": {"weekday": 0}}, {"weekly": {"weekday": 8}}, {"weekly": {"weekday": 1.5}},
    {"weekly": {"enabled": 1}}, {"weekly": {"time": "9:30"}}, {"weekly": {"time": "24:00"}},
    {"weekly": {"time": "09:60"}}, {"weekly": {"time": "09:30\n"}},
    {"weekly": {"push_channel": 42}}, {"weekly": {"push_channel": ""}},
])
def test_lesson_settings_reject_invalid_values_without_mutating_input(raw):
    before = deepcopy(raw)
    with pytest.raises(ValueError):
        model.parse_settings(raw)
    # NaN is deliberately unequal to itself, so compare its unchanged representation.
    assert repr(raw) == repr(before)


def test_settings_accept_all_iso_weekdays_and_strict_positive_finite_budgets():
    for weekday in range(1, 8):
        assert model.parse_settings({"weekly": {"weekday": weekday}}).weekly.weekday == weekday
    assert model.parse_settings({"max_lessons": 50, "retro_budget_usd": 1}).retro_budget_usd == 1.0
    assert model.parse_settings({"weekly": {"push_channel": "synthetic-bot"}}).weekly.push_channel == "synthetic-bot"
