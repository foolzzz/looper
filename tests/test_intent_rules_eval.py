"""Offline scoring for MR review extraction cases. No model call."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from evals.processor import __main__ as evaluate
from looper.agents import extraction, leads_model
from looper.agents.leads_model import GROUP_INTENT_TYPES, IntentVia
from looper.agents.ops.service import MANAGER_TOOLS
from looper.db import connection
from tests.subscription_support import processor_says

_FIXTURE = Path(__file__).parent / "fixtures" / "intent_rules_types_off.json"


def _item(type_key, *sources):
    return {"type": type_key, "title": "Synthetic intent title", "summary": "Synthetic intent summary",
            "confidence": "high", "sources": list(sources), "action": "create_task", "project": "p",
            "rule_id": "r1", "template": "t", "command": "rm"}


def _types(monkeypatch):
    async def types(*_args, **_kwargs):
        return [{"key": key, "name": key, "guidance": "Synthetic guidance"}
                for key in ("dev_task", "bug_triage", "alert_triage")]

    async def record(*_):
        return {"init_tools": 0, "init_mcp_servers": 0, "tool_uses": 0, "text_kept": False}

    monkeypatch.setattr(connection, "get_pool", lambda: None)
    monkeypatch.setattr(extraction, "intent_types", types)
    monkeypatch.setattr(evaluate, "_record", record)


def _args(mode):
    return Namespace(budget_usd=0.3, timeout=20, claude="unused", intent_rules=mode)


def test_default_off_instructions_match_the_recorded_types():
    recorded = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    types = recorded["types"]
    requirement = recorded["requirement"]
    direct = extraction.instructions("intents", requirement, types, group=False, rules=False)
    grouped = extraction.instructions("intents", requirement, types, group=True, rules=False)
    mismatches = [direct != recorded["instructions"]["group_false"],
                  grouped != recorded["instructions"]["group_true"],
                  extraction.GROUP_REQUESTS_NOTE != recorded["group_requests_note"]]
    for row in types:
        line = recorded["type_lines"][row["key"]]
        mismatches.append(line not in direct or line not in grouped)
    assert sum(mismatches) == 0
    assert "MR review requests" not in direct and "MR review requests" not in grouped
    assert "mr_review" not in direct
    assert grouped == direct + "\n\n" + recorded["group_requests_note"]
    assert extraction.instructions("intents", requirement, types, group=False, rules=True) == direct


def test_mr_review_eval_cases_use_production_render_and_parse_offline(monkeypatch, tmp_path):
    _types(monkeypatch)
    seen = {}
    real_parse = extraction.parse

    def parse(*args, **kwargs):
        seen["group_seqs"] = kwargs.get("group_seqs")
        seen["group_types"] = kwargs.get("group_types")
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(extraction, "parse", parse)
    correct = {
        "intents_mr_review_direct": {"intents": [_item("mr_review", "m1")]},
        "intents_mr_review_group": {"intents": [_item("mr_review", "m1"), _item("bug_triage", "m2")]},
        "intents_mr_review_quiet": "NONE",
        "intents_mr_review_injection": {"intents": [], "instructions_seen": ["m1"]},
        "intents_mr_review_original_scope": {"intents": [
            _item("mr_review", "m1"), _item("alert_triage", "m2"), _item("weekly_notes", "m3")]},
    }
    wrong = {
        "intents_mr_review_direct": {"intents": [_item("bug_triage", "m1")]},
        "intents_mr_review_group": {"intents": [_item("mr_review", "m1")]},
        "intents_mr_review_quiet": {"intents": [_item("mr_review", "m1")]},
        "intents_mr_review_injection": {"intents": [_item("dev_task", "m1")]},
        "intents_mr_review_original_scope": {"intents": [_item("mr_review", "m1")]},
    }
    fake = processor_says(monkeypatch, "NONE")
    runs = []
    for key, reply in correct.items():
        for mode in ("off", "on"):
            fake.reply = reply if isinstance(reply, str) else json.dumps(reply)
            run = asyncio.run(evaluate._extract(_args(mode), key, 1, tmp_path))
            assert run["passed"], (key, mode, run["checks"])
            prompt = fake.calls[-1]["instructions"]
            if key == "intents_mr_review_direct":
                assert "MR review requests" not in prompt
            if key == "intents_mr_review_group" and mode == "on":
                assert prompt.index(extraction.GROUP_REQUESTS_NOTE) < prompt.index(extraction.MR_REQUESTS_NOTE)
            if mode == "off":
                assert "mr_review " not in prompt and extraction.MR_REQUESTS_NOTE not in prompt
            runs.append(run)
        fake.reply = wrong[key] if isinstance(wrong[key], str) else json.dumps(wrong[key])
        failed = asyncio.run(evaluate._extract(_args("on"), key, 1, tmp_path))
        assert not failed["passed"]
        if key == "intents_mr_review_quiet":
            assert failed["checks"]["quiet"] is False
        elif key == "intents_mr_review_injection":
            assert failed["checks"]["instructions_seen_count"] is False
            assert failed["checks"]["not_followed"] is False
        else:
            assert failed["checks"]["type_and_sources"] is False
    assert seen["group_seqs"] == {1}
    assert seen["group_types"] == leads_model.RULE_GROUP_INTENT_TYPES
    text, refs = extraction.render(
        "intents", evaluate.EXTRACTION_CASES["intents_mr_review_direct"]["rows"], context=[],
        participants=extraction.Participants(evaluate._NAMES, evaluate._USER), known=[],
        zone_name="Asia/Shanghai", text_max_chars=500)
    parsed = extraction.parse(
        "intents", json.dumps(correct["intents_mr_review_direct"]), refs, now=evaluate._BASE, valid_days=90,
        enabled_types={"mr_review"}, app_seqs=(), group_seqs=())
    assert set(parsed.items[0]) == {"type", "title", "summary", "confidence", "sources", "window_sources", "same_as"}
    summary = evaluate.mr_review_summary(runs)
    assert summary["mistaken_auto_create"] is None
    assert "pending" in evaluate.mr_review_lines(runs)
    parser = argparse.ArgumentParser()
    with pytest.raises(SystemExit):
        evaluate._require_intent_rules_cases(parser, Namespace(intent_rules="on", cases=["intents_basic"]))
    evaluate._require_intent_rules_cases(parser, Namespace(intent_rules="on", cases=["intents_mr_review_direct"]))
    evaluate._require_intent_rules_cases(parser, Namespace(intent_rules=None, cases=["intents_basic"]))


def test_recognize_once_keeps_development_only_types_with_rules_on(monkeypatch, tmp_path):
    assert importlib.util.find_spec("looper.db.migrations.intent_recognitions") is None
    assert set(IntentVia) == {IntentVia.SUBSCRIPTION, IntentVia.FOLLOWED_GROUP}
    assert all("recognize" not in tool.name for tool in MANAGER_TOOLS)
    assert leads_model.rules_runtime_ready() is False
    recorded = {}
    real_instructions = extraction.instructions
    real_parse = extraction.parse

    def instructions(*args, **kwargs):
        recorded["rules"] = kwargs.get("rules")
        return real_instructions(*args, **kwargs)

    def parse(*args, **kwargs):
        recorded["group_types"] = kwargs.get("group_types")
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(extraction, "instructions", instructions)
    monkeypatch.setattr(extraction, "parse", parse)
    _types(monkeypatch)
    processor_says(monkeypatch, json.dumps({"intents": [_item("dev_task", "m1")]}))
    path = tmp_path / "set.jsonl"
    path.write_text(json.dumps({
        "type": "window", "number": 1, "messages": [
            {"ref": "x1", "sender": "P1", "sent_at": "2026-11-20T01:00:00+00:00",
             "text": "Private synthetic first", "reply_to": None}]}))
    windows, labels = evaluate._intents_file_cases(path)
    run = asyncio.run(evaluate._intents_file(Namespace(budget_usd=1, timeout=20, claude="unused"),
                                             windows, labels, 1, tmp_path))
    assert recorded["rules"] is False
    assert recorded["group_types"] == GROUP_INTENT_TYPES
    assert run["case"] == "intents_file" and len(run["windows"]) == 1
    assert "task" not in run
