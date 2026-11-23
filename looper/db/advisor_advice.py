"""A consult of the advisor's question and advice (``advisor_advice``, the advisor RFC, section 5.3): one row per
``agent_spawns`` row whose request passed its checks. Its question is the last request sent; its advice the answer,
once there is one."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool
from ..agents.run_usage import CostStatus

# The kinds of request (``advice.AdviceKind``'s values; the db package does not import the agents').
_KINDS = frozenset({"diagnose", "design_choice", "plan_review", "unblock"})

SOURCES = ("ladder", "checkpoint", "optional")


def _row(record: asyncpg.Record | None) -> dict[str, Any] | None:
    if record is None:
        return None
    row = dict(record)
    for key in ("question", "advice", "adoption_detail"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    return row


async def record_question(*, spawn_id: int, task_id: str, step_id: str | None, attempt: int, source: str, kind: str,
                          question: Mapping[str, Any], pool: asyncpg.Pool | None = None) -> dict[str, Any]:
    """The consult's row with ``question`` as its question: created by its first request sent, replaced by the
    next (a round that answers what the advisor asked)."""
    if source not in SOURCES:
        raise ValueError(f"unknown advice source {source!r}")
    pool = pool or get_pool()
    row = await pool.fetchrow(
        """INSERT INTO advisor_advice (spawn_id, task_id, step_id, attempt, source, kind, question)
           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
           ON CONFLICT (spawn_id) DO UPDATE SET question = EXCLUDED.question RETURNING *""",
        spawn_id, task_id, step_id, attempt, source, kind, json.dumps(dict(question)))
    return _row(row)  # type: ignore[return-value]


async def record_advice(spawn_id: int, advice: Mapping[str, Any], *,
                        pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """The consult's answer; None when it has no row (its request was never sent)."""
    pool = pool or get_pool()
    return _row(await pool.fetchrow("UPDATE advisor_advice SET advice = $2::jsonb WHERE spawn_id = $1 RETURNING *",
                                    spawn_id, json.dumps(dict(advice))))


async def get(spawn_id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    return _row(await pool.fetchrow("SELECT * FROM advisor_advice WHERE spawn_id = $1", spawn_id))


async def adopt(spawn_id: int, adoption: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> None:
    """What the Planner took of the consult's answer (``advice_adoption``)."""
    pool = pool or get_pool()
    await pool.execute(
        "UPDATE advisor_advice SET adoption = $2, adoption_detail = $3::jsonb, adopted_at = now() "
        "WHERE spawn_id = $1 AND advice IS NOT NULL AND adoption IS NULL",
        spawn_id, adoption["adoption"], json.dumps({"items": list(adoption.get("items", ()))}))


async def unlock(task_id: str, step_id: str, attempt: int, passed: bool, *,
                 pool: asyncpg.Pool | None = None) -> int:
    """Whether the attempt the step's answered consults were for passed (``unlocked``), set once; how many."""
    pool = pool or get_pool()
    status = await pool.execute(
        "UPDATE advisor_advice SET unlocked = $4, unlocked_at = now() WHERE task_id = $1 AND step_id = $2 "
        "AND attempt = $3 AND advice IS NOT NULL AND unlocked IS NULL", task_id, step_id, attempt, passed)
    return int(status.split()[-1])


async def for_step(step_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The step's consults that sent a request, oldest first."""
    pool = pool or get_pool()
    return [_row(r) for r in await pool.fetch(
        "SELECT * FROM advisor_advice WHERE step_id = $1 ORDER BY spawn_id", step_id)]


async def consults_view(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The task's consults of the advisor, oldest first, as its page shows them: where each came from and for which
    attempt, how it ended (its state and why it failed), its rounds, returns, spend and duration, the checks its
    request failed (their stage and category, with how many times), the request that was sent (``question``: only
    one that passed every check, never its bindings), the advisor's questions back (``needs_info``), its answer
    (``advice``), what the Planner took of it and why (``adoption`` and ``adoption_items``) and whether the attempt
    passed. Never a request that was sent back, nor where a check matched."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """SELECT s.id, s.step_id, s.state, s.rounds, s.returns, s.spent_usd, s.brief, s.result,
                  s.created_at, s.finished_at, a.kind, a.attempt, a.question, a.advice, a.adoption, a.adoption_detail,
                  a.unlocked
             FROM agent_spawns s LEFT JOIN advisor_advice a ON a.spawn_id = s.id
            WHERE s.task_id = $1 AND s.kind = 'advisor' ORDER BY s.id""", task_id)
    consults = []
    for row in rows:
        brief = json.loads(row["brief"]) if isinstance(row["brief"], str) else (row["brief"] or {})
        advice = json.loads(row["advice"]) if isinstance(row["advice"], str) else (row["advice"] or {})
        result = json.loads(row["result"]) if isinstance(row["result"], str) else (row["result"] or {})
        checks = Counter((str(c.get("stage")), str(c.get("category"))) for c in result.get("checks", []))
        question = json.loads(row["question"]) if isinstance(row["question"], str) else row["question"]
        detail = json.loads(row["adoption_detail"]) if isinstance(row["adoption_detail"], str) else (
            row["adoption_detail"] or {})
        ended = row["finished_at"]
        consults.append({
            # a request that never passed has no question: its kind is the one the Planner first wrote, when it is one
            "id": row["id"], "step_id": row["step_id"], "source": brief.get("source"),
            "kind": row["kind"] or (brief.get("asked") if brief.get("asked") in _KINDS else None),
            "attempt": row["attempt"] if row["attempt"] is not None else brief.get("attempt"),
            "state": row["state"], "failure": result.get("failure"), "rounds": row["rounds"], "returns": row["returns"],
            "spent_usd": float(row["spent_usd"]), "status": advice.get("status"),
            "cost_status": (CostStatus.UNKNOWN if result.get("unpriced_runs") else CostStatus.KNOWN).value,
            "checks": [{"stage": stage, "category": category, "count": count}
                       for (stage, category), count in checks.items()],
            "answers": [{"question_id": a["question_id"], "conclusion": a["conclusion"],
                         "confidence": a.get("confidence")} for a in advice.get("answers", [])],
            "adoption": row["adoption"], "unlocked": row["unlocked"],
            "question": question, "needs_info": list(result.get("needs_info") or []),
            "advice": advice or None,
            "adoption_items": [{"ref": i.get("ref"), "use": i.get("use"), "why": i.get("why", "")}
                               for i in detail.get("items", [])],
            "created_at": row["created_at"].isoformat(),
            "finished_at": ended.isoformat() if ended else None,
            "duration_s": round((ended - row["created_at"]).total_seconds(), 1) if ended else None,
        })
    return consults


# The heading of the line a task's summary gives its consults (the Planner adds it while the advisor is on).
SUMMARY_HEADING = "## Advisor"
_SUMMARY = re.compile(r"(?:\n\n)?^## Advisor\n[^\n]*", re.M)


def without_summary(text: str) -> str:
    """``text`` (a task's summary) without the advisor's line in it."""
    return _SUMMARY.sub("", text)


async def summary_line(task_id: str, *, pool: asyncpg.Pool | None = None) -> str | None:
    """The line a task's summary gives its consults of the advisor (the advisor RFC, section 14): how many, what
    they spent, the points the Planner took, those of them whose attempt then passed, and the steps whose
    checkpoint consult did not end with an answer (one the advisor refused included); None for a task without
    one."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        """SELECT count(*) AS consults, COALESCE(sum(s.spent_usd), 0) AS spent,
                  bool_or(jsonb_array_length(COALESCE(s.result->'unpriced_runs', '[]')) > 0) AS unknown,
                  COALESCE(sum((SELECT count(*) FROM jsonb_array_elements(a.adoption_detail->'items') i
                                 WHERE i->>'use' IN ('adopted', 'partial'))), 0) AS taken,
                  COALESCE(sum((SELECT count(*) FROM jsonb_array_elements(a.adoption_detail->'items') i
                                 WHERE i->>'use' IN ('adopted', 'partial'))) FILTER (WHERE a.unlocked), 0) AS helped,
                  array_remove(array_agg(DISTINCT st.step_key) FILTER (
                      WHERE s.brief->>'source' = 'checkpoint' AND s.finished_at IS NOT NULL
                      AND NOT (s.state = 'answered' AND a.advice->>'status' = 'answered')), NULL) AS incomplete
             FROM agent_spawns s LEFT JOIN advisor_advice a ON a.spawn_id = s.id
             LEFT JOIN task_steps st ON st.id = s.step_id
            WHERE s.task_id = $1 AND s.kind = 'advisor'""", task_id)
    if not row["consults"]:
        return None
    money = (f"花费未知（已知小计 {float(row['spent']):.2f} 美元）" if row["unknown"]
             else f"花费 {float(row['spent']):.2f} 美元")
    line = (f"请教 advisor {row['consults']} 次，{money}，采纳 {row['taken']} 条，"
            f"其中 {row['helped']} 条帮步骤通过")
    if row["incomplete"]:
        line += f"；检查点请教未完成：{', '.join(sorted(row['incomplete']))}"
    return line
