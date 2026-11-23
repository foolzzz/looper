"""The real worktree/commit/fast-forward path, confined to temporary synthetic git repositories."""

import asyncio
import subprocess
import threading
from datetime import datetime, timezone

import pytest

from looper.agents import memory_model as model, memory_settings, memory_write as writer, vault
from looper.db import memory as store
from tests import memory_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


async def setup(pool, repo, tmp_path, *, changes=None, statement="Conclusion first", kind="preference"):
    settings = await sup.configure_write(repo, pool)
    cid = await sup.candidate(pool, statement=statement, kind=kind)
    row = {"id": cid, "kind": kind, "title": statement}
    async with store.transaction(pool) as conn:
        revised = await store.add_revision(conn, cid, action="draft", actor="system", via="system", made_by="draft",
            changes=changes or [sup.NOTE, sup.INDEX], title=statement, subject=writer.subject(settings, row),
            diff_stats={}, note_path=next((c["path"] for c in changes or [sup.NOTE] if c["op"] == "create_note"), None),
            pending_days=7, now=datetime.now(timezone.utc))
    await sup.move(pool, cid, "confirmed", "confirm", actor="user", via="dashboard",
                   content_hash=revised["content_hash"])
    return cid


repo_at = sup.synthetic_vault


def test_success_preserves_unrelated_staged_work_and_verifies_trailers(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)
    (repo / "other.md").write_text("operator's staged change\n")
    git(repo, "add", "--", "other.md")

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        service = writer.MemoryWriteService(tmp_path / "runtime", db=pool)
        assert await service.run_once()
        found = await store.get(cid, db=pool)
        row = found["candidate"]
        assert row["state"] == "written" and row["commit_sha"] == git(repo, "rev-parse", "HEAD")
        assert (repo / sup.NOTE["path"]).read_text() == sup.NOTE["content"]
        assert set(git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()) == {
            sup.NOTE["path"], sup.INDEX["path"]}
        message = git(repo, "log", "-1", "--format=%B")
        assert message.startswith(f"mem: {model.format_id(cid)} preference Conclusion first\n")
        assert f"Looper-Content-Sha256: {row['content_hash']}" in message
        assert "Looper-Revision: 1" in message and "Looper-Attempt: 1" in message
        assert "ou_operator_synthetic" not in message
        assert git(repo, "diff", "--cached", "--name-only") == "other.md"
        assert git(repo, "branch", "--list", "looper/memory/*") == ""
        assert len(git(repo, "worktree", "list", "--porcelain").split("worktree ")) == 2

    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("problem,reason", [("dirty", "dirty_target"), ("untracked", "dirty_target"),
    ("ignored", "dirty_target"),
    ("detached", "detached_head"), ("branch", "branch_mismatch"), ("merge", "operation_in_progress"),
    ("hook", "hook_rejected"), ("hook_bytes", "commit_mismatch"), ("hook_path", "commit_mismatch"),
    ("hook_subject", "commit_mismatch"), ("hook_mode", "commit_mismatch")])
def test_blocked_writes_preserve_main_checkout_and_confirmation(monkeypatch, tmp_path, problem, reason):
    repo = repo_at(tmp_path)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        if problem == "dirty":
            (repo / sup.INDEX["path"]).write_text("## Index\nunsaved\n")
        elif problem in ("untracked", "ignored"):
            (repo / sup.NOTE["path"]).write_text("operator's new note\n")
            if problem == "ignored":
                (repo / ".gitignore").write_text(sup.NOTE["path"] + "\n")
        elif problem == "detached":
            git(repo, "checkout", "--detach", "-q")
        elif problem == "branch":
            git(repo, "checkout", "-qb", "other")
        elif problem == "merge":
            (repo / ".git/MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD") + "\n")
        else:
            hook = repo / (".git/hooks/commit-msg" if problem == "hook_subject" else ".git/hooks/pre-commit")
            hook.write_text("#!/bin/sh\n" + {"hook": "exit 1\n",
                "hook_bytes": "printf 'hook changed\\n' >> mem/prefs/weekly.md\ngit add -- mem/prefs/weekly.md\n",
                "hook_path": "printf 'hook extra\\n' > extra.md\ngit add -- extra.md\n",
                "hook_subject": "sed -i.bak '1s/.*/changed subject/' \"$1\"\n",
                "hook_mode": "chmod +x mem/prefs/weekly.md\ngit add -- mem/prefs/weekly.md\n"}[problem])
            hook.chmod(0o755)
        before_head = git(repo, "rev-parse", "HEAD")
        before_status = git(repo, "status", "--porcelain", "--untracked-files=all")
        assert await writer.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert (row["state"], row["reason"]) == ("blocked", reason)
        assert row["confirmed_event_id"] and row["attempts"] == 1
        assert git(repo, "rev-parse", "HEAD") == before_head
        assert git(repo, "status", "--porcelain", "--untracked-files=all") == before_status

    run_db(check, monkeypatch, timeout=40)


def test_disabled_writer_does_no_git_no_state_changes_or_cleanup(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        await pool.execute("UPDATE system_settings SET value = replace(value, 'true', 'false') WHERE key = 'memory_write'")
        monkeypatch.setattr(vault, "_git", lambda *a: pytest.fail("git used with memory_write disabled"))
        service = writer.MemoryWriteService(tmp_path / "runtime", db=pool)
        assert not await service.run_once()
        await service.reconcile()
        await service.cleanup()
        assert (await store.get(cid, db=pool))["candidate"]["state"] == "confirmed"
        assert not (tmp_path / "worktrees").exists()

    run_db(check, monkeypatch, timeout=30)


def test_two_candidates_can_append_to_the_same_index_without_binding_its_base(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)

    async def check(pool):
        first = await setup(pool, repo, tmp_path, changes=[sup.INDEX])
        second = await setup(pool, repo, tmp_path, statement="Another memory", changes=[{
            **sup.INDEX, "line": "- [[another]] another conclusion"}])
        service = writer.MemoryWriteService(tmp_path / "runtime", db=pool)
        assert await service.run_once() and await service.run_once()
        assert [(await store.get(cid, db=pool))["candidate"]["state"] for cid in (first, second)] == ["written"] * 2
        assert (repo / sup.INDEX["path"]).read_text().splitlines() == [
            "## Index", sup.INDEX["line"], "- [[another]] another conclusion"]

    run_db(check, monkeypatch, timeout=50)


@pytest.mark.parametrize("touch_target", [False, True])
def test_main_advancing_rebases_only_when_targets_are_unchanged(monkeypatch, tmp_path, touch_target):
    repo = repo_at(tmp_path)
    real_git = vault._git
    advanced = False

    def moving_git(root, *args):
        nonlocal advanced
        result = real_git(root, *args)
        if "commit" in args and root != repo and not advanced:
            advanced = True
            path = sup.INDEX["path"] if touch_target else "other.md"
            (repo / path).write_text("## Index\nadvanced\n" if touch_target else "advanced\n")
            git(repo, "add", "--", path)
            git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "operator advanced")
        return result

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        monkeypatch.setattr(vault, "_git", moving_git)
        assert await writer.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        found = await store.get(cid, db=pool)
        if touch_target:
            assert found["candidate"]["state"] == "drafting"
            assert not found["candidate"]["confirmed_event_id"]
            assert any(e["action"] == "conflict" and e["note"] == "target_changed" for e in found["events"])
            assert not (repo / sup.NOTE["path"]).exists()
        else:
            assert found["candidate"]["state"] == "written"
            assert git(repo, "log", "-2", "--format=%s").splitlines()[-1] == "operator advanced"
    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("after_merge", [False, True])
def test_restart_recovers_a_committed_merge_or_retries_an_unmerged_attempt(monkeypatch, tmp_path, after_merge):
    repo = repo_at(tmp_path)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        row = await sup.move(pool, cid, "writing", "claim")
        settings, _ = await memory_settings.load(pool)
        if after_merge:
            config, _ = await vault.load()
            found = await store.get(cid, db=pool)
            found["confirmation"] = dict(await pool.fetchrow(
                "SELECT * FROM memory_candidate_events WHERE id=$1", row["confirmed_event_id"]))
            commit = await asyncio.to_thread(writer._write, config, settings, found, repo, tmp_path / "runtime")
        else:
            worktree, branch = writer._names(row, repo, settings, tmp_path / "runtime")
            worktree.parent.mkdir(parents=True)
            git(repo, "worktree", "add", "-b", branch, str(worktree), "HEAD")
            (worktree / "uncommitted.md").write_text("interrupted work\n")
        service = writer.MemoryWriteService(tmp_path / "runtime", db=pool)
        await service.reconcile()
        await service.cleanup()
        found = await store.get(cid, db=pool)
        assert found["candidate"]["state"] == ("written" if after_merge else "confirmed")
        assert found["candidate"]["attempts"] == 1
        if after_merge:
            assert found["candidate"]["commit_sha"] == commit
        else:
            assert not worktree.exists() and not git(repo, "branch", "--list", branch)
            assert await service.run_once()
            assert (await store.get(cid, db=pool))["candidate"]["state"] == "written"
        assert len(git(repo, "log", "--format=%s").splitlines()) == 2
    run_db(check, monkeypatch, timeout=50)


def test_cleanup_timeout_after_merge_is_still_written_and_later_reaped(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)
    real_git = vault._git

    def delayed_cleanup(root, *args):
        if args[:2] == ("worktree", "remove"):
            raise subprocess.TimeoutExpired("synthetic cleanup", 30)
        return real_git(root, *args)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        monkeypatch.setattr(vault, "_git", delayed_cleanup)
        service = writer.MemoryWriteService(tmp_path / "runtime", db=pool)
        assert await service.run_once()
        assert (await store.get(cid, db=pool))["candidate"]["state"] == "written"
        assert git(repo, "branch", "--list", "looper/memory/*")
        monkeypatch.setattr(vault, "_git", real_git)
        await service.cleanup()
        assert not git(repo, "branch", "--list", "looper/memory/*")
    run_db(check, monkeypatch, timeout=40)


def test_source_deletion_waits_for_write_then_purges_written_metadata(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)
    entered, release = threading.Event(), threading.Event()
    real_write = writer._write

    def paused_write(*args):
        entered.set()
        assert release.wait(10)
        return real_write(*args)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        source = await pool.fetchval("SELECT manager_action_id FROM memory_candidate_sources WHERE candidate_id=$1", cid)
        monkeypatch.setattr(writer, "_write", paused_write)
        task = asyncio.create_task(writer.MemoryWriteService(tmp_path / "runtime", db=pool).run_once())
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            deletion = asyncio.create_task(pool.execute("DELETE FROM manager_actions WHERE id=$1", source))
            await asyncio.sleep(.05)
            assert not deletion.done()
        finally:
            release.set()
        assert await task
        await deletion
        row = (await store.get(cid, db=pool))["candidate"]
        assert row["state"] == "written" and row["purged_at"] is not None
        assert row["statement"] == "" and (repo / sup.NOTE["path"]).exists()
    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("lock_failures", [2, 3])
def test_only_index_lock_failures_get_bounded_git_retries(monkeypatch, tmp_path, lock_failures):
    repo = repo_at(tmp_path)
    real_run = vault._run_git
    tries = 0

    def locked_index(command, **kwargs):
        nonlocal tries
        if "add" in command and "worktree" not in command:
            tries += 1
            if tries <= lock_failures:
                return subprocess.CompletedProcess(command, 128, "", "fatal: index.lock already exists")
        return real_run(command, **kwargs)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        monkeypatch.setattr(vault, "_run_git", locked_index)
        assert await writer.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert tries == 3
        assert (row["state"], row["reason"]) == (("written", None) if lock_failures == 2 else ("blocked", "git_failed"))
    run_db(check, monkeypatch, timeout=40)


def test_duplicate_operations_drop_without_commit_or_confirmation(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)
    (repo / sup.INDEX["path"]).write_text("## Index\n" + sup.INDEX["line"] + "\n")
    git(repo, "add", "--", sup.INDEX["path"])
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "Already remembered")
    before = git(repo, "rev-parse", "HEAD")

    async def check(pool):
        cid = await setup(pool, repo, tmp_path, changes=[sup.INDEX])
        assert await writer.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert (row["state"], row["reason"], row["confirmed_hash"]) == ("dropped", "duplicate", None)
        assert git(repo, "rev-parse", "HEAD") == before
    run_db(check, monkeypatch, timeout=40)


def test_conflict_queues_only_one_automatic_redraft(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)

    async def check(pool):
        changes = [{**sup.INDEX, "under": "## Missing"}]
        cid = await setup(pool, repo, tmp_path, changes=changes)
        service = writer.MemoryWriteService(tmp_path / "runtime", db=pool)
        assert await service.run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert row["state"] == "drafting" and row["confirmed_hash"] is None
        settings, _ = await memory_settings.load(pool)
        row = await sup.revise_current(pool, cid, settings, changes=changes)
        await sup.move(pool, cid, "confirmed", "confirm", actor="user", via="dashboard", content_hash=row["content_hash"])
        assert await service.run_once()
        found = await store.get(cid, db=pool)
        assert (found["candidate"]["state"], found["candidate"]["reason"]) == ("conflict", "anchor_missing")
        assert sum(e["action"] == "redraft" for e in found["events"]) == 1
    run_db(check, monkeypatch, timeout=40)


def test_cancellation_keeps_the_cross_process_lock_until_git_and_database_finish(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)
    entered, release = threading.Event(), threading.Event()
    real_write = writer._write

    def paused(*args):
        entered.set()
        assert release.wait(10)
        return real_write(*args)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        monkeypatch.setattr(writer, "_write", paused)
        first = writer.MemoryWriteService(tmp_path / "runtime", db=pool)
        task = asyncio.create_task(first.run_once())
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            assert first.drain_waiting() == 1
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(.01)
            assert first.drain_waiting() == 1
            assert not await writer.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
            assert not task.done()  # the cancelled caller must not release an in-flight git operation's locks
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await store.get(cid, db=pool))["candidate"]["state"] == "written"
        assert first.drain_waiting() == 0
    run_db(check, monkeypatch, timeout=40)


def test_real_hook_timeout_kills_its_process_group_and_preserves_main(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        hook = repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nsleep 30\n")
        hook.chmod(0o755)
        real_run = vault._run_git

        def bounded_commit(command, **kwargs):
            if "commit" in command:
                with monkeypatch.context() as patch:
                    patch.setattr(vault, "GIT_TIMEOUT_SECONDS", .2)
                    return real_run(command, **kwargs)
            return real_run(command, **kwargs)

        monkeypatch.setattr(vault, "_run_git", bounded_commit)
        before = git(repo, "rev-parse", "HEAD")
        assert await writer.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert (row["state"], row["reason"]) == ("blocked", "git_timeout")
        assert git(repo, "rev-parse", "HEAD") == before
        assert not git(repo, "branch", "--list", "looper/memory/*")
    run_db(check, monkeypatch, timeout=40)


def test_ignore_probe_timeout_is_reported_as_git_timeout(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)
    real_run = subprocess.run

    def timed_out(command, *args, **kwargs):
        if "check-ignore" in command:
            raise subprocess.TimeoutExpired(command, 30)
        return real_run(command, *args, **kwargs)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        before = git(repo, "rev-parse", "HEAD")
        monkeypatch.setattr(subprocess, "run", timed_out)
        assert await writer.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert (row["state"], row["reason"]) == ("blocked", "git_timeout")
        assert git(repo, "rev-parse", "HEAD") == before
        assert not git(repo, "branch", "--list", "looper/memory/*")

    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("tamper", [False, True])
def test_recovery_matches_exact_trailers_and_stops_after_three_interruptions(monkeypatch, tmp_path, tamper):
    repo = repo_at(tmp_path)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        for attempt in range(3):
            await sup.move(pool, cid, "writing", "claim")
            if attempt < 2:
                await sup.move(pool, cid, "confirmed", "recover")
        if tamper:
            row = (await store.get(cid, db=pool))["candidate"]
            # Correct identity but wrong hash: never treat a forged/modified commit as a completed write.
            message = "Unrelated\n\n" + "\n".join(f"{key}: {value}" for key, value in {
                **writer._expected_trailers(row), "Looper-Content-Sha256": "0" * 64}.items())
            git(repo, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", message)
        await writer.MemoryWriteService(tmp_path / "runtime", db=pool).reconcile()
        row = (await store.get(cid, db=pool))["candidate"]
        assert (row["state"], row["reason"]) == ("blocked", "commit_mismatch" if tamper else "interrupted")
        assert row["confirmed_event_id"] and row["attempts"] == 3
    run_db(check, monkeypatch, timeout=40)


def test_vault_subdirectory_paths_and_lead_promotion_share_the_commit_transaction(monkeypatch, tmp_path):
    repo = repo_at(tmp_path)

    async def check(pool):
        await vault.update_settings({"path": str(repo / "mem")})
        await memory_settings.update_settings({"root": "", "enabled": True, "worktree_root": str(tmp_path / "trees"),
            "targets": {"decision": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"}}})
        settings, _ = await memory_settings.load(pool)
        lead_id = await sup.lead(pool)
        await pool.execute("UPDATE source_chats SET name = 'Synthetic project chat'")
        cid = await sup.candidate(pool, kind="decision", evidence=[store.Evidence(model.EvidenceKind.LEAD, lead_id=lead_id)])
        revision = await sup.revise_current(pool, cid, settings, changes=[{**sup.INDEX, "path": "prefs/_index.md"}])
        await sup.move(pool, cid, "confirmed", "confirm", actor="user", via="dashboard", content_hash=revision["content_hash"])
        assert await writer.MemoryWriteService(tmp_path / "runtime", db=pool).run_once()
        assert (await store.get(cid, db=pool))["candidate"]["state"] == "written"
        lead = await pool.fetchrow("SELECT * FROM message_leads WHERE id=$1", lead_id)
        assert (lead["state"], lead["promoted_kind"], lead["promoted_ref"]) == ("promoted", "vault_proposal", model.format_id(cid))
        assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "mem/prefs/_index.md"
        assert f"lead-{lead_id} · Synthetic project chat" in git(repo, "log", "-1", "--format=%B")
    run_db(check, monkeypatch, timeout=40)


@pytest.mark.parametrize("failure", ["vault_problem", "not_git", "git_timeout"])
def test_startup_blocks_interrupted_writes_when_preflight_cannot_run(monkeypatch, tmp_path, failure):
    repo = repo_at(tmp_path)

    async def check(pool):
        cid = await setup(pool, repo, tmp_path)
        await sup.move(pool, cid, "writing", "claim")
        if failure == "vault_problem":
            await pool.execute("UPDATE system_settings SET value = '{}' WHERE key='knowledge_vault'")
        elif failure == "not_git":
            (repo / ".git").rename(repo / ".git-synthetic-disabled")
        else:
            def timeout(*_):
                raise subprocess.TimeoutExpired("synthetic git", 30)
            monkeypatch.setattr(vault, "_git_root_sync", timeout)
        service = writer.MemoryWriteService(tmp_path / "runtime", db=pool)
        await service.run_once()
        row = (await store.get(cid, db=pool))["candidate"]
        assert (row["state"], row["reason"]) == ("blocked", failure)
        assert row["confirmed_event_id"] is not None
        assert not (repo / sup.NOTE["path"]).exists()
    run_db(check, monkeypatch, timeout=40)
