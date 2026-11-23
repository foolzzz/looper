"""Confirmed memory follows the real CLI server, operator HTTP API and vault git path."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper.agents import memory_model as model
from looper.db import memory as db_memory
from tests import memory_support as sup
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, timeout=10).stdout.strip()


async def _seed(url: str, repo: Path, *, enabled: bool) -> tuple[str, str]:
    from looper.agents import memory_write

    conn = await asyncpg.connect(url)
    try:
        settings = model.MemorySettings(
            root="mem", enabled=enabled, branch="main", commit_prefix="mem",
            rules_files=("mem/AGENTS.md",), targets={model.Kind.PREFERENCE: model.Target(
                model.Shape.NOTE, folders=("prefs",), index="prefs/_index.md")})
        for key, value in (("knowledge_vault", {"path": str(repo)}), ("memory_write", settings.to_json())):
            await conn.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2) "
                               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, json.dumps(value))
        candidate_id = await sup.candidate(conn)
        row = dict(await conn.fetchrow("SELECT * FROM memory_candidates WHERE id = $1", candidate_id))
        row["title"] = "Weekly reports: conclusion first"
        async with db_memory.transaction(conn) as transaction:
            revised = await db_memory.add_revision(
                transaction, candidate_id, action=model.Action.DRAFT, actor=model.Actor.SYSTEM,
                via=model.Via.SYSTEM, made_by=model.MadeBy.DRAFT, changes=[sup.NOTE, sup.INDEX],
                title=row["title"], subject=memory_write.subject(settings, row), diff_stats={},
                note_path=sup.NOTE["path"], pending_days=7, now=datetime.now(timezone.utc))
            if not enabled:
                await db_memory.transition(
                    transaction, candidate_id, model.State.CONFIRMED, action=model.Action.CONFIRM,
                    actor=model.Actor.USER, via=model.Via.DASHBOARD, content_hash=revised["content_hash"],
                    now=datetime.now(timezone.utc))
        return model.format_id(candidate_id), revised["content_hash"]
    finally:
        await conn.close()


def _wait(api, candidate: str, state: str) -> dict:
    deadline = time.monotonic() + 25
    while True:
        found = api.post("/api/v1/memory/get", {"candidate": candidate})["result"]["candidate"]
        if found["state"] == state:
            return found
        assert time.monotonic() < deadline, found
        time.sleep(0.1)


@pytest.mark.parametrize("enabled", [False, True], ids=["default-off", "confirm-block-retry"])
def test_memory_writes_through_serve(monkeypatch, tmp_path, enabled):
    monkeypatch.setattr(runner, "_SERVE", "import sys\nfrom looper.cli import main\n"
                        'sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], '
                        '"--runtime-root", sys.argv[2]]))\n')
    repo = sup.synthetic_vault(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    home, binaries, runtime = (tmp_path / name for name in ("home", "bin", "runtime"))
    home.mkdir()
    binaries.mkdir()
    for name in ("claude", "codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "coco", "lark-cli"):
        script = binaries / name
        script.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(76)\n")
        script.chmod(0o755)
    with fresh_database_without_recording(str(DSN)) as (url, _):
        candidate, digest = asyncio.run(_seed(url, repo, enabled=enabled))
        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=binaries, home=home,
                                   notes_root=home / "notes", log_path=tmp_path / "serve.log") as api:
            assert not api.base_url.endswith(":8767")
            if not enabled:
                # A persisted confirmation must remain inert while the switch is off.
                time.sleep(0.4)
                found = _wait(api, candidate, "confirmed")
                assert [event["action"] for event in found["events"]] == ["propose", "draft", "confirm"]
                assert _git(repo, "rev-parse", "HEAD") == before
                assert not (repo / sup.NOTE["path"]).exists()
                assert not (runtime / "vault-worktrees").exists()
                return
            target = repo / sup.NOTE["path"]
            target.write_text("An operator's unsaved change.\n")
            api.post("/api/v1/memory/confirm", {"candidate": candidate, "content_hash": digest})
            blocked = _wait(api, candidate, "blocked")
            assert blocked["reason"] == "dirty_target"
            assert _git(repo, "rev-parse", "HEAD") == before
            assert target.read_text() == "An operator's unsaved change.\n"
            target.unlink()
            api.post("/api/v1/memory/retry", {"candidate": candidate})
            written = _wait(api, candidate, "written")
            assert written["commit"] == _git(repo, "rev-parse", "HEAD") != before
            assert target.read_text() == sup.NOTE["content"]
            assert sup.INDEX["line"] in (repo / sup.INDEX["path"]).read_text().splitlines()
            assert _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines() == [
                sup.INDEX["path"], sup.NOTE["path"]]
            assert f"Looper-Candidate: {candidate}" in _git(repo, "show", "-s", "--format=%B", "HEAD")
            assert not _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/looper/memory/")
            assert len(_git(repo, "worktree", "list", "--porcelain").split("worktree ")) == 2
            assert (repo / "mine/notes.md").read_text() == "Personal synthetic note\n"
