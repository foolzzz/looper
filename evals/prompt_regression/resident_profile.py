"""The resident profile comparison: synthetic data only, fresh dialog servers, one spend ledger."""

from __future__ import annotations

import asyncio
import re
from copy import deepcopy
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
    """Check recall or abstention, allowing surrounding runtime facts; not a model judge."""
    text = re.sub(r"[*_`#]", "", answer).strip().replace("’", "'")
    text = re.sub(r"\byou're\b", "you are", text, flags=re.I)
    text = re.sub(r"\b8:00\b", "08:00", text)
    if not known:
        normalized = re.sub(r"\s+", " ", text).casefold()
        if any(fact.casefold() in normalized for fact in (*FACTS, "Honolulu")):
            return "reply leaks a synthetic profile value"
        if _unsupported_claim(text):
            return "reply asserts an unsupported profile claim"
        unknown = (r"\bI (?:do not|don't|cannot|can't|could not|couldn't) (?:know|have|see|find|locate|tell)\b"
                   r"|\bI (?:have no|have neither|lack|found no|saw no)\b"
                   r"|\bno (?:\w+[ -]){0,3}(?:name|profile|information|rule|quiet.hours)\b"
                   r"|(?:不(?:知道|清楚|了解)|没有|未(?:找到|知|提供)).{0,30}"
                   r"(?:名|身份|资料|画像|你是谁|项目|在做什么|限制|规则|免打扰)")
        # The sole seeded Looper project is operational state, not a personal profile.
        inventory = question == "project" and re.search(r"\beval-calc\b", text, re.I)
        return None if inventory or re.search(unknown, text, re.I) else "expected an abstention without profile claims"
    patterns = {
        "identity": r"(?:you are|your name is|(?:your )?name\s*[:：]|你叫|你是|你的名字是)\s*Mira Vale\b",
        "project": r"(?:you (?:are )?working on|your current project is|(?:current )?project\s*[:：]|你(?:正在|当前)?(?:做|开发|参与)"
                   r"(?:的项目是)?|当前项目[是：:])\s*(?:the )?Lantern Atlas\b",
    }
    if question != "restriction":
        value = FACTS[0 if question == "identity" else 1]
        affirmed = re.search(patterns[question], text, re.I) or re.fullmatch(re.escape(value) + r"[.!。]?", text, re.I)
    else:
        window = r"22:00\s*(?:to|and|[-–—]|至|到)\s*08:00"
        prohibition = (r"no proactive messages|(?:do not|don't|must not|cannot|can't|never) send (?:you )?proactive messages"
                       r"|proactive messages (?:must not|may not|cannot|can't) be sent"
                       r"|quiet (?:hours|period)\s*(?::|(?:is|are|from|between)\b)"
                       r"|(?:is|are) (?:your )?quiet (?:hours|period)\b"
                       r"|not (?:between|from)|不得主动发(?:送)?消息|禁止主动发(?:送)?消息|不发消息|不接收消息|免打扰")
        denial = (r"\b(?:no|without|not(?: your)?) quiet (?:hours|period)\b"
                  r"|\bquiet (?:hours|period)\s*(?::|(?:is|are)\b)\s*not\b"
                  r"|\b(?:do not|don't) have (?:any )?quiet (?:hours|period)\b"
                  r"|(?:没有|不是|并非|无)免打扰")
        permission = (r"\b(?:proactive )?messages (?:are |is )?(?:allowed|permitted|fine)\b"
                      r"|\b(?:can|may) send (?:you )?(?:proactive )?messages\b"
                      r"|\bfine to (?:message|send)\b|(?:可以|允许)发(?:送)?消息")
        # Accept either order within one sentence, and an omitted zone. Explicitly wrong zones still fail.
        affirmed = any(re.search(window, sentence, re.I) and re.search(prohibition, sentence, re.I)
                       for sentence in re.split(r"[.!。\n]", text))
        affirmed = affirmed or re.fullmatch(r"(?:between |from )?" + window
                                            + r"(?:\s+(?:in )?Pacific/Honolulu(?: time)?)?[.!。]?", text, re.I)
        # An allowance outside the window does not contradict a restriction inside it.
        if any(re.search(window, sentence, re.I) and (
                   re.search(denial, sentence, re.I) or any(
                       re.search(permission, clause, re.I) and not re.search(r"\boutside\b|之外|以外", clause, re.I)
                       for clause in re.split(r"[;；,，]", sentence)))
               for sentence in re.split(r"[.!。\n]", text)):
            affirmed = False
        if re.search(r"\b(?:UTC|GMT|[A-Z][a-z]+/[A-Za-z_]+)\b", text, re.I) and "pacific/honolulu" not in text.casefold():
            affirmed = False
    return None if affirmed else f"reply does not affirm the synthetic {question}"


def _unsupported_claim(text: str) -> bool:
    """Reject affirmative personal claims anywhere, including after a valid disclaimer."""
    identity = (r"(?:\byou are|\byour (?:name|identity) is|\bname\s*:|你叫|你是|你的(?:名字|姓名)是)"
                r"[ \t]*([^\s.!。！？,，;；][^.!。！？,，;；\n]*)")
    for match in re.finditer(identity, text, re.I):
        if re.search(r"\b(?:who|what|if|when|whether|work)\s+$", text[:match.start()], re.I):
            continue
        value = match[1].strip()
        if value in ("谁", "什么"):
            continue
        if not re.match(r"(?:(?:the |a )?(?:user|person|operator)\b|(?:\w+ing|signed in)\b"
                        r"|(?:这个|这位)?(?:用户|操作者))", value, re.I):
            return True
    project = (r"(?:\byou (?:are )?working on|\byour (?:current )?project\s*(?:is\b|:)"
               r"|\bcurrent project\s*:|你(?:正在|当前)?(?:做|开发|参与)(?:的项目是)?|你的(?:当前)?项目[是：:])"
               r"[ \t]*([^\s.!。！？,，;；][^.!。！？,，;；\n]*)")
    for match in re.finditer(project, text, re.I):
        if not re.match(r"eval-calc\b", match[1].strip(), re.I):
            return True
    return bool(re.search(
        r"\b\d{1,2}:\d{2}\b|\byour (?:time ?zone|quiet hours|communication (?:rule|restriction))\s*(?:is|are|:)"
        r"|(?:never|must not|don't|do not) send (?:you )?proactive messages|no proactive messages (?:on|between|from)"
        r"|(?:^|[.!。;]\s*|\bbut\s+)proactive messages (?:cannot|can't|may not|must not) be sent"
        r"|你的(?:时区|免打扰时间|沟通限制)[是：:]|(?:禁止|不得)主动发(?:送)?消息", text, re.I))


def rescore(saved: dict[str, Any]) -> dict[str, Any]:
    """Recheck saved replies only; keep source verdicts, operational failures and all run evidence."""
    if (not isinstance(saved, dict) or saved.get("schema") != "looper-resident-profile/v1"
            or saved.get("suite") != "resident-profile"):
        raise ValueError("expected a resident-profile run report")
    if not isinstance(saved.get("dialogs"), list) or not saved["dialogs"]:
        raise ValueError("resident-profile report needs dialog rows")
    result = deepcopy(saved)
    specs = {f"resident_profile_{state}_{question}": (state, question) for state in STATES for question in QUESTIONS}
    answer_problems = {"expected an explicit abstention without profile claims",
                       "expected an abstention without profile claims", "reply leaks a synthetic profile value",
                       "reply asserts an unsupported profile claim",
                       *(f"reply does not affirm the synthetic {q}" for q in QUESTIONS)}
    for row in result["dialogs"]:
        if not isinstance(row, dict) or row.get("id") not in specs:
            raise ValueError("unknown resident-profile dialog")
        if row.get("skipped") or row.get("outcome") == "error":
            continue
        turns = row.get("turns")
        if (row.get("outcome") not in ("pass", "fail") or not isinstance(turns, list) or len(turns) != 1
                or not isinstance(turns[0], dict) or not isinstance(turns[0].get("reply"), str)):
            raise ValueError("scored resident-profile rows need exactly one saved reply")
        row.setdefault("original_scoring", {"outcome": row["outcome"], "success": row.get("success"),
                                           "problems": deepcopy(row.get("problems", [])),
                                           "turn_problems": [t.get("problem") for t in turns]})
        state, question = specs[row["id"]]
        turn = turns[0]
        old = turn.get("problem")
        problems = list(row.get("problems", []))
        if old in problems:
            problems.remove(old)
        if not old or old in answer_problems:
            turn["problem"] = answer_problem(question, turn["reply"], known=state == "present-on")
        if turn.get("problem"):
            problems.insert(0, turn["problem"])
        row.update(problems=problems, success=not problems, outcome="fail" if problems else "pass")
    result["rescored_at"] = datetime.now(timezone.utc).isoformat()
    result["rescore_git"] = runner._git()
    return result


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
