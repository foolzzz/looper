"""Recall and precision of the leads search, ``leads.search_mode`` ``words`` against ``fulltext``, on a synthetic,
labelled set (``leads_search_set.json``; assistant search RFC, section 4).

Each query is searched as ``looper leads search`` and the Manager's tool do (``agents.leads.search``, at most 5 leads)
in a schema of its own: as written in both modes, and in its bilingual form (the words both ways, as the fulltext
tool description asks) in ``fulltext``. A query's recall is the share of its relevant leads in the 5; precision is
the share of the returned leads that are relevant, over all queries. ``fulltext`` runs with pg_trgm when the
database has it, and again without it.

Run from a checkout's root against a disposable PostgreSQL database (it creates and drops a schema there)::

    uv run --extra test python -m evals.leads_search --pg-url postgresql://USER:PASSWORD@HOST:PORT/DB
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SET = Path(__file__).with_name("leads_search_set.json")
SOURCE = "eval-leads"
LIMIT = 5
MODES = ("words", "fulltext", "fulltext_bilingual", "fulltext_no_trgm")


def load() -> dict[str, Any]:
    return json.loads(SET.read_text(encoding="utf-8"))


async def seed(pool: Any, data: Mapping[str, Any]) -> dict[str, int]:
    """The set's leads, one source message each in its chat, by its sender (oldest first, one minute apart): their
    database ids by label."""
    now = datetime.now(timezone.utc)
    await pool.execute("INSERT INTO event_sources (id, kind, name) VALUES ($1, 'lark_user', 'Eval') "
                       "ON CONFLICT DO NOTHING", SOURCE)
    for key, name in data["chats"].items():
        await pool.execute("INSERT INTO source_chats (source_id, chat_id, kind, name) VALUES ($1, $2, 'group', $3)",
                           SOURCE, f"oc_{key}", name)
    for key, name in data["people"].items():
        await pool.execute("INSERT INTO source_people (source_id, open_id, name, name_source) "
                           "VALUES ($1, $2, $3, 'contact')", SOURCE, f"ou_{key}", name)
    ids = {}
    for number, lead in enumerate(data["leads"]):
        sent = now - timedelta(days=3) + timedelta(minutes=number)
        seq = await pool.fetchval(
            "INSERT INTO source_events (source_id, external_id, chat_id, sender_id, sender_kind, sent_at, msg_type, "
            "mentions_me, text, body_bytes, origin) VALUES ($1, $2, $3, $4, 'user', $5, 'text', false, $6, 1, 'live') "
            "RETURNING seq", SOURCE, f"om_{lead['id']}", f"oc_{lead['chat']}", f"ou_{lead['by']}", sent,
            lead["summary"])
        ids[lead["id"]] = await pool.fetchval(
            "INSERT INTO message_leads (source_id, kind, summary, detail, confidence, valid_until, first_sent_at, "
            "last_sent_at) VALUES ($1, $2, $3, $4, 'high', now() + interval '90 days', $5, $5) RETURNING id",
            SOURCE, lead["kind"], lead["summary"], lead["detail"], sent)
        await pool.execute("INSERT INTO message_lead_sources (lead_id, event_seq) VALUES ($1, $2)", ids[lead["id"]], seq)
        await pool.execute("INSERT INTO message_lead_people (lead_id, open_id, role) VALUES ($1, $2, 'sender')",
                           ids[lead["id"]], f"ou_{lead['by']}")
    return ids


async def run_mode(pool: Any, data: Mapping[str, Any], ids: Mapping[str, int], mode: str) -> list[dict[str, Any]]:
    """Each query's labels of what ``mode`` returned, best first."""
    from looper.agents import leads, leads_model
    from looper.db import message_leads

    labels = {f"lead-{number}": label for label, number in ids.items()}
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE "
                       "SET value = $2", leads_model.LEADS_SEARCH_MODE_KEY,
                       "words" if mode == "words" else "fulltext")
    trigram = message_leads.trigram_schema
    if mode == "fulltext_no_trgm":
        async def no_trigram(db: Any = None) -> None:
            return None
        message_leads.trigram_schema = no_trigram
    try:
        results = []
        for query in data["queries"]:
            text = query.get("bilingual", query["query"]) if mode == "fulltext_bilingual" else query["query"]
            found = await leads.search(words=text.split(), limit=LIMIT, source_id=SOURCE, db=pool)
            results.append({"id": query["id"], "category": query["category"], "relevant": query["relevant"],
                            "returned": [labels[lead["id"]] for lead in found["leads"]]})
        return results
    finally:
        message_leads.trigram_schema = trigram


def summary(results: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None]:
    """Mean recall, micro precision (None when nothing was returned), the queries with a relevant lead returned, and
    the mean reciprocal rank."""
    recall = [len(set(r["relevant"]) & set(r["returned"])) / len(r["relevant"]) for r in results]
    returned = sum(len(r["returned"]) for r in results)
    hits = sum(len(set(r["relevant"]) & set(r["returned"])) for r in results)
    ranks = [next((1 / (i + 1) for i, label in enumerate(r["returned"]) if label in r["relevant"]), 0.0)
             for r in results]
    return {"queries": len(results), "recall": round(sum(recall) / len(results), 3),
            "precision": round(hits / returned, 3) if returned else None,
            "found": sum(1 for value in recall if value > 0), "mrr": round(sum(ranks) / len(results), 3)}


async def measure(pool: Any, data: Mapping[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every mode's results on ``pool``, a migrated schema of its own."""
    data = data or load()
    ids = await seed(pool, data)
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true') "
                       "ON CONFLICT (key) DO UPDATE SET value = 'true'")
    return {mode: await run_mode(pool, data, ids, mode) for mode in MODES}


def table(measured: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    categories = sorted({r["category"] for r in next(iter(measured.values()))})
    lines = ["| queries | mode | recall@5 | precision@5 | found | MRR |", "|---|---|---|---|---|---|"]
    for category in ["all", *categories]:
        for mode, results in measured.items():
            chosen = [r for r in results if category in ("all", r["category"])]
            s = summary(chosen)
            precision = "—" if s["precision"] is None else f"{s['precision']:.2f}"
            lines.append(f"| {category} ({s['queries']}) | {mode} | {s['recall']:.2f} | {precision} | "
                         f"{s['found']}/{s['queries']} | {s['mrr']:.2f} |")
    return "\n".join(lines)


async def _main(pg_url: str, out: Path | None) -> int:
    import asyncpg

    from looper.db import connection, message_leads

    schema = f"looper_eval_leads_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(pg_url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(pg_url, min_size=1, max_size=2, server_settings={"search_path": schema})
    try:
        await connection.ensure_schema(pool)
        trigram = await message_leads.trigram_schema(pool)
        measured = await measure(pool)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
    print(f"pg_trgm: {'installed in ' + trigram if trigram else 'not installed'}\n")
    print(table(measured))
    if out is not None:
        out.write_text(json.dumps({"trigram": trigram, "results": measured}, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pg-url", required=True, help="a disposable PostgreSQL database (a schema is made there)")
    parser.add_argument("--out", type=Path, help="write every query's results as JSON here")
    args = parser.parse_args()
    return asyncio.run(_main(args.pg_url, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
