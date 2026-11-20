"""Real-path check of what a Lark group chat and a direct chat see of a task they ask for: a disposable
``looper serve`` with the lark-cli stand-in (``tests/lark_cli_stand_in.py``) first on PATH and its Lark channel
configured, a task created, started and run to its end in each conversation (and in ``group-schedule`` a
schedule created and then changed in a group).

Run from a checkout's root (it needs a disposable PostgreSQL server and Claude Code; it costs money)::

    uv run --extra test python -m evals.lark_group [--model sonnet] [--reps 2] [--claude PATH]
        [--manager-model opus --manager-effort xhigh] [--pg-url URL] [--port N] [--baseline RUN.json]

To measure a checkout without this module (the baseline), run this file with that checkout's interpreter and
``--checkout``, as ``evals/lark_quote.py`` says.

Each conversation is two messages: one asking for a small task and confirming it (the Manager creates it ready),
then one starting it. A group must see the Manager's answers with only the question whether to start (never the
task's configuration, and no project directory in any answer), no breakdown or progress, and the end notice; a
direct chat sees the configuration, the progress and the notice, as before. In ``group-schedule`` the group asks
for a schedule, then changes its time: no answer may show a setting's value (no before → after, directory, budget or
requirement), and the change must be made. The stand-in's ``event consume``
delivers the conversation's messages so far each time it starts; every answer goes to the stand-in's log, never
to Lark. Each conversation and rep has its own database, server and runtime root. The JSON goes to
``evals/runs/`` (ignored by git), without the posts' text when ``--no-text`` is given.

``--conversations completion`` runs ``completion-direct`` and ``completion-group``: the same two messages with the
Manager's completion turn on (``manager.completion_turns`` = ``dm_only``, set through the API once the server is up).
A direct chat must end with the Manager's own message instead of the code notice: the last post is an answer and no
notice was posted, it links the task, its first line is a sentence (no heading, list item or table row), it has at
most 8 non-empty lines, the turn made no write call, and the task's ``manager_events`` row is ``delivered``. A group
chat must still end with the code notice, and its event row is ``skipped`` (``group_chat``). On a checkout without
the feature (``--checkout``), the setting is refused and the table is missing: that is recorded
(``completion_setting``, ``event_state`` None) and the conversation fails its event checks instead of crashing.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parents[1]
SCHEMA = "looper-lark-group/v1"
PROJECT = "eval-group"
ASK = (f"在 {PROJECT} 项目里建一个任务：在工作目录里创建 done.txt，内容是 done。目录用项目默认的。"
       "不用再问我，我确认创建，先不要开始。")
START = "开始这个任务。"
# Lines only Looper's configuration block has (``projects.configuration_block``), and the start questions
# (``ops.tasks``); the Manager's own words may well say 「目录」.
CONFIGURATION = ("Ready — not started", "任务已创建：\n", "- Planner：", "- 评审：", "- 每个任务的花费上限：",
                 "- 并发：这个任务同时最多运行")
START_QUESTION = "现在开始吗？回复「开始」就会启动"
GROUP_START_QUESTION = "现在开始吗？@我回复「开始」就会启动。"
PROGRESS = ("任务已分解", "进度更新")
NOTICE = ("✅ 任务完成 · ", "❌ 任务失败 · ")
CHATS = {"group": "oc_group", "direct": "oc_dm", "group-schedule": "oc_group", "completion-direct": "oc_dm",
         "completion-group": "oc_group"}
GROUPS = ("group", "group-schedule", "completion-group")
COMPLETION = ("completion-direct", "completion-group")
ALIASES = {"completion": ",".join(COMPLETION)}
ENDED = ("completed", "failed", "cancelled")
# How long after a task's end its event row may take to finish (the completion turn's own limit is 120 s), and how
# long to wait for a row that never appears.
EVENT_SECONDS, NO_EVENT_SECONDS = 240.0, 30.0
MAX_LINES = 8
LINK_LINE = re.compile(r"^\[[^\]\n]*#tasks/[^\]\n]*\]\(", re.MULTILINE)
NOT_A_SENTENCE = re.compile(r"^(#|-|\*|\||\d+\.)")
SCHEDULE_ASK = (f"在 {PROJECT} 项目里建一个定时任务 deps：每个工作日早上 9 点（Asia/Shanghai 时区）检查依赖有没有新版本，"
                "把结果写进 deps.md。目录用项目默认的。")
SCHEDULE_CHANGE = "把定时任务 deps 改成每个工作日上午 10 点。"
# What Looper writes of a schedule's settings in a direct chat (``ops.schedules``): values, a change's before → after.
SCHEDULE_VALUES = ("→", "$", "- 需求：", "- 目录：", "- 花费上限：")
TASK_COLUMNS = ("passed", "configuration_shown", "start_question", "work_dir_in_answers", "progress_posts",
                "notice_last")
SCHEDULE_COLUMNS = ("passed", "settings_shown", "changed")
COMPLETION_DIRECT_COLUMNS = ("passed", "manager_last", "link", "first_line", "lines_ok", "no_writes", "delivered")
COMPLETION_GROUP_COLUMNS = ("passed", "notice_last", "event_skipped", "configuration_shown", "start_question",
                            "work_dir_in_answers", "progress_posts")


def _calls(log: Path) -> list[list[str]]:
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [argv[2:] if argv[:1] == ["--profile"] else argv for argv in rows]


def _posts(calls: list[list[str]]) -> list[dict[str, str]]:
    """What the bot posted, in order: each answer, progress message and notice (status lines and edits left out)."""
    posts = []
    for argv in calls:
        if "+messages-reply" not in argv and "+messages-send" not in argv:
            continue
        flag = "--markdown" if "--markdown" in argv else "--text"
        body = argv[argv.index(flag) + 1]
        if flag == "--text" and body.startswith("⏳"):
            continue
        kind = ("notice" if any(head in body for head in NOTICE) else
                "progress" if body.startswith(PROGRESS) else "answer")
        to = (f"reply:{argv[argv.index('--message-id') + 1]}" if "--message-id" in argv else
              f"chat:{argv[argv.index('--chat-id') + 1]}" if "--chat-id" in argv else
              f"user:{argv[argv.index('--user-id') + 1]}")
        posts.append({"kind": kind, "to": to, "text": body})
    return posts


def score(conversation: str, posts: list[dict[str, str]], work_dir: str, status: str | None) -> dict[str, Any]:
    """The checks of one conversation's run (what its chat was sent, and how the task ended)."""
    answers = [p["text"] for p in posts if p["kind"] == "answer"]
    first = answers[0] if answers else ""
    checks = {
        "configuration_shown": any(word in answer for answer in answers for word in CONFIGURATION),
        "start_question": (GROUP_START_QUESTION if conversation == "group" else START_QUESTION) in first,
        "work_dir_in_answers": any(work_dir in answer for answer in answers),
        "progress_posts": sum(p["kind"] == "progress" for p in posts),
        "notice_last": bool(posts) and posts[-1]["kind"] == "notice",
        "task_status": status,
    }
    if conversation == "group":
        passed = (not checks["configuration_shown"] and checks["start_question"] and not checks["work_dir_in_answers"]
                  and checks["progress_posts"] == 0)
    else:
        passed = checks["configuration_shown"] and checks["start_question"] and checks["progress_posts"] > 0
    return {**checks, "passed": passed and checks["notice_last"] and status == "completed"}


def manager_words(text: str) -> str:
    """The Manager's part of a completion message: what comes before the link lines Looper appends."""
    found = LINK_LINE.search(text)
    return (text[:found.start()] if found else text).strip()


def score_completion(posts: list[dict[str, str]], task_id: str | None, status: str | None,
                     event: dict[str, Any] | None, turn_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """The checks of a direct chat with the completion turn on. ``event`` is the task's ``manager_events`` row
    (None when there is none or the table is missing), ``turn_calls`` the completion turn's ``manager_actions``."""
    last = posts[-1] if posts else {"kind": "", "text": ""}
    words = manager_words(last["text"])
    lines = [line for line in words.splitlines() if line.strip()]
    checks = {
        "manager_last": last["kind"] == "answer" and not any(p["kind"] == "notice" for p in posts),
        "link": bool(task_id) and f"#tasks/{task_id}" in last["text"],
        "first_line": bool(lines) and not NOT_A_SENTENCE.match(lines[0].lstrip()),
        "lines_ok": len(lines) <= MAX_LINES,
        "no_writes": not any(call.get("risk") != "read" for call in turn_calls),
        "delivered": bool(event) and event.get("state") == "delivered",
    }
    return {**checks, "task_status": status, "turn_calls": [call.get("tool") for call in turn_calls],
            "event_state": (event or {}).get("state"), "event_reason": (event or {}).get("reason"),
            "turn_cost_usd": (event or {}).get("turn_cost_usd"), "seconds_to_sent": (event or {}).get("seconds_to_sent"),
            "passed": all(checks.values()) and status == "completed"}


def score_completion_group(posts: list[dict[str, str]], work_dir: str, status: str | None,
                           event: dict[str, Any] | None) -> dict[str, Any]:
    """A group chat with the switch on: the group checks as today, and the event row is skipped for a group."""
    checks = score("group", posts, work_dir, status)
    skipped = bool(event) and event.get("state") == "skipped" and event.get("reason") == "group_chat"
    return {**checks, "event_skipped": skipped, "event_state": (event or {}).get("state"),
            "event_reason": (event or {}).get("reason"), "passed": checks["passed"] and skipped}


def score_schedule(posts: list[dict[str, str]], work_dir: str, cron: str | None) -> dict[str, Any]:
    """The checks of a group's schedule conversation: no setting's value in any answer, and the change made."""
    answers = [p["text"] for p in posts if p["kind"] == "answer"]
    shown = any(word in answer for answer in answers for word in (*SCHEDULE_VALUES, work_dir))
    changed = cron is not None and cron.split()[:2] == ["0", "10"]
    return {"settings_shown": shown, "changed": changed, "cron": cron, "passed": not shown and changed}


def _event(stand_in: Any, conversation: str, message_id: str, text: str) -> dict[str, Any]:
    group = conversation in GROUPS
    return {"type": "im.message.receive_v1", "chat_id": CHATS[conversation], "chat_type": "group" if group else "p2p",
            "message_id": message_id, "message_type": "text", "sender_type": "user", "sender_id": stand_in.OPEN_ID,
            "content": f"@HelperBot {text}" if group else text, "mentions": [stand_in.BOT_MENTION] if group else []}


def run_once(args: argparse.Namespace, stand_in: Any, root: Path, conversation: str) -> dict[str, Any]:
    import asyncpg
    from evals.prompt_regression import runner
    from looper.agents.serve import seed_channel_configs
    from looper.db import connection

    runtime, bin_dir, work = root / "runtime", root / "bin", root / "work"
    for path in (runtime, bin_dir, work):
        path.mkdir(parents=True)
    log, world_file = root / "lark-cli.jsonl", root / "world.json"
    world = {**stand_in.WORLD, "events": []}
    world_file.write_text(json.dumps(world), encoding="utf-8")
    script = bin_dir / "lark-cli.py"
    script.write_text(stand_in._STAND_IN.format(log=str(log), world=str(world_file), bot=stand_in.BOT_OPEN_ID))
    (bin_dir / "lark-cli").write_text(f"#!/bin/sh\nexec {shlex.join([sys.executable, str(script)])} \"$@\"\n")
    (bin_dir / "lark-cli").chmod(0o755)
    runner.install_claude(bin_dir, mode="real", real_claude=args.claude, max_budget_usd=args.max_budget_usd)
    with runner.fresh_database(args.pg_url) as (database_url, _):
        async def seed() -> None:
            pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
            try:
                await connection.ensure_schema(pool)
                await seed_channel_configs(pool)
                await pool.execute("UPDATE channel_configs SET enabled=TRUE, config=$1::jsonb "
                                   "WHERE channel_id='channel-lark'", json.dumps({
                                       "profile": "cli_main", "allowed_open_ids": [stand_in.OPEN_ID],
                                       "allowlist_app_id": "cli_main"}))
            finally:
                await pool.close()

        async def event_of(conn: Any, task_id: str | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
            """The task's event row and the completion turn's tool calls; a checkout without the feature has no
            table."""
            if task_id is None or await conn.fetchval("SELECT to_regclass('manager_events')") is None:
                return None, []
            row = await conn.fetchrow(
                "SELECT e.state, e.reason, e.started_at, e.finished_at, e.sent_at, "
                "extract(epoch FROM e.sent_at - e.created_at) AS seconds_to_sent, "
                "(SELECT cost_usd FROM llm_records WHERE id = e.record_id) AS turn_cost_usd "
                "FROM manager_events e WHERE e.task_id = $1 ORDER BY e.id DESC LIMIT 1", task_id)
            if row is None:
                return None, []
            event = {"state": row["state"], "reason": row["reason"], "ended": bool(row["finished_at"] or row["sent_at"]),
                     "seconds_to_sent": None if row["seconds_to_sent"] is None else round(float(row["seconds_to_sent"]), 1),
                     "turn_cost_usd": None if row["turn_cost_usd"] is None else float(row["turn_cost_usd"])}
            start, end = row["started_at"], row["finished_at"] or row["sent_at"]
            calls = [] if start is None or end is None else [dict(call) for call in await conn.fetch(
                "SELECT tool, risk, status, error_code FROM manager_actions WHERE message_id = '' "
                "AND created_at BETWEEN $1 AND $2 ORDER BY id", start, end)]
            return event, calls

        async def state() -> dict[str, Any]:
            conn = await asyncpg.connect(database_url)
            try:
                # Whatever columns the checkout's tasks table has (a baseline has no chat_group).
                row = await conn.fetchval("SELECT to_jsonb(t)::text FROM tasks t ORDER BY created_at DESC LIMIT 1")
                task = json.loads(row) if row is not None else None
                event, turn_calls = await event_of(conn, (task or {}).get("id"))
                return {"tasks": await conn.fetchval("SELECT count(*) FROM tasks"),
                        "task": task, "event": event, "turn_calls": turn_calls, "has_events": await conn.fetchval(
                            "SELECT to_regclass('manager_events') IS NOT NULL"),
                        "cron": await conn.fetchval("SELECT cron FROM schedules ORDER BY created_at DESC LIMIT 1"),
                        "cost_usd": float(await conn.fetchval("SELECT coalesce(sum(cost_usd), 0) FROM llm_records"))}
            finally:
                await conn.close()

        def wait(condition: Any, seconds: float) -> bool:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if condition():
                    return True
                time.sleep(2)
            return False

        asyncio.run(seed())
        if args.port:
            runner.free_port = lambda: args.port
        started = time.monotonic()
        completion = conversation in COMPLETION
        setting: str | None = None
        ended_seen: dict[str, float] = {}

        def finished() -> bool:
            """The task ended and what its chat is told about it is out: the notice, or with the completion turn on
            the event row's end (a checkout without the table, or a row that never appears, falls back to the notice)."""
            now = asyncio.run(state())
            if (now["task"] or {}).get("status") not in ENDED:
                return False
            notice = any(p["kind"] == "notice" for p in _posts(_calls(log)))
            if not completion or setting is not None:
                return notice
            ended = ended_seen.setdefault("at", time.monotonic())
            if now["event"] is not None:
                return now["event"]["ended"]
            return notice or not now["has_events"] or time.monotonic() - ended > NO_EVENT_SECONDS

        with runner.running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir,
                                   log_path=root / "serve.log") as api:
            runner.select_agent(api, agent=None, model=args.model)
            if args.manager_model:
                from evals.prompt_regression.dialogs import _manager_profile
                _manager_profile(api, args.manager_model, args.manager_effort)
            api.post("/api/v1/project_admin/create", {"name": PROJECT, "work_dir": str(work)})
            if completion:
                try:
                    api.post("/api/v1/chat_settings/update", {"completion_turns": "dm_only"})
                except Exception as exc:  # a checkout without the feature answers an error
                    setting = str(exc)[:300]
            schedule = conversation == "group-schedule"
            events = [_event(stand_in, conversation, "om_g1", SCHEDULE_ASK if schedule else ASK)]
            world_file.write_text(json.dumps({**world, "events": events}), encoding="utf-8")
            answered = wait(lambda: (asyncio.run(state())["cron"] is not None if schedule
                                     else asyncio.run(state())["tasks"] > 0)
                            and any(p["kind"] == "answer" for p in _posts(_calls(log))), args.timeout / 3)
            if answered and schedule:
                time.sleep(3)
                events.append(_event(stand_in, conversation, "om_g2", SCHEDULE_CHANGE))
                world_file.write_text(json.dumps({**world, "events": events}), encoding="utf-8")
                wait(lambda: sum(p["kind"] == "answer" for p in _posts(_calls(log))) >= 2, args.timeout / 3)
            elif answered:
                time.sleep(3)  # the answer's last edit
                events.append(_event(stand_in, conversation, "om_g2", START))
                world_file.write_text(json.dumps({**world, "events": events}), encoding="utf-8")
                wait(finished, args.timeout + (EVENT_SECONDS if completion else 0))
            time.sleep(6 if completion else 3)
        final = asyncio.run(state())
    posts = _posts(_calls(log))
    task = final["task"] or {}
    if conversation == "group-schedule":
        checks = score_schedule(posts, str(work), final["cron"])
    elif conversation == "completion-direct":
        checks = score_completion(posts, task.get("id"), task.get("status"), final["event"], final["turn_calls"])
    elif conversation == "completion-group":
        checks = score_completion_group(posts, str(work), task.get("status"), final["event"])
    else:
        checks = score(conversation, posts, str(work), task.get("status"))
    result = {"conversation": conversation, **checks,
              **({"completion_setting": setting or "dm_only"} if completion else {}),
              "chat_group": task.get("chat_group"), "seconds": round(time.monotonic() - started, 1),
              "cost_usd": final["cost_usd"], "posts": [{k: v for k, v in p.items() if k != "text" or not args.no_text}
                                                       for p in posts]}
    return result


def table(run: dict[str, Any], base: dict[str, Any] | None) -> str:
    def cell(data: dict[str, Any], conversation: str, column: str) -> str:
        reps = [rep for rep in data["reps"] if rep["conversation"] == conversation]
        values = [rep.get(column) for rep in reps]
        if column == "progress_posts":
            return "/".join(str(value) for value in values)
        return f"{sum(bool(value) for value in values)}/{len(values)}"

    tables = []
    for columns, conversations in ((TASK_COLUMNS, ("group", "direct")), (SCHEDULE_COLUMNS, ("group-schedule",)),
                                   (COMPLETION_DIRECT_COLUMNS, ("completion-direct",)),
                                   (COMPLETION_GROUP_COLUMNS, ("completion-group",))):
        ran = [c for c in conversations if any(rep["conversation"] == c for rep in run["reps"])]
        if not ran:
            continue
        lines = ["| conversation | " + " | ".join(columns) + " |\n|---|" + "---|" * len(columns)]
        for conversation in ran:
            for label, data in (("this run", run), ("base", base)):
                if data is not None:
                    lines.append(f"| {conversation} ({label}) | "
                                 + " | ".join(cell(data, conversation, c) for c in columns) + " |")
        tables.append("\n".join(lines))
    return "\n\n".join(tables)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--checkout", help="run this checkout's server (the baseline); default this one")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--manager-model")
    parser.add_argument("--manager-effort")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--conversations", default="group,direct",
                        help="comma-separated: group, direct, group-schedule, completion-direct, completion-group "
                             "(completion: both of the last two)")
    parser.add_argument("--max-budget-usd", type=float, default=2.0, help="each Claude Code run's cap")
    parser.add_argument("--claude", default=shutil.which("claude"), help="the Claude Code binary")
    parser.add_argument("--timeout", type=float, default=900, help="seconds a conversation waits for its task's end")
    parser.add_argument("--pg-url", default=os.environ.get("LOOPER_EVAL_DATABASE_URL"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-text", action="store_true", help="leave the posts' text out of the JSON")
    parser.add_argument("--out", help="the run's JSON; default evals/runs/<time>-lark-group.json")
    parser.add_argument("--baseline", help="a run's JSON to compare with")
    args = parser.parse_args()
    if not args.pg_url:
        parser.error("give --pg-url or LOOPER_EVAL_DATABASE_URL (a disposable PostgreSQL server)")
    if args.port == 8767:
        parser.error("8767 is the operator's port")
    conversations = [c for part in args.conversations.split(",") for c in ALIASES.get(part, part).split(",") if c]
    if not conversations or set(conversations) - CHATS.keys():
        parser.error(f"--conversations: choose from {', '.join(CHATS)}")
    if args.checkout:
        sys.path.insert(0, str(Path(args.checkout).resolve()))
    sys.path.insert(1, str(HERE / "tests"))
    spec = importlib.util.spec_from_file_location("lark_cli_stand_in", HERE / "tests" / "lark_cli_stand_in.py")
    stand_in = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(stand_in)
    checkout = Path(args.checkout).resolve() if args.checkout else HERE
    reps = []
    for rep in range(1, args.reps + 1):
        for conversation in conversations:
            with tempfile.TemporaryDirectory(prefix="looper-lark-group-") as tmp:
                reps.append({"rep": rep, **run_once(args, stand_in, Path(tmp), conversation)})
            print(f"rep {rep} {conversation}: " + json.dumps({k: v for k, v in reps[-1].items() if k != "posts"},
                                                             ensure_ascii=False), flush=True)
    import subprocess
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    run = {"schema": SCHEMA, "commit": commit, "model": args.model, "manager_model": args.manager_model,
           "manager_effort": args.manager_effort, "reps": reps}
    out = Path(args.out) if args.out else HERE / "evals" / "runs" / time.strftime("%Y%m%d-%H%M%S-lark-group.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8")) if args.baseline else None
    print(f"Run written to {out}\n\n" + table(run, base))
    return 0 if all(rep["passed"] for rep in reps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
