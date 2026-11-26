"""Blocked vault commits remain operator work until their preserved branch is handled."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import attention, vault_commit_attention
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, timeout=30).stdout.strip()


@pytest.fixture
def blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.defpath + os.pathsep + os.environ.get("PATH", ""))
    repo = tmp_path / "vault"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Vault Tester")
    git(repo, "config", "user.email", "vault@example.test")
    git(repo, "config", "core.hooksPath", str(repo / ".git/hooks"))
    (repo / "note.md").write_text("Initial\n")
    git(repo, "add", "--", "note.md")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "Initial")
    worktree = tmp_path / "worktrees" / "vault"
    worktree.parent.mkdir()
    branch = "looper/vault/synthetic-attempt"
    git(repo, "worktree", "add", "-b", branch, str(worktree))
    (worktree / "note.md").write_text("Confirmed\n")
    git(worktree, "add", "--", "note.md")
    git(worktree, "-c", "commit.gpgsign=false", "commit", "-qm", "Confirmed")
    result = SimpleNamespace(state="blocked", reason="merge_blocked", branch=branch, worktree=worktree,
                             commit=git(worktree, "rev-parse", "HEAD"))
    return repo, result


def test_blocked_commit_is_durable_in_needs_you_until_branch_is_merged(blocked, monkeypatch):
    repo, result = blocked
    from datetime import datetime, timezone
    from looper.agents import failures, idle_checks
    from looper.db import reminders, step_failures

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(failures, "typing_mode", AsyncMock(return_value=failures.FailureTyping.ACT))
    monkeypatch.setattr(step_failures, "list_holds", AsyncMock(return_value=[
        {"task_id": "held-task", "step_id": "held-step", "step_key": "Resume work", "failure_id": 1,
         "kind": "environment", "cause": "login", "created_at": now, "streak": 3}]))

    async def check(pool):
        reminder = await reminders.create(text="Check the application", due_at=now, timezone="UTC",
                                          channel_id="channel-lark", chat="ou_synthetic",
                                          created_channel="web", created_message_id=None)
        await reminders.settle(pool, reminder["id"], "uncertain", now=now, from_status="pending")
        await vault_commit_attention.record(result, repo=repo)
        # A duplicate notification updates one item, and persistence is not tied to a running writer.
        await vault_commit_attention.record(result, repo=repo)
        records = await pool.fetch("SELECT value FROM system_settings WHERE key LIKE 'vault_commit.blocked.%'")
        assert len(records) == 1 and json.loads(records[0]["value"])["commit"] == result.commit
        items, total = await attention._vault_commits(None)
        assert total == 1 and items[0]["facts"]["branch"] == result.branch
        assert items[0]["facts"]["worktree"] == str(result.worktree)
        assert items[0]["facts"]["reason"] == "merge_blocked"
        assert await attention.earliest("vault_commits") == items[0]
        summary = await attention.collect()
        kinds = [section["kind"] for section in summary["sections"]]
        assert kinds == [kind for kind in attention.KINDS if kind in kinds]
        assert kinds[-3:] == ["failure_holds", "reminders", "vault_commits"]
        for kind in kinds[-3:]:
            found, = [section for section in summary["sections"] if section["kind"] == kind]
            assert found["state"] == "ok" and found["count"] == 1
            assert await attention.earliest(kind) == found["items"][0]
        text = attention.summary_text(summary)
        for title in ("Resume work：login", "Check the application", result.branch):
            assert text.count(title) == 1
        assert idle_checks.select(summary, now=now) == []
        assert "知识库提交受阻" in attention.summary_text({"total": 1, "sections": [
            {"kind": "vault_commits", "state": "ok", "count": 1, "items": items}]})
        assert result.branch in attention.summary_text({"total": 1, "sections": [
            {"kind": "vault_commits", "state": "ok", "count": 1, "items": items}]})
        git(repo, "merge", "--ff-only", result.branch)
        assert await vault_commit_attention.pending() == []
        # Attention only reads: it does not remove the operator's preserved worktree or branch.
        assert result.worktree.exists() and git(repo, "rev-parse", result.branch) == result.commit

    run_db(check, monkeypatch, timeout=30)


def test_handled_branch_and_success_do_not_add_operator_work(blocked, monkeypatch):
    repo, result = blocked

    async def check(pool):
        success = SimpleNamespace(state="committed")
        await vault_commit_attention.record(success, repo=repo)
        assert await vault_commit_attention.pending() == []
        await vault_commit_attention.record(result, repo=repo)
        assert len(await vault_commit_attention.pending()) == 1  # positive control
        git(repo, "worktree", "remove", str(result.worktree))
        git(repo, "branch", "-D", result.branch)
        assert await vault_commit_attention.pending() == []

    run_db(check, monkeypatch, timeout=30)


def test_unreadable_preserved_branch_is_an_attention_error(blocked, monkeypatch):
    repo, result = blocked

    async def check(pool):
        await vault_commit_attention.record(result, repo=repo)
        assert len(await vault_commit_attention.pending()) == 1
        # A repository that becomes unavailable is not evidence that someone handled its commit.
        repo.rename(repo.with_name("unavailable"))
        summary = await attention.collect()
        section = next(item for item in summary["sections"] if item["kind"] == "vault_commits")
        assert section["state"] == "error"
        assert "知识库提交受阻：没能读取" in attention.summary_text(summary)

    run_db(check, monkeypatch, timeout=30)
