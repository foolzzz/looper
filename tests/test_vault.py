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
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> vault.VaultConfig:
    # Use stock Git: local patched builds may automatically remove a competing index.lock.
    monkeypatch.setenv("PATH", os.defpath + os.pathsep + os.environ.get("PATH", ""))
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
    with pytest.raises(ValueError, match="symbolic link" if field == "writable" else "leaves the vault"):
        vault.check_config({"path": str(config.path), field: ["escape"] if field == "writable"
                           else "escape/rules.md"})


@pytest.mark.parametrize("folder", ["inner", "via/sub"])
def test_config_refuses_a_writable_folder_through_a_link_inside_the_vault(
    config: vault.VaultConfig, folder: str,
) -> None:
    # Writes walk real folders only, so a link is refused when the setting is saved, not at the first write.
    (config.path / "codex-mem/sub").mkdir()
    (config.path / "inner").symlink_to(config.path / "codex-mem", target_is_directory=True)
    (config.path / "via").symlink_to(config.path / "codex-mem", target_is_directory=True)
    with pytest.raises(ValueError, match=f"writable folder {folder} is or passes through a symbolic link"):
        vault.check_config({"path": str(config.path), "writable": [folder]})


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

    def paused(source, target, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        original(source, target, **kwargs)

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

    def fail(*args: object, **kwargs: object) -> None:
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
        "path or text. Up to limit paths (relative to the vault), most hits first, each with a snippet. "
        "Each vault call re-checks that this task's project still has the vault on.",
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
    from unittest.mock import Mock

    entered, release = threading.Event(), threading.Event()
    original = vault.os.replace
    replacements: list[str] = []

    def paused(source, target, **kwargs):
        fd = os.open(source, os.O_RDONLY, dir_fd=kwargs["src_dir_fd"])
        with os.fdopen(fd, "r") as stream:
            replacements.append(stream.read())
        if len(replacements) == 1:
            entered.set()
            assert release.wait(timeout=5)
        original(source, target, **kwargs)

    monkeypatch.setattr(vault.os, "replace", paused)
    monkeypatch.setattr(vault, "_git_root_sync", Mock(return_value=None))

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


@pytest.mark.parametrize(("name", "operator_name"), [
    ("a*.md", "ab.md"), ("a?.md", "ab.md"), ("[ab].md", "a.md"),
])
def test_git_pathspec_magic_names_commit_only_the_literal_note(
        config: vault.VaultConfig, name: str, operator_name: str) -> None:
    operator = config.path / "codex-mem" / operator_name
    operator.write_text("original")
    git(config.path, "add", "--", f"codex-mem/{operator_name}")
    git(config.path, "-c", "commit.gpgsign=false", "commit", "-qm", "operator baseline")
    operator.write_text("operator's uncommitted change")

    result = write(config, f"codex-mem/{name}")

    assert result["commit"]
    assert git(config.path, "show", "--name-only", "--format=", "HEAD") == f"codex-mem/{name}"
    assert git(config.path, "status", "--porcelain") == f"M codex-mem/{operator_name}"
    assert git(config.path, "diff", "--cached", "--name-only") == ""
    assert git(config.path, "diff", "--name-only") == f"codex-mem/{operator_name}"
    assert git(config.path, "show", f"HEAD:codex-mem/{operator_name}") == "original"
    assert operator.read_text() == "operator's uncommitted change"


@pytest.mark.parametrize("hidden", [".git/config", ".obsidian/x.md"])
def test_read_refuses_symlink_to_a_hidden_resolved_path(config: vault.VaultConfig, hidden: str) -> None:
    target = config.path / hidden
    if hidden.startswith(".obsidian"):
        target.parent.mkdir()
        target.write_text("synthetic private settings")
    (config.path / "codex-mem/leak.md").symlink_to(target)
    with pytest.raises(OpError, match="points into a hidden path") as caught:
        asyncio.run(vault.read(config, "codex-mem/leak.md"))
    assert caught.value.code is ErrorCode.FORBIDDEN


def test_write_refuses_parent_swapped_for_symlink_at_fd_open(
        config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = config.path / "codex-mem/sub"
    parent.mkdir()
    outside = config.path.parent / "outside"
    original_open = os.open
    swapped = False

    def swap_and_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "sub" and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(parent.with_name("original-sub"))
            parent.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(vault.os, "open", swap_and_open)
    with pytest.raises(OpError) as caught:
        write(config, "codex-mem/sub/new.md")
    assert caught.value.code is ErrorCode.FORBIDDEN
    assert swapped, "the race must run at the directory-fd walk, after initial validation"
    assert not (outside / "new.md").exists()
    assert not (parent.with_name("original-sub") / "new.md").exists()


def test_write_refuses_saved_writable_folder_replaced_with_internal_symlink(config: vault.VaultConfig) -> None:
    folder = config.path / "codex-mem"
    folder.rename(config.path / "original-codex-mem")
    folder.symlink_to(config.path / "mine", target_is_directory=True)
    with pytest.raises(OpError) as caught:
        write(config)
    assert caught.value.code is ErrorCode.FORBIDDEN
    assert not (config.path / "mine/new.md").exists()


def test_write_refuses_parent_symlink_without_writing_outside(config: vault.VaultConfig) -> None:
    outside = config.path.parent / "outside"
    (config.path / "codex-mem/sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OpError) as caught:
        write(config, "codex-mem/sub/new.md")
    assert caught.value.code is ErrorCode.FORBIDDEN
    assert list(outside.iterdir()) == []


def test_writes_from_separate_event_loops_share_one_transaction_lock(
        config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    first = write(config)
    barrier = threading.Barrier(3)
    entered, release, contenders = threading.Event(), threading.Event(), threading.Event()
    original_replace = os.replace
    results: list[dict | BaseException] = []
    mutex, attempts_mutex = threading.Lock(), threading.Lock()
    attempts = 0

    class ObservedLock:
        def __enter__(self):
            nonlocal attempts
            with attempts_mutex:
                attempts += 1
                if attempts == 2:
                    contenders.set()
            mutex.acquire()
            return self

        def __exit__(self, *args):
            mutex.release()

    monkeypatch.setitem(vault._LOCKS, str(config.path), ObservedLock())

    def paused_replace(source, target, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_replace(source, target, **kwargs)

    def writer(content: str) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(write(config, content=content, expected=first["sha256"]))
        except BaseException as exc:
            results.append(exc)

    monkeypatch.setattr(vault.os, "replace", paused_replace)
    threads = [threading.Thread(target=writer, args=(text,), daemon=True) for text in ("one", "two")]
    for thread in threads:
        thread.start()
    try:
        barrier.wait(timeout=5)
        assert entered.wait(5)
        assert contenders.wait(5), "both event loops must reach the same transaction lock"
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads), "writers must finish across independent event loops"
    successes = [result for result in results if isinstance(result, dict)]
    errors = [result for result in results if isinstance(result, OpError)]
    assert len(successes) == 1, results
    assert len(errors) == 1 and errors[0].code is ErrorCode.CONFLICT, results
    assert successes[0]["commit"]
    assert git(config.path, "rev-list", "--count", f"{first['commit']}..HEAD") == "1"


def test_git_index_lock_released_during_retry_commits(
        config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = config.path / ".git/index.lock"
    lock.write_text("synthetic competing git operation")
    timer = threading.Timer(0.3, lambda: lock.unlink(missing_ok=True))
    original_run = vault._run_git
    started = False
    failed_attempts = 0

    def record_run(args, **kwargs):
        nonlocal started, failed_attempts
        result = original_run(args, **kwargs)
        if "add" in args and result.returncode and "index.lock" in result.stderr:
            failed_attempts += 1
            if not started:
                # Start only after Git really encounters the lock, independent of process startup speed.
                started = True
                timer.start()
        return result

    monkeypatch.setattr(vault, "_run_git", record_run)
    try:
        result = write(config)
    finally:
        if started:
            timer.cancel()
            timer.join(timeout=5)
        lock.unlink(missing_ok=True)
    assert started and failed_attempts >= 1, "a real index.lock failure must precede a successful retry"
    assert result["commit"] == git(config.path, "rev-parse", "HEAD"), result.get("note")
    assert git(config.path, "show", "--name-only", "--format=", "HEAD") == "codex-mem/new.md"


def test_git_index_lock_held_through_retries_keeps_written_note(config: vault.VaultConfig) -> None:
    head = git(config.path, "rev-parse", "HEAD")
    lock = config.path / ".git/index.lock"
    lock.write_text("synthetic competing git operation")
    try:
        result = write(config)
    finally:
        lock.unlink(missing_ok=True)
    assert result["commit"] is None, result
    assert result["git_exit"] != 0 and "index.lock" in result["note"]
    assert git(config.path, "rev-parse", "HEAD") == head
    assert (config.path / "codex-mem/new.md").read_text() == "new note"


@pytest.mark.parametrize("command", ["add", "commit"])
def test_git_index_lock_retries_exactly_three_times(
        config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fail(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, "", "fatal: unable to create index.lock")

    monkeypatch.setattr(vault, "_run_git", fail)
    monkeypatch.setattr(vault.time, "sleep", sleeps.append)
    with pytest.raises(subprocess.CalledProcessError):
        vault._git(config.path, command, "--", "codex-mem/new.md")
    assert len(calls) == 3
    assert sleeps == [0.25, 0.5]


@pytest.mark.parametrize(("command", "stderr"), [
    ("add", "synthetic ordinary failure"), ("rev-parse", "synthetic index.lock failure"),
])
def test_git_does_not_retry_other_failures(
        config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch, command: str, stderr: str) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fail(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", stderr)

    monkeypatch.setattr(vault, "_run_git", fail)
    monkeypatch.setattr(vault.time, "sleep", sleeps.append)
    with pytest.raises(subprocess.CalledProcessError):
        vault._git(config.path, command)
    assert len(calls) == 1
    assert sleeps == []


def test_search_stops_at_total_byte_cap(config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    for number in range(10):
        (config.path / "mine" / f"note-{number}.md").write_text("searchbudgetword " * 30)
    baseline = asyncio.run(vault.search(config, "searchbudgetword"))
    assert baseline["matches"] == 10 and not baseline["truncated"]
    monkeypatch.setattr(vault, "SEARCH_MAX_BYTES", 300)
    result = asyncio.run(vault.search(config, "searchbudgetword"))
    assert result["truncated"]
    assert 0 < result["scanned"] < baseline["scanned"]


def test_search_stops_at_elapsed_time_cap(config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    (config.path / "mine/note.md").write_text("searchbudgetword")
    assert asyncio.run(vault.search(config, "searchbudgetword"))["matches"] == 1
    monkeypatch.setattr(vault, "SEARCH_MAX_SECONDS", 0)
    result = asyncio.run(vault.search(config, "searchbudgetword"))
    assert result["truncated"] and result["scanned"] == 0 and result["results"] == []


def test_search_and_read_exclude_temporary_notes(config: vault.VaultConfig) -> None:
    (config.path / "codex-mem/.looper-1.tmp").write_text("syntheticsecret")
    (config.path / "codex-mem/.looper-1.tmp.md").write_text("syntheticsecret")
    (config.path / "mine/visible.md").write_text("syntheticsecret")
    result = asyncio.run(vault.search(config, "syntheticsecret"))
    assert [entry["path"] for entry in result["results"]] == ["mine/visible.md"]
    for name in (".looper-1.tmp", ".looper-1.tmp.md"):
        with pytest.raises(OpError) as caught:
            asyncio.run(vault.read(config, f"codex-mem/{name}"))
        assert caught.value.code is ErrorCode.INVALID


def test_git_uses_literal_pathspecs_and_the_allowed_child_environment(
        config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper import child_env

    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "LOOPER_DATABASE_URL", "PGPASSWORD",
                 "VAULT_SYNTHETIC_SECRET"):
        monkeypatch.setenv(name, "synthetic-private-value")
    calls: list[tuple[list[str], dict]] = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "synthetic output\n", "")

    monkeypatch.setattr(vault, "_run_git", run)
    assert vault._git(config.path, "add", "--", "codex-mem/a*.md") == "synthetic output"
    args, kwargs = calls[0]
    assert args == ["git", "--literal-pathspecs", "-C", str(config.path), "add", "--", "codex-mem/a*.md"]
    assert kwargs == {"env": {**child_env.inherited(), "GIT_TERMINAL_PROMPT": "0"}}
    assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "LOOPER_DATABASE_URL", "PGPASSWORD",
                "VAULT_SYNTHETIC_SECRET"} & kwargs["env"].keys()


@pytest.mark.parametrize("failure", [None, "fdopen_read", "fdopen_write", "fsync", "fchmod", "replace"])
def test_fd_write_closes_every_descriptor_and_cleans_temporary_files(
        config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch, failure: str | None) -> None:
    import errno

    path = "codex-mem/nested/deeper/note.md"
    first = write(config, path, "original")
    opened: list[int] = []
    original_open, original_fdopen = os.open, os.fdopen

    def tracking_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail(*args, **kwargs):
        raise OSError("synthetic fd failure")

    def fdopen(fd, mode, *args, **kwargs):
        if failure == f"fdopen_{'read' if mode == 'rb' else 'write'}":
            fail()
        return original_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(vault.os, "open", tracking_open)
    monkeypatch.setattr(vault.os, "fdopen", fdopen)
    if failure in ("fsync", "fchmod", "replace"):
        monkeypatch.setattr(vault.os, failure, fail)
    if failure:
        with pytest.raises(OSError, match="synthetic fd failure"):
            write(config, path, "changed", first["sha256"])
    else:
        result = write(config, path, "changed", first["sha256"])
        assert result["commit"] and (config.path / path).read_text() == "changed"
    assert len(opened) >= 5, "root, writable, nested parents and current note must all be opened"
    for fd in set(opened):
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF
    assert not list(config.path.rglob(".looper-*.tmp"))
    if failure:
        assert (config.path / path).read_text() == "original"


def test_a_git_timeout_ends_the_hooks_it_started(config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "31.4159"  # a sleep no other process runs
    hook = config.path / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\nsleep {marker} &\nsleep {marker}\n")
    hook.chmod(0o755)
    monkeypatch.setattr(vault, "GIT_TIMEOUT_SECONDS", 1)
    result = write(config)
    assert result["commit"] is None and result["git_status"] == "timed_out"
    assert (config.path / "codex-mem/new.md").read_text() == "new note"
    left = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True, timeout=10).stdout
    assert left.strip() == "", "the hook's processes outlived git's timeout"


def test_a_hook_printing_bytes_that_are_not_utf8_still_gives_a_result(config: vault.VaultConfig) -> None:
    hook = config.path / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nprintf '\\377\\376 not utf-8\\n' >&2\nexit 1\n")
    hook.chmod(0o755)
    result = write(config)
    assert result["commit"] is None and result["git_exit"] == 1
    assert result["note"].startswith("written, but the commit failed: ") and "not utf-8" in result["note"]
    assert (config.path / "codex-mem/new.md").read_text() == "new note"


def test_a_root_swapped_for_a_link_after_the_check_is_refused(config: vault.VaultConfig) -> None:
    real = config.path.with_name("vault-real")
    config.path.rename(real)
    config.path.symlink_to(real, target_is_directory=True)
    with pytest.raises(OpError) as caught:
        write(config)
    assert caught.value.code is ErrorCode.FORBIDDEN and "vault folder changed" in str(caught.value)
    assert not (real / "codex-mem/new.md").exists()


def test_a_parent_of_the_root_swapped_for_a_link_is_refused(config: vault.VaultConfig, tmp_path: Path) -> None:
    # The root itself is a real directory, but the path to it now passes through a link to a copy elsewhere.
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    (elsewhere / "vault/codex-mem").mkdir(parents=True)
    moved = tmp_path.with_name(f"{tmp_path.name}-moved")
    tmp_path.rename(moved)
    try:
        tmp_path.symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(OpError) as caught:
            write(config)
        assert caught.value.code is ErrorCode.FORBIDDEN
        assert not (elsewhere / "vault/codex-mem/new.md").exists()
    finally:
        tmp_path.unlink()
        moved.rename(tmp_path)


def test_a_saved_root_that_now_leads_elsewhere_no_longer_holds(
    config: vault.VaultConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from looper.db import system_settings

    stored = {vault.SETTING_KEY: json.dumps({"path": str(config.path), "writable": ["codex-mem"], "rules_file": None})}

    async def get_settings(keys, **kwargs):
        return {key: stored[key] for key in keys if key in stored}

    monkeypatch.setattr(system_settings, "get_settings", get_settings)
    assert asyncio.run(vault.load())[0] == vault.VaultConfig(config.path, ("codex-mem",), None)
    real = config.path.with_name("vault-real")
    config.path.rename(real)
    config.path.symlink_to(real, target_is_directory=True)
    loaded, problem = asyncio.run(vault.load())
    assert loaded is None and "now leads to" in problem
