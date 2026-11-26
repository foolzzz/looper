"""The ``memory_write`` setting (memory-write-path RFC, section 9.1): saving it against the knowledge vault, git and the
channels, reading it back checked, and the read-only preflight of what a write needs. Every vault here is a git
repository created in a temporary directory."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from looper.agents import memory_settings, vault
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """A vault repository with a memory folder, its rules, a preferences folder and index, a commitments page, and a
    personal folder: committed on main."""
    repo = tmp_path / "vault"
    for folder in ("mem/prefs", "mem/me", "mine"):
        (repo / folder).mkdir(parents=True)
    for name, text in (("mem/AGENTS.md", "rules\n"), ("mem/prefs/_index.md", "## Index\n"),
                       ("mem/me/commitments.md", "# Commitments\n"), ("mine/notes.md", "mine\n")):
        (repo / name).write_text(text)
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "looper-test@example.invalid")
    _git(repo, "config", "user.name", "Looper test")
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    return repo


def _body(**changes):
    body = {"enabled": True, "root": "mem", "rules_files": ["mem/AGENTS.md"],
            "targets": {"preference": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"},
                        "commitment": {"shape": "entry", "files": ["me/commitments.md"]}},
            "sources": {"manager": True}}
    body.update(changes)
    return body


async def _vault(path: Path, **fields):
    await vault.update_settings({"path": str(path), **fields})


def test_a_saved_setting_records_the_branch_and_the_commit_prefix_and_reads_back(monkeypatch, tmp_path):
    repo = _repo(tmp_path)

    async def check(pool):
        await _vault(repo)
        view = await memory_settings.update_settings(_body())
        assert (view["configured"], view["enabled"], view["problem"]) == (True, True, None)
        assert (view["settings"]["branch"], view["settings"]["commit_prefix"]) == ("main", "mem")
        stored = json.loads(await pool.fetchval("SELECT value FROM system_settings WHERE key = 'memory_write'"))
        assert stored == view["settings"]
        settings, problem = await memory_settings.load()
        assert problem is None and settings.to_json() == stored
        assert await memory_settings.enabled()
        given = await memory_settings.update_settings(_body(commit_prefix="memory", branch="main"))
        assert given["settings"]["commit_prefix"] == "memory"
        cleared = await memory_settings.update_settings({"root": None})
        assert cleared == {"configured": False, "enabled": False, "settings": None, "problem": None}
        assert not await memory_settings.enabled()

    run_db(check, monkeypatch, timeout=30)


def test_the_commit_prefix_is_the_memory_folders_first_folder_in_the_repository(monkeypatch, tmp_path):
    repo = _repo(tmp_path)

    async def check(pool):
        await _vault(repo / "mem")  # the knowledge vault is the memory folder itself
        view = await memory_settings.update_settings(_body(
            root="", rules_files=["AGENTS.md"],
            targets={"preference": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"}}))
        assert view["settings"]["commit_prefix"] == "mem"
        await _vault(repo)
        whole = await memory_settings.update_settings({"root": ""})
        assert whole["settings"]["commit_prefix"] == "vault"

    run_db(check, monkeypatch, timeout=30)


def test_a_setting_git_or_the_channels_cannot_hold_is_refused(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "mem").mkdir()

    async def check(pool):
        with pytest.raises(memory_settings.SettingsError, match="configure the knowledge vault first"):
            await memory_settings.update_settings(_body())
        await _vault(tmp_path / "plain")
        with pytest.raises(memory_settings.SettingsError, match="not in a git repository"):
            await memory_settings.update_settings({"root": "mem"})
        await _vault(repo)
        with pytest.raises(memory_settings.SettingsError, match="not the main checkout's branch"):
            await memory_settings.update_settings(_body(branch="release"))
        with pytest.raises(memory_settings.SettingsError, match="inside the vault's repository"):
            await _vault(repo / "mem")
            await memory_settings.update_settings(_body(root="", rules_files=[], targets={},
                                                        worktree_root=str(repo / "mine" / "trees")))
        await _vault(repo)
        with pytest.raises(memory_settings.SettingsError, match="not a Lark or Telegram bot"):
            await memory_settings.update_settings(_body(push_channel="nope"))
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, enabled, config) VALUES "
                           "('lark-mem', 'lark', true, '{}'), ('tg-mem', 'telegram', false, $1)",
                           json.dumps({"operator_sender_id": "12345"}))
        with pytest.raises(memory_settings.SettingsError, match="no operator id"):
            await memory_settings.update_settings(_body(push_channel="lark-mem"))
        with pytest.raises(memory_settings.SettingsError, match="turned off"):
            await memory_settings.update_settings(_body(push_channel="tg-mem"))
        await pool.execute("UPDATE channel_configs SET enabled = true WHERE channel_id = 'tg-mem'")
        assert (await memory_settings.update_settings(_body(push_channel="tg-mem")))["settings"][
            "push_channel"] == "tg-mem"
        with pytest.raises(memory_settings.SettingsError, match="overlaps the writable folder"):
            await _vault(repo, writable=["mem/prefs"])
            await memory_settings.update_settings(_body())

    run_db(check, monkeypatch, timeout=30)


def test_an_ignored_memory_path_or_a_detached_head_is_refused(monkeypatch, tmp_path):
    repo = _repo(tmp_path)

    async def check(pool):
        await _vault(repo)
        # A tracked page under an ignored folder can still be committed; new notes in an ignored folder cannot.
        (repo / ".gitignore").write_text("mem/me/\n")
        assert (await memory_settings.update_settings(_body()))["enabled"]
        (repo / ".gitignore").write_text("mem/prefs/*\n!mem/prefs/_index.md\n")
        with pytest.raises(memory_settings.SettingsError, match="git ignores mem/prefs"):
            await memory_settings.update_settings(_body())
        (repo / ".gitignore").unlink()
        _git(repo, "checkout", "-q", "--detach")
        with pytest.raises(memory_settings.SettingsError, match="detached HEAD"):
            await memory_settings.update_settings(_body())

    run_db(check, monkeypatch, timeout=30)


def test_a_setting_that_no_longer_holds_is_a_problem_when_read(monkeypatch, tmp_path):
    repo = _repo(tmp_path)

    async def check(pool):
        await _vault(repo)
        await memory_settings.update_settings(_body())
        (repo / "mem" / "me" / "commitments.md").unlink()
        view = await memory_settings.settings_view()
        assert (view["configured"], view["enabled"]) == (True, False)
        assert "no longer holds" in view["problem"] and view["settings"]["root"] == "mem"
        assert not await memory_settings.enabled()
        await vault.update_settings({"path": None})
        assert "knowledge vault does not hold" in (await memory_settings.settings_view())["problem"]

    run_db(check, monkeypatch, timeout=30)


def test_the_check_says_what_a_write_needs_and_changes_nothing(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    async def check(pool):
        result = await memory_settings.check(runtime_root=runtime)
        assert (result["configured"], result["ok"]) == (False, False)
        await _vault(repo)
        await memory_settings.update_settings(_body())
        before = _git(repo, "status", "--porcelain")
        result = await memory_settings.check(runtime_root=runtime)
        assert result["ok"] and result["enabled"], result
        assert {item["name"]: item["ok"] for item in result["checks"]} == {
            "setting": True, "git": True, "branch": True, "operation": True, "ignored": True,
            "worktree_root": True, "push_channel": True}
        worktree = next(item for item in result["checks"] if item["name"] == "worktree_root")
        assert worktree["detail"] == str(tmp_path.parent / f"{tmp_path.name}-vault-worktrees") and not (runtime / "vault-worktrees").exists()
        # A merge under way, another branch checked out, no runtime root.
        _git(repo, "checkout", "-q", "-b", "other")
        (Path(repo) / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
        result = await memory_settings.check(runtime_root=None)
        failed = {item["name"]: item["detail"] for item in result["checks"] if not item["ok"]}
        assert not result["ok"] and set(failed) == {"branch", "operation"}, failed
        assert "MERGE_HEAD" in failed["operation"] and "on other, not main" in failed["branch"]
        assert _git(repo, "status", "--porcelain") == before

    run_db(check, monkeypatch, timeout=30)
