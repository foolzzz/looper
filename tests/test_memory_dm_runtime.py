"""Memory DMs follow a real CLI server; only Telegram's network is replaced by files."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper.agents import memory_model as model
from looper.db import memory as db_memory
from tests import memory_support as sup
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
OWNER = 700001


def _install_telegram(runtime: Path) -> None:
    """Subprocess boundary: keep real admission, ingress, outbound formatting and registry methods."""
    from looper.agents.channels.telegram import TelegramChannelAdapter

    class Bot:
        counter = 100

        def record(self, kind, fields):
            with (runtime / "dm-output.jsonl").open("a") as output:
                output.write(json.dumps({"kind": kind, **fields}, ensure_ascii=False) + "\n")

        async def send_message(self, **fields):
            self.counter += 1
            self.record("send", {**fields, "message_id": self.counter})
            return SimpleNamespace(message_id=self.counter)

        async def edit_message_text(self, **fields):
            self.record("edit", fields)

    async def start(adapter, bus):
        from telegram import Update

        adapter._bus, adapter._running = bus, True
        adapter._app = SimpleNamespace(bot=Bot(), updater=None)
        adapter._acknowledge = lambda update: None
        adapter._outbound_task = asyncio.create_task(adapter._outbound_loop())

        async def receive():
            consumed = 0
            while True:
                source = runtime / "dm-input.jsonl"
                lines = source.read_text().splitlines() if source.exists() else []
                for line in lines[consumed:]:
                    consumed += 1
                    item = json.loads(line)
                    message = {"message_id": consumed, "date": int(time.time()), "text": item["text"],
                               "chat": {"id": OWNER, "type": "private"},
                               "from": {"id": OWNER, "is_bot": False, "first_name": "Synthetic"}}
                    if item.get("reply_to"):
                        message["reply_to_message"] = {"message_id": int(item["reply_to"]),
                            "date": int(time.time()), "chat": message["chat"], "text": "Synthetic push"}
                    update = Update.de_json({"update_id": consumed, "message": message}, adapter._app.bot)
                    await adapter._on_message(update, None)
                await asyncio.sleep(.05)

        adapter._synthetic_incoming = asyncio.create_task(receive())

    async def stop(adapter):
        adapter._running = False
        tasks = [getattr(adapter, name, None) for name in ("_outbound_task", "_synthetic_incoming")]
        for task in tasks:
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
        adapter._app = adapter._bus = None

    TelegramChannelAdapter.start = start
    TelegramChannelAdapter.stop = stop


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, timeout=10).stdout.strip()


async def _seed(url, repo):
    conn = await asyncpg.connect(url)
    try:
        settings = model.MemorySettings(
            root="mem", enabled=True, branch="main", commit_prefix="mem", push_channel="channel-telegram",
            rules_files=("mem/AGENTS.md",), targets={model.Kind.PREFERENCE: model.Target(
                model.Shape.NOTE, folders=("prefs",), index="prefs/_index.md")})
        for key, value in (("knowledge_vault", {"path": str(repo)}), ("memory_write", settings.to_json())):
            await conn.execute("INSERT INTO system_settings (key,value) VALUES ($1,$2) "
                               "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", key, json.dumps(value))
        await conn.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) "
                           "VALUES ('channel-telegram','telegram','Synthetic bot',true,$1)",
                           json.dumps({"allowed_user_ids": [str(OWNER)], "operator_sender_id": str(OWNER)}))
        await conn.execute("INSERT INTO channel_secrets (channel_id,bot_token) "
                           "VALUES ('channel-telegram','700000:synthetic')")
        return settings
    finally:
        await conn.close()


async def _candidate(url, settings, suffix):
    from looper.agents import memory_write

    conn = await asyncpg.connect(url)
    try:
        number = await sup.candidate(conn)
        row = dict(await conn.fetchrow("SELECT * FROM memory_candidates WHERE id=$1", number))
        note = {**sup.NOTE, "path": f"mem/prefs/weekly{suffix}.md"}
        title = f"Conclusion first {suffix}".strip()
        async with db_memory.transaction(conn) as transaction:
            await db_memory.add_revision(transaction, number, action=model.Action.DRAFT, actor=model.Actor.SYSTEM,
                via=model.Via.SYSTEM, made_by=model.MadeBy.DRAFT, changes=[note, sup.INDEX], title=title,
                subject=memory_write.subject(settings, {**row, "title": title}), diff_stats={},
                note_path=note["path"], pending_days=7, now=datetime.now(timezone.utc))
        return model.format_id(number), note
    finally:
        await conn.close()


async def _snapshot(url, label):
    conn = await asyncpg.connect(url)
    try:
        number = model.parse_id(label)
        candidate = dict(await conn.fetchrow("SELECT * FROM memory_candidates WHERE id=$1", number))
        posts = [dict(row) for row in await conn.fetch("SELECT * FROM memory_posts WHERE candidate_id=$1 ORDER BY id", number)]
        return candidate, posts
    finally:
        await conn.close()


def _wait(check, detail):
    deadline = time.monotonic() + 35
    while True:
        found = check()
        if found:
            return found
        assert time.monotonic() < deadline, detail()
        time.sleep(.1)


@pytest.mark.parametrize("dirty", [False, True], ids=["confirm", "blocked-retry"])
def test_memory_dm_commit_and_blocked_retry_through_serve(monkeypatch, tmp_path, dirty):
    bootstrap = """import sys
from pathlib import Path
from tests.test_memory_dm_runtime import _install_telegram
_install_telegram(Path(sys.argv[2]))
from looper.cli import main
sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], "--runtime-root", sys.argv[2]]))
"""
    monkeypatch.setattr(runner, "_SERVE", bootstrap)
    repo = sup.synthetic_vault(tmp_path)
    initial = _git(repo, "rev-parse", "HEAD")
    home, binaries, runtime = (tmp_path / name for name in ("home", "bin", "runtime"))
    home.mkdir()
    binaries.mkdir()
    for name in ("claude", "codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "coco", "lark-cli"):
        binary = binaries / name
        binary.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(76)\n")
        binary.chmod(0o755)
    with fresh_database_without_recording(str(DSN)) as (url, _):
        settings = asyncio.run(_seed(url, repo))
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=binaries, home=home,
                                   notes_root=home / "notes", log_path=tmp_path / "serve.log") as api:
            assert not api.base_url.endswith(":8767")
            output = runtime / "dm-output.jsonl"

            def events():
                return [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []

            def reply(text, quote=None):
                with (runtime / "dm-input.jsonl").open("a") as destination:
                    destination.write(json.dumps({"text": text, "reply_to": quote}, ensure_ascii=False) + "\n")

            label, note = asyncio.run(_candidate(url, settings, ""))
            def snapshot():
                return asyncio.run(_snapshot(url, label))
            push = _wait(lambda: next((post for post in snapshot()[1] if post["kind"] == "push"
                                      and post["state"] == "sent"), None), snapshot)
            assert push["complete"] and push["external_id"]
            target = repo / note["path"]
            if dirty:
                target.write_text("Synthetic unsaved edit.\n")
            reply("记" if not dirty else f"记 {label}", push["external_id"])
            if dirty:
                _wait(lambda: snapshot()[0]["state"] == "blocked", snapshot)
                _wait(lambda: any(event["kind"] == "edit" and event.get("message_id") == int(push["external_id"])
                                  and "⚠️" in event["text"] for event in events()), events)
                assert snapshot()[0]["reason"] == "dirty_target"
                target.unlink()
                reply(f"重试 {label}")
            _wait(lambda: snapshot()[0]["state"] == "written", snapshot)
            _wait(lambda: any(event["kind"] == "edit" and event.get("message_id") == int(push["external_id"])
                              and "✅" in event["text"] for event in events()), events)
            assert target.read_text() == note["content"]
            assert snapshot()[0]["commit_sha"] == _git(repo, "rev-parse", "HEAD") != initial
            assert f"Looper-Candidate: {label}" in _git(repo, "show", "-s", "--format=%B", "HEAD")
            _wait(lambda: any(event["kind"] == "send" and label in event["text"] and "✅" in event["text"]
                              for event in events()), events)
            assert (repo / "mine/notes.md").read_text() == "Personal synthetic note\n"
            assert not _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/looper/memory/")
