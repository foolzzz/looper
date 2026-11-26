"""The resident profile comparison: synthetic data only, fresh dialog servers, one spend ledger."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from looper.agents.operator_profile import HEADING

from . import dialogs, runner

NOTE = """## Name
Mira Vale
## Time zone
Pacific/Honolulu
## Communication
No proactive messages from 22:00 to 08:00 in Pacific/Honolulu.
- Current project: Lantern Atlas [kind:: fact]
"""
SOURCE = "synthetic-operator.md"
FACTS = ("Mira Vale", "Lantern Atlas", "22:00", "08:00", "Pacific/Honolulu")
QUESTIONS = {
    "identity": "Who am I?",
    "project": "What am I working on?",
    "restriction": "When may proactive messages not be sent?",
}
ANSWERS = {
    "identity": "You are Mira Vale.",
    "project": "You are working on Lantern Atlas.",
    "restriction": "No proactive messages from 22:00 to 08:00 in Pacific/Honolulu.",
}
STATES = ("present-off", "present-on", "absent-off", "absent-on")


def answer_problem(question: str, answer: str, *, known: bool) -> str | None:
    """Conservative bilingual patterns, not a model judge; see README for false negatives/positives."""
    text = re.sub(r"[*_`#]", "", answer).strip().replace("’", "'")
    text = re.sub(r"\byou're\b", "you are", text, flags=re.I)
    if not known:
        # Match the whole reply: a disclaimer followed by an invented identity must fail.
        unknown = (r"I (?:do not|don't) (?:know|have (?:any |enough )?information (?:about|on)) "
                   r"(?:who you are|your (?:name|identity|current project|communication restrictions?|quiet hours)"
                   r"|what you are working on|when proactive messages (?:may not|must not|cannot) be sent)"
                   r"[.!。]?(?:\s*Please (?:tell me|provide (?:that|this) information)[.!。]?)?"
                   r"|我(?:还|目前)?(?:不知道|不清楚|没有)(?:你的?|您(?:的)?|当前)?"
                   r"(?:名字|身份|是谁|在做什么|当前项目|项目|沟通限制|免打扰时间)(?:的信息)?[。！.!]?")
        return None if re.fullmatch(unknown, text, re.I) else "expected an explicit abstention without profile claims"
    patterns = {
        "identity": r"(?:you are|your name is|你叫|你是|你的名字是)\s*Mira Vale\b",
        "project": r"(?:you (?:are )?working on|your current project is|你(?:正在|当前)?(?:做|开发|参与)"
                   r"(?:的项目是)?|当前项目[是：:])\s*(?:the )?Lantern Atlas\b",
        "restriction": r"(?:no proactive messages|(?:do not|don't|must not|cannot) send proactive messages"
                       r"|proactive messages (?:must not|may not|cannot) be sent|quiet hours (?:are|:)"
                       r"|不得主动发(?:送)?消息|禁止主动发(?:送)?消息)"
                       r"[^.!。]*22:00\s*(?:to|and|[-–—]|至|到)\s*08:00[^.!。]*Pacific/Honolulu",
    }
    return None if re.search(patterns[question], text, re.I) else f"reply does not affirm the synthetic {question}"


def seed(api: runner.Api, runtime_root: Path, state: str) -> None:
    """Write only beneath this case's disposable runtime; no operator notes or existing vaults."""
    vault = runtime_root / "synthetic-vault"
    vault.mkdir()
    note = vault / SOURCE
    note.write_text(NOTE, encoding="utf-8")
    runner._confirmed(api, "/api/v1/knowledge_vault/update", {
        "path": str(vault), "profile_source": SOURCE, "writable": [], "manager_read": False})
    if state.startswith("absent"):
        note.unlink()  # Configure a valid source, then exercise its unavailable-file path.
    runner._confirmed(api, "/api/v1/profile_settings/update", {"enabled": state.endswith("-on")})


def check(look: dialogs.Look, *, question: str, state: str) -> str | None:
    known = state == "present-on"
    source = look.api.post("/api/v1/profile_source/get", {})["result"]
    setting = look.api.post("/api/v1/profile_settings/get", {})["result"]
    compiled = look.api.post("/api/v1/profile/get", {})["result"]["text"]
    if source != {"source": SOURCE} or setting != {"enabled": state.endswith("-on")}:
        return "synthetic source or injection switch readback mismatch"
    if bool(compiled) != state.startswith("present"):
        return "synthetic profile presence readback mismatch"
    if state.startswith("present") and any(fact not in compiled for fact in FACTS):
        return "the compiled synthetic source is missing a profile fact"
    if look.fake:
        assert look.runtime_root is not None
        received = (look.runtime_root.parent / "scenarios.context.txt").read_text(encoding="utf-8")
        block, channel, _ = received.partition("\nChannel:")
        if not channel or not block.startswith("Looper state (rev "):
            return "the fresh turn did not receive a whole stable block"
        if known:
            if HEADING not in block or not compiled or compiled not in block:
                return "the stable block did not carry the compiled synthetic profile"
        elif HEADING in received or any(fact in received for fact in FACTS):
            return "profile content reached a turn with injection off or source absent"
    return answer_problem(question, look.reply, known=known)


def cases() -> tuple[dialogs.Dialog, ...]:
    return tuple(dialogs.Dialog(
        f"resident_profile_{state}_{question}",
        (dialogs.Say(text, partial(check, question=question, state=state),
                     (("__capture_context__", {}), ("__reply__", {"text": ANSWERS[question]
                       if state == "present-on" else "I don't know " + {
                           "identity": "who you are.", "project": "what you are working on.",
                           "restriction": "your communication restrictions."}[question]}))),),
        resident_profile=state, tags=("resident_profile",),
    ) for state in STATES for question, text in QUESTIONS.items())


def reported_models(database_url: str) -> list[str]:
    """Model versions from explicit runtime evidence, independent of configured aliases."""
    import asyncpg

    async def read():
        conn = await asyncpg.connect(database_url)
        try:
            return await conn.fetch("SELECT DISTINCT metadata->>'model_reported' AS model FROM llm_records "
                                    "WHERE agent_id = 'manager' AND metadata->>'model_reported' IS NOT NULL "
                                    "ORDER BY model")
        finally:
            await conn.close()

    return [row["model"] for row in asyncio.run(read())]


def run(*, mode: str, admin_url: str, out_dir: Path, claude_bin: str | None = None,
        reps: tuple[int, int] = (2, 1), total_cap_usd: float = 12,
        sonnet_cap_usd: float = .5, opus_cap_usd: float = 1,
        timeout: float | None = None) -> tuple[dict[str, Any], Path]:
    if mode not in ("fake", "real") or len(reps) != 2 or reps[0] < 1 or reps[1] != 1:
        raise ValueError("use fake/real mode and --reps SONNET,1; Opus retries only its first failed cases")
    for value, ceiling in ((total_cap_usd, 12), (sonnet_cap_usd, .5), (opus_cap_usd, 1)):
        runner.check_case_budget(value)
        if value > ceiling:
            raise ValueError(f"budget may not exceed {ceiling:g} USD")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    stopped, aborted = False, None
    spent = 0.0

    def batch(model: str, selected: tuple[dialogs.Dialog, ...], count: int, attempt: int = 1):
        nonlocal spent, stopped, aborted
        if stopped or spent >= total_cap_usd:
            stopped = True
            return
        result, _ = dialogs.run(
            mode=mode, dialogs=selected, admin_url=admin_url,
            out_dir=out_dir / f"{stamp}-{model}-{attempt}-{len(rows)}",
            model=model, manager_model=model, manager_effort="xhigh" if model == "opus" else None,
            stable_context=True, reps=count, claude_bin=claude_bin, timeout=timeout,
            max_budget_usd=sonnet_cap_usd if model == "sonnet" else opus_cap_usd,
            total_cap_usd=total_cap_usd - spent)
        for row in result["dialogs"]:
            rows.append({**row, "requested_model": model, "effort": "xhigh" if model == "opus" else None,
                         "attempt": attempt})
            spent += float(row.get("cost_usd") or 0)
        stopped = result["stopped_by_cap"] or spent >= total_cap_usd
        aborted = result["aborted"]

    try:
        batch("sonnet", cases(), reps[0])
        if not aborted:
            batch("opus", cases(), 1)
        if not aborted and not stopped:
            failed = {r["id"] for r in rows if r["requested_model"] == "opus" and r.get("outcome") == "fail"}
            if failed:
                batch("opus", tuple(c for c in cases() if c.id in failed), 1, attempt=2)
    except BaseException as exc:
        aborted = runner.aborted_reason(exc)
        if not isinstance(exc, Exception):
            raise
    finally:
        result = {
            "schema": "looper-resident-profile/v1", "suite": "resident-profile", "mode": mode,
            "started_at": stamp, "git": runner._git(), "reps": list(reps),
            "caps": {"total_cap_usd": total_cap_usd, "sonnet_cap_usd": sonnet_cap_usd,
                     "opus_cap_usd": opus_cap_usd},
            "planned_calls": 12 * sum(reps),
            "completed_calls": sum("outcome" in r for r in rows),
            "resolved_models": sorted({m for r in rows for m in r.get("resolved_models", [])}),
            "stopped_by_cap": stopped, "aborted": aborted,
            "total_cost_usd": round(spent, 6), "dialogs": rows,
        }
        # Keep the aggregate's separate schema out of ordinary dialog baseline discovery.
        path = out_dir / f"{stamp}-{mode}-resident-profile.json"
        result = runner.write_result(result, path, roots=(), rows="dialogs")
    return result, path
