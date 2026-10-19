"""Knowledge vault files: confined paths, bounded text, conflicts and single-note commits."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import threading
from pathlib import Path

import pytest

from looper.agents import vault
from looper.agents.ops import ErrorCode, OpError


def git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, timeout=30, env=env).stdout.strip()


@pytest.fixture
def config(tmp_path: Path) -> vault.VaultConfig:
    root = tmp_path / "vault"
    (root / "codex-mem").mkdir(parents=True)
    (root / "mine").mkdir()
    (tmp_path / "outside").mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Vault Tester")
    git(root, "config", "user.email", "vault@example.test")
    (root / "codex-mem/AGENTS.md").write_text("Keep one topic per note.")
    git(root, "add", "--", "codex-mem/AGENTS.md")
    git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "initial rules")
    return vault.check_config({"path": str(root), "writable": ["codex-mem/"],
                               "rules_file": "codex-mem/AGENTS.md"})


@pytest.mark.parametrize("change", [
    {"path": "relative"}, {"path": "/does-not-exist-vault-test"}, {"writable": ["missing"]},
    {"writable": ["../outside"]}, {"writable": [".git"]}, {"writable": ["codex-mem", "codex-mem/"]},
    {"writable": ["codex-mem/./nested"]}, {"writable": ["/absolute"]}, {"writable": [1]},
    {"writable": "codex-mem"}, {"unknown": True}, {"rules_file": ".git/config"},
])
def test_config_refuses_invalid_values(config: vault.VaultConfig, change: dict) -> None:
    with pytest.raises(ValueError):
        vault.check_config({"path": str(config.path), "writable": ["codex-mem"], **change})


def test_config_requires_a_directory(config: vault.VaultConfig) -> None:
    with pytest.raises(ValueError):
        vault.check_config({"path": str(config.path / "codex-mem/AGENTS.md")})


@pytest.mark.parametrize("field", ["writable", "rules_file"])
def test_config_refuses_symlinks_leaving_root(config: vault.VaultConfig, field: str) -> None:
    outside = config.path.parent / "outside"
    (outside / "rules.md").write_text("outside")
    (config.path / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="leaves the vault"):
        vault.check_config({"path": str(config.path), field: ["escape"] if field == "writable"
                           else "escape/rules.md"})


def test_config_normalizes_and_resolves(config: vault.VaultConfig) -> None:
    assert config.path.is_absolute()
    assert config.writable == ("codex-mem",)
    assert config.folders() == "codex-mem/"
    assert vault.check_config({"path": str(config.path), "writable": None}).writable == ()


@pytest.mark.parametrize("path", ["../outside/x.md", "/tmp/x.md", ".git/config", "a//b.md",
                                  "a/./b.md", "C:/x.md", "a\\b.md", ""])
def test_read_refuses_invalid_paths(config: vault.VaultConfig, path: str) -> None:
    with pytest.raises(OpError) as caught:
        asyncio.run(vault.read(config, path))
    assert caught.value.code is ErrorCode.INVALID


def test_read_truncates_utf8_and_hashes_whole_file(config: vault.VaultConfig) -> None:
    data = ("a" + "记" * 30_000).encode()
    (config.path / "mine/long.md").write_bytes(data)
    result = asyncio.run(vault.read(config, "./mine/long.md"))
    assert result == {"path": "mine/long.md", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                      "content": data[:vault.READ_MAX_BYTES].decode("utf-8", errors="ignore"), "truncated": True}


@pytest.mark.parametrize("data", [b"text\0binary", b"\xff", b"a" * 65536 + b"\xff"])
def test_read_refuses_non_text(config: vault.VaultConfig, data: bytes) -> None:
    (config.path / "mine/binary.md").write_bytes(data)
    with pytest.raises(OpError) as caught:
        asyncio.run(vault.read(config, "mine/binary.md"))
    assert caught.value.code is ErrorCode.INVALID


@pytest.mark.parametrize(("path", "code"), [("mine", ErrorCode.INVALID), ("missing.md", ErrorCode.NOT_FOUND)])
def test_read_requires_a_regular_existing_file(config: vault.VaultConfig, path: str, code: ErrorCode) -> None:
    with pytest.raises(OpError) as caught:
        asyncio.run(vault.read(config, path))
    assert caught.value.code is code


def test_read_allows_internal_symlink_but_refuses_escape(config: vault.VaultConfig) -> None:
    (config.path / "inside.md").symlink_to(config.path / "codex-mem/AGENTS.md")
    assert asyncio.run(vault.read(config, "inside.md"))["content"] == "Keep one topic per note."
    (config.path / "out.md").symlink_to(config.path.parent / "outside/note.md")
    with pytest.raises(OpError) as caught:
        asyncio.run(vault.read(config, "out.md"))
    assert caught.value.code is ErrorCode.FORBIDDEN


def test_search_all_words_order_limits_and_omissions(config: vault.VaultConfig) -> None:
    for name, text in {"alpha.md": "BETA " * 60, "second.md": "alpha beta", "third.md": "beta alpha",
                       "only.md": "alpha", "not.txt": "alpha beta"}.items():
        (config.path / name).write_text(text)
    (config.path / ".obsidian").mkdir()
    (config.path / ".obsidian/hide.md").write_text("alpha beta")
    (config.path / "link.md").symlink_to(config.path / "second.md")
    (config.path / "linked-dir").symlink_to(config.path / "mine", target_is_directory=True)
    (config.path / "big.md").write_text("alpha beta" + "x" * vault.SEARCH_FILE_MAX_BYTES)
    result = asyncio.run(vault.search(config, "ALPHA beta", 2))
    assert result["matches"] == 3
    assert [r["path"] for r in result["results"]] == ["alpha.md", "second.md"]
    assert result["results"][0]["hits"] == 60
    assert all(len(r["snippet"]) <= 200 for r in result["results"])
    assert not result["truncated"]


def test_search_empty_query_is_invalid(config: vault.VaultConfig) -> None:
    with pytest.raises(OpError) as caught:
        asyncio.run(vault.search(config, "   ", None))
    assert caught.value.code is ErrorCode.INVALID


def test_search_stops_at_file_cap(config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "SEARCH_MAX_FILES", 1)
    (config.path / "mine/a.md").write_text("topic")
    (config.path / "mine/b.md").write_text("topic")
    result = asyncio.run(vault.search(config, "topic", None))
    assert result["scanned"] == 1 and result["truncated"]


def write(config: vault.VaultConfig, path: str = "codex-mem/new.md", content: str = "new note",
          expected: str | None = None, reason: str = "why") -> dict:
    return asyncio.run(vault.write(config, path, content, expected, reason, task_id="task-1", who="planner"))


def test_write_commits_only_the_note_and_keeps_other_staged_changes(config: vault.VaultConfig) -> None:
    (config.path / "mine/staged.md").write_text("operator change")
    git(config.path, "add", "--", "mine/staged.md")
    result = write(config)
    assert result["commit"] == git(config.path, "rev-parse", "HEAD")
    assert git(config.path, "log", "-1", "--format=%s%n%an%n%ae") == \
        "looper: task-1 planner: why\nVault Tester\nvault@example.test"
    assert git(config.path, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == "codex-mem/new.md"
    assert git(config.path, "diff", "--cached", "--name-only") == "mine/staged.md"


@pytest.mark.parametrize(("path", "code"), [("mine/x.md", ErrorCode.FORBIDDEN),
                                          ("codex-mem/x.txt", ErrorCode.INVALID)])
def test_write_refuses_outside_writable_or_non_markdown(config: vault.VaultConfig, path: str, code: ErrorCode) -> None:
    with pytest.raises(OpError) as caught:
        write(config, path)
    assert caught.value.code is code
    if code is ErrorCode.FORBIDDEN:
        assert "codex-mem/" in str(caught.value)
    assert not (config.path / path).exists()


@pytest.mark.parametrize("expected", [None, "stale"])
def test_existing_note_requires_current_sha(config: vault.VaultConfig, expected: str | None) -> None:
    write(config)
    with pytest.raises(OpError) as caught:
        write(config, content="changed", expected=expected)
    assert caught.value.code is ErrorCode.CONFLICT
    assert (config.path / "codex-mem/new.md").read_text() == "new note"


def test_sha_for_missing_note_conflicts(config: vault.VaultConfig) -> None:
    with pytest.raises(OpError) as caught:
        write(config, expected="stale")
    assert caught.value.code is ErrorCode.CONFLICT


def test_identical_text_is_unchanged_without_commit(config: vault.VaultConfig) -> None:
    first = write(config)
    result = write(config, expected=first["sha256"])
    assert result["unchanged"] and result["commit"] is None
    assert git(config.path, "rev-parse", "HEAD") == first["commit"]


def test_concurrent_writes_with_one_sha_make_exactly_one_commit(config: vault.VaultConfig,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    first = write(config)
    entered, release = threading.Event(), threading.Event()
    original = vault.os.replace

    def paused(source, target):
        entered.set()
        assert release.wait(timeout=5)
        original(source, target)

    monkeypatch.setattr(vault.os, "replace", paused)

    async def race() -> list:
        tasks = [asyncio.create_task(vault.write(config, "codex-mem/new.md", text, first["sha256"], "why",
                                                 task_id="task-1", who="planner")) for text in ["one", "two"]]
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await asyncio.sleep(0.05)
        finally:
            release.set()
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = asyncio.run(race())
    assert sum(isinstance(r, dict) for r in results) == 1
    errors = [r for r in results if isinstance(r, OpError)]
    assert len(errors) == 1 and errors[0].code is ErrorCode.CONFLICT
    assert git(config.path, "rev-list", "--count", f"{first['commit']}..HEAD") == "1"


def test_replace_failure_leaves_original_and_no_tempfile(config: vault.VaultConfig,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    first = write(config)

    def fail(*args: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(vault.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        write(config, content="changed", expected=first["sha256"])
    assert (config.path / "codex-mem/new.md").read_text() == "new note"
    assert not list((config.path / "codex-mem").glob(".looper-*.tmp"))


def test_non_git_vault_is_written_without_commit(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    (root / "notes").mkdir(parents=True)
    config = vault.check_config({"path": str(root), "writable": ["notes"]})
    result = write(config, "notes/new.md")
    assert result["commit"] is None and "not a git repository" in result["note"]
    assert (root / "notes/new.md").read_text() == "new note"


def test_write_refuses_symlinked_subfolder_escape(config: vault.VaultConfig) -> None:
    (config.path / "codex-mem/escape").symlink_to(config.path.parent / "outside", target_is_directory=True)
    with pytest.raises(OpError) as caught:
        write(config, "codex-mem/escape/new.md")
    assert caught.value.code is ErrorCode.FORBIDDEN


def test_write_refuses_symlink_leaving_writable_folder(config: vault.VaultConfig) -> None:
    (config.path / "codex-mem/personal").symlink_to(config.path / "mine", target_is_directory=True)
    with pytest.raises(OpError) as caught:
        write(config, "codex-mem/personal/new.md")
    assert caught.value.code is ErrorCode.FORBIDDEN


def test_write_refuses_large_existing_note(config: vault.VaultConfig) -> None:
    (config.path / "codex-mem/new.md").write_bytes(b"a" * (vault.READ_MAX_BYTES + 1))
    with pytest.raises(OpError) as caught:
        write(config)
    assert caught.value.code is ErrorCode.FORBIDDEN


@pytest.mark.parametrize(("content", "reason"), [("a\0b", "why"), ("x" * 65537, "why"),
                                              ("text", ""), ("text", "a\nb"), ("text", "x" * 201)])
def test_write_validates_content_and_reason(config: vault.VaultConfig, content: str, reason: str) -> None:
    with pytest.raises(OpError) as caught:
        write(config, content=content, reason=reason)
    assert caught.value.code is ErrorCode.INVALID


def test_write_preserves_file_mode(config: vault.VaultConfig) -> None:
    first = write(config)
    target = config.path / "codex-mem/new.md"
    target.chmod(0o640)
    write(config, content="changed", expected=first["sha256"])
    assert target.stat().st_mode & 0o777 == 0o640


def test_tool_descriptions_and_guides_are_the_model_contract(config: vault.VaultConfig) -> None:
    from looper.agents.ops import vault as tools

    assert [t.description for t in tools.TOOLS] == [
        "Search the operator's knowledge vault (Markdown notes): every word of query, in any case, in a note's "
        "path or text. Up to limit paths (relative to the vault), most hits first, each with a snippet.",
        "Read a file of the knowledge vault by its path (relative to the vault): its text (the first 64 KiB) "
        "and the sha256 of the whole file, which looper_vault_write needs to change it.",
        "Write a Markdown note (its whole text) in a writable folder of the knowledge vault. expected_sha256: "
        "the sha256 looper_vault_read gave, null for a new note; a conflict means it changed: read it again. "
        "reason: one line, the commit's message. Looper commits each write.",
    ]
    assert tools.planner_guide(config) == (
        "Knowledge vault: looper_vault_search and looper_vault_read read the operator's notes; "
        "looper_vault_write writes notes under codex-mem/ only. Read them when earlier notes may help; "
        "when the task ends, Looper asks you what to keep.\n")
    assert tools.run_section(config) == (
        "## Knowledge vault\nlooper_vault_search and looper_vault_read read the operator's notes; "
        "looper_vault_write writes notes under codex-mem/ only. Read them when earlier notes may help. "
        "Most steps write nothing: the Planner keeps what lasts when the task ends.")
    readonly = vault.VaultConfig(config.path, (), None)
    assert tools.planner_guide(readonly) == (
        "Knowledge vault: looper_vault_search and looper_vault_read read the operator's notes (read only). "
        "Read them when earlier notes may help.\n")
    assert tools.run_section(readonly) == (
        "## Knowledge vault\nlooper_vault_search and looper_vault_read read the operator's notes (read only). "
        "Read them when earlier notes may help.")


def test_cancelled_write_keeps_the_lock_until_the_file_thread_finishes(config: vault.VaultConfig,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    entered, release = threading.Event(), threading.Event()
    original = vault.os.replace
    replacements: list[str] = []

    def paused(source, target):
        replacements.append(Path(source).read_text())
        if len(replacements) == 1:
            entered.set()
            assert release.wait(timeout=5)
        original(source, target)

    monkeypatch.setattr(vault.os, "replace", paused)
    monkeypatch.setattr(vault, "_git_root", AsyncMock(return_value=None))

    async def race() -> None:
        first = asyncio.create_task(vault.write(config, "codex-mem/race.md", "first", None, "why",
                                                task_id="task-1", who="planner"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            first.cancel()
            second = asyncio.create_task(vault.write(config, "codex-mem/race.md", "second", None, "why",
                                                     task_id="task-1", who="planner"))
            # A task scheduled after both writers proves that the second had a chance to take the lock.
            await asyncio.sleep(0.05)
            assert replacements == ["first"]
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
        with pytest.raises(OpError) as caught:
            await second
        assert caught.value.code is ErrorCode.CONFLICT

    asyncio.run(race())
