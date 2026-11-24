"""The group-intents eval exercises production rendering/parsing with offline processor replies."""

import asyncio
import json
from argparse import Namespace

import pytest

from evals.processor import __main__ as evaluate
from looper.agents import extraction
from looper.db import connection
from tests.subscription_support import processor_says


def _reply(*sources, type_key="bug_triage"):
    return {"type": type_key, "title": "Synthetic intent title", "summary": "Synthetic intent summary",
            "confidence": "high", "sources": list(sources)}


def _types(monkeypatch):
    async def types(_):
        return [{"key": key, "name": key, "guidance": "Synthetic guidance"}
                for key in ("dev_task", "bug_triage", "alert_triage")]

    async def record(*_):
        return {"init_tools": 0, "init_mcp_servers": 0, "tool_uses": 0, "text_kept": False}

    monkeypatch.setattr(connection, "get_pool", lambda: None)
    monkeypatch.setattr(extraction, "intent_types", types)
    monkeypatch.setattr(evaluate, "_record", record)


@pytest.mark.parametrize("case,reply", [
    ("intents_group_requests", {"intents": [_reply("m1", type_key="dev_task"), _reply("m3"),
                                           _reply("m7", type_key="dev_task")]}),
    ("intents_group_requests_merge", {"intents": [_reply("m1", "m2", "m3")]}),
    ("intents_group_requests_quiet", "NONE"),
    ("intents_group_requests_injection", {"intents": [], "instructions_seen": ["m1"]}),
])
def test_group_cases_run_the_group_contract_offline(monkeypatch, tmp_path, case, reply):
    _types(monkeypatch)
    fake = processor_says(monkeypatch, reply if isinstance(reply, str) else json.dumps(reply))
    args = Namespace(budget_usd=0.3, timeout=20, claude="unused")
    run = asyncio.run(evaluate._extract(args, case, 1, tmp_path))
    assert run["passed"], run["checks"]
    assert fake.calls[0]["instructions"].endswith(extraction.GROUP_REQUESTS_NOTE)
    assert "《产品群》（群 · 全部消息）" in fake.calls[0]["input_text"]
    if case == "intents_group_requests":
        _, refs = extraction.render("intents", evaluate.EXTRACTION_CASES[case]["rows"], context=[],
            participants=extraction.Participants({}, None), known=[], zone_name="UTC", text_max_chars=500)
        missing_named = extraction.parse("intents", json.dumps({"intents": reply["intents"][:2]}), refs,
            now=evaluate._BASE, valid_days=90, enabled_types=("dev_task", "bug_triage"), app_seqs=())
        assert not evaluate.score_extraction(evaluate.EXTRACTION_CASES[case], run, missing_named, refs)["requests_from"]
    if "merged_from" in run["checks"]:
        text, refs = extraction.render("intents", evaluate.EXTRACTION_CASES[case]["rows"], context=[],
            participants=extraction.Participants({}, None), known=[], zone_name="UTC", text_max_chars=500)
        parsed = extraction.parse("intents", json.dumps({"intents": [_reply("m1", "m2", "m3"), _reply("m2")]}),
            refs, now=evaluate._BASE, valid_days=90, enabled_types=("bug_triage",), app_seqs=())
        assert not evaluate.score_extraction(evaluate.EXTRACTION_CASES[case], run, parsed, refs)["one_intent"]


def _file(path, *, labels=True):
    windows = [{"type": "window", "number": 1, "messages": [
        {"ref": "x1", "sender": "P1", "sent_at": "2026-11-20T01:00:00+00:00", "text": "Private synthetic first", "reply_to": None},
        {"ref": "x2", "sender": "P2", "sent_at": "2026-11-20T01:01:00+00:00", "text": "Private synthetic none", "reply_to": "x1"},
        {"ref": "x3", "sender": "P1", "sent_at": "2026-11-20T01:02:00+00:00", "text": "Private synthetic unlabeled", "reply_to": None}]},
        {"type": "window", "number": 2, "messages": [
        {"ref": "x4", "sender": "P1", "sent_at": "2026-11-20T02:00:00+00:00", "text": "Private synthetic second", "reply_to": "x1"}]}]
    rows = windows + ([{"type": "label", "lead": "lead-1", "kind": "issue", "expected": "intent", "sources": ["x1", "x4"]},
                       {"type": "label", "lead": "lead-2", "kind": "fact", "expected": "none", "sources": ["x2"]}]
                      if labels else [])
    path.write_text("\n".join(json.dumps(row) for row in rows))
    return path


def test_intents_file_counts_recall_once_and_false_positive_sources_without_text(monkeypatch, tmp_path, capsys):
    _types(monkeypatch)
    fake = processor_says(monkeypatch)
    fake.outcomes = [(json.dumps({"intents": [_reply("m1"), _reply("m2"), _reply("m3")]}), None),
                     (json.dumps({"intents": [_reply("m1")]}), None)]
    windows, labels = evaluate._intents_file_cases(_file(tmp_path / "set.jsonl"))
    assert windows[0]["rows"][1]["parent_id"] == "x1"
    assert all(row["via_group"] for window in windows for row in window["rows"])
    run = asyncio.run(evaluate._intents_file(Namespace(budget_usd=1, timeout=20, claude="unused"),
                                           windows, labels, 1, tmp_path))
    assert (run["expected"], run["recalled"], run["intents"], run["false_positives"]) == (1, 1, 4, 2)
    assert run["intents_per_100"] == 100 and run["sources_per_intent"] == 1
    assert [w["cost_usd"] for w in run["windows"]] == [0.01, 0.01]
    assert not run["passed"]  # 50 false positives per 100 messages exceeds the predeclared gate.
    output = capsys.readouterr().out + json.dumps(run) + evaluate.table({"runs": [run]}, None)
    assert "Private synthetic" not in output and "Synthetic intent" not in output and "reply" not in run


def test_intents_file_shares_its_budget_between_windows(monkeypatch, tmp_path):
    _types(monkeypatch)
    fake = processor_says(monkeypatch, "NONE")
    fake.cost = 0.1
    windows, labels = evaluate._intents_file_cases(_file(tmp_path / "set.jsonl", labels=False))
    run = asyncio.run(evaluate._intents_file(Namespace(budget_usd=0.1, timeout=20, claude="unused"),
                                           windows, labels, 1, tmp_path))
    assert len(fake.calls) == 1 and len(run["windows"]) == 1 and not run["passed"]


def test_invalid_intents_file_does_not_echo_its_contents(tmp_path):
    path = tmp_path / "invalid.jsonl"
    path.write_text('{"type":"window","messages":[{"text":"PRIVATE-CONTENT"}]}')
    with pytest.raises(ValueError, match=r"^invalid intents file at line 1$"):
        evaluate._intents_file_cases(path)
