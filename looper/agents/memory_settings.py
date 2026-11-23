"""The ``memory_write`` system setting (memory-write-path RFC, section 9.1): reading it back checked, saving it, and
the read-only preflight ``check`` of what a write needs (sections 6.1 and 9.2).

The setting is local paths, so the configuration export leaves it out. With no row the feature is off. What needs the
filesystem only is ``memory_model.check_settings``; what needs git or the channels is here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import child_env
from ..db import channel_configs, system_settings
from . import memory_model as model, vault
from .message import LARK_CHANNEL_TYPES
from .operator_identity import OPERATOR_KEY

WORKTREE_FOLDER = "vault-worktrees"
# What a write finds in the main checkout's git directory while another operation is under way there.
OPERATION_FILES = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD")
_PUSH_CHANNEL_TYPES = (*LARK_CHANNEL_TYPES, "telegram")


class SettingsError(ValueError):
    """The setting cannot be saved; the message says why."""


def _git(root: Path, *args: str) -> tuple[int, str]:
    """``git -C root args``: its exit status and its first output line (``vault._run_git``'s limits)."""
    env = {**child_env.inherited(), "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = vault._run_git(["git", "--literal-pathspecs", "-C", str(root), *args], env=env)
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return done.returncode, done.stdout.strip()


@dataclass(frozen=True)
class GitFacts:
    toplevel: Path | None
    branch: str | None  # None when HEAD is detached
    ignored: tuple[str, ...]  # the memory paths git ignores, relative to the knowledge vault
    operations: tuple[str, ...]  # the operations under way in the main checkout
    ignore_error: str | None = None  # git could not say what it ignores


def _ignored(toplevel: Path, paths: list[Path], *, by_patterns: bool) -> set[Path]:
    """Which of ``paths`` git ignores: by its patterns alone (``by_patterns``: a file not added yet), or as a commit
    would see them (a tracked file never is). Paths go on standard input, which check-ignore reads literally (it
    takes no pathspec magic). ``RuntimeError`` when git cannot answer."""
    if not paths:
        return set()
    command = ["git", "-C", str(toplevel), "check-ignore", "--stdin", "-z", *(["--no-index"] if by_patterns else [])]
    try:
        done = subprocess.run(command, input="".join(f"{path}\0" for path in paths).encode(), capture_output=True,
                              env={**child_env.inherited(), "GIT_TERMINAL_PROMPT": "0"},
                              timeout=vault.GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"git check-ignore could not run ({type(error).__name__})") from error
    if done.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore failed (exit {done.returncode})")
    return {Path(name) for name in done.stdout.decode("utf-8", errors="replace").split("\0") if name}


# A note a write would add, to ask git whether a folder's new notes are ignored.
_PROBE = "looper-new-note.md"


def _memory_paths(settings: model.MemorySettings) -> list[tuple[str, bool]]:
    """The memory folder, its target folders (True: a write adds notes there) and its target pages (False: a write
    changes them), relative to the knowledge vault."""
    def under(path: str) -> str:
        return f"{settings.root}/{path}" if settings.root else path

    folders = [settings.root or ".", *(under(folder) for target in settings.targets.values()
                                       for folder in target.folders)]
    pages = [under(page) for target in settings.targets.values()
             for page in (*([target.index] if target.index else []), *target.files)]
    return [(folder, True) for folder in folders] + [(page, False) for page in pages]


def git_facts(config: vault.VaultConfig, settings: model.MemorySettings) -> GitFacts:
    """What git says about the knowledge vault: its repository, the main checkout's branch, which memory paths it
    ignores and which operations are under way. Blocking: call it in a thread."""
    found = vault._git_root_sync(config.path)
    if found is None:
        return GitFacts(None, None, (), ())
    toplevel = Path(os.path.realpath(found))
    code, branch = _git(toplevel, "symbolic-ref", "--short", "-q", "HEAD")
    # A folder: would git ignore a new note in it (by its patterns alone)? A page: is it ignored as a commit sees it
    # (a tracked page never is)?
    paths = _memory_paths(settings)
    probes = {(config.path / path).resolve() / _PROBE: path for path, folder in paths if folder}
    pages = {(config.path / path).resolve(): path for path, folder in paths if not folder}
    ignored: list[str] = []
    ignore_error = None
    try:
        found = _ignored(toplevel, list(probes), by_patterns=True) | _ignored(toplevel, list(pages), by_patterns=False)
        ignored = [name for where, name in (*probes.items(), *pages.items()) if where in found]
    except RuntimeError as error:
        ignore_error = str(error)
    operations = []
    for name in OPERATION_FILES:
        status, where = _git(toplevel, "rev-parse", "--git-path", name)
        if status == 0 and where and (toplevel / where).exists():
            operations.append(name)
    return GitFacts(toplevel, branch if code == 0 and branch else None, tuple(ignored), tuple(operations),
                    ignore_error)


def commit_prefix(config: vault.VaultConfig, root: str, toplevel: Path) -> str:
    """The memory folder's first folder below the repository (``codex-mem``), or ``vault`` when the memory folder is
    the repository itself: what each commit's message starts with, as the vault's rules ask."""
    relative = (config.path / root).resolve().relative_to(toplevel.resolve())
    return relative.parts[0] if relative.parts else "vault"


def worktree_root(settings: model.MemorySettings, runtime_root: Path | None) -> Path | None:
    if settings.worktree_root is not None:
        return Path(settings.worktree_root)
    return runtime_root / WORKTREE_FOLDER if runtime_root is not None else None


def _worktree_problem(path: Path, toplevel: Path | None) -> str | None:
    resolved = path.resolve()
    if toplevel is not None and resolved.is_relative_to(toplevel.resolve()):
        return f"the worktree folder {path} is inside the vault's repository"
    if resolved.exists():
        if not resolved.is_dir():
            return f"the worktree folder {path} is not a directory"
        return None if os.access(resolved, os.W_OK | os.X_OK) else f"the server cannot write in {path}"
    parent = next((item for item in resolved.parents if item.exists()), None)
    if parent is None or not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        return f"the worktree folder {path} cannot be created: {parent or '/'} is not writable"
    return None


async def _push_channel_problem(channel_id: str | None, db: Any = None) -> str | None:
    if channel_id is None:
        return None
    row = await channel_configs.get_config(channel_id, pool=db)
    if row is None or row["channel_type"] not in _PUSH_CHANNEL_TYPES:
        return f"push_channel {channel_id} is not a Lark or Telegram bot"
    operator = (row.get("config") or {}).get(OPERATOR_KEY)
    if not isinstance(operator, str) or not operator.strip():
        return f"push_channel {channel_id} has no operator id (My ID) set: Looper pushes only to the operator"
    if not row["enabled"]:
        return f"push_channel {channel_id} is turned off"
    return None


async def stored(db: Any = None) -> Mapping[str, Any] | None:
    """The stored value, unchecked; None without a row (or an unreadable one)."""
    value = (await system_settings.get_settings([model.SETTING_KEY], pool=db)).get(model.SETTING_KEY)
    try:
        raw = json.loads(value) if value is not None else None
    except ValueError:
        return {}
    return raw if isinstance(raw, Mapping) or raw is None else {}


async def load(db: Any = None) -> tuple[model.MemorySettings | None, str | None]:
    """The setting checked against the knowledge vault as it is now: (settings, None), (None, why it no longer
    holds), or (None, None) without a row. Checked again on every read, as the knowledge vault is."""
    raw = await stored(db)
    if raw is None:
        return None, None
    config, problem = await vault.load()
    if config is None:
        return None, f"the knowledge vault does not hold: {problem or 'it is not configured'}"
    try:
        return model.check_settings(raw, config), None
    except (ValueError, OSError) as error:
        return None, f"the memory_write setting no longer holds: {error}"


async def enabled(db: Any = None) -> bool:
    settings, _ = await load(db)
    return settings is not None and settings.enabled


async def settings_view(db: Any = None) -> dict[str, Any]:
    raw = await stored(db)
    settings, problem = await load(db)
    return {"configured": raw is not None, "enabled": settings is not None and settings.enabled,
            "settings": settings.to_json() if settings is not None else (dict(raw) if raw else None),
            "problem": problem}


async def update_settings(body: Mapping[str, Any], *, db: Any = None) -> dict[str, Any]:
    """Save ``body`` as the whole setting (``{"root": null}`` alone clears it), checked against the knowledge vault,
    git and the channels; ``SettingsError`` when it cannot be saved. The branch is the main checkout's now, and the
    commit prefix is derived when not given."""
    if dict(body) == {"root": None}:
        await system_settings.put_settings({model.SETTING_KEY: None}, pool=db)
        return await settings_view(db)
    config, problem = await vault.load()
    if config is None:
        raise SettingsError(f"configure the knowledge vault first: {problem or 'it is not configured'}")
    try:
        settings = model.check_settings(body, config)
    except (ValueError, OSError) as error:
        raise SettingsError(str(error)) from None
    facts = await asyncio.to_thread(git_facts, config, settings)
    if facts.toplevel is None:
        raise SettingsError("the knowledge vault is not in a git repository: Looper writes memory as commits")
    if facts.branch is None:
        raise SettingsError("the vault's main checkout has a detached HEAD: check out its branch first")
    if settings.branch is not None and settings.branch != facts.branch:
        raise SettingsError(f"branch {settings.branch} is not the main checkout's branch ({facts.branch})")
    if facts.ignore_error:
        raise SettingsError(facts.ignore_error)
    if facts.ignored:
        raise SettingsError(f"git ignores {facts.ignored[0]}: a commit could not add it")
    if settings.worktree_root is not None and (
            why := _worktree_problem(Path(settings.worktree_root), facts.toplevel)):
        raise SettingsError(why)
    if why := await _push_channel_problem(settings.push_channel, db):
        raise SettingsError(why)
    settings = replace(settings, branch=facts.branch,
                       commit_prefix=settings.commit_prefix or commit_prefix(config, settings.root, facts.toplevel))
    await system_settings.put_settings({model.SETTING_KEY: json.dumps(settings.to_json(), ensure_ascii=False)},
                                       pool=db)
    return await settings_view(db)


def _check(name: str, problem: str | None, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": problem is None, "detail": problem if problem is not None else detail}


async def check(*, runtime_root: Path | None, db: Any = None) -> dict[str, Any]:
    """The read-only preflight of what a write needs now: the setting, the knowledge vault, its repository and
    branch, no operation under way, no ignored memory path, the worktree folder and the push channel. Changes
    nothing."""
    raw = await stored(db)
    if raw is None:
        return {"configured": False, "enabled": False, "ok": False,
                "checks": [_check("setting", "the memory_write setting is not configured")]}
    settings, problem = await load(db)
    checks = [_check("setting", problem, "the setting holds against the knowledge vault")]
    if settings is None:
        return {"configured": True, "enabled": False, "ok": False, "checks": checks}
    config, _ = await vault.load()
    assert config is not None  # load() held
    facts = await asyncio.to_thread(git_facts, config, settings)
    checks.append(_check("git", None if facts.toplevel else "the knowledge vault is not in a git repository",
                         str(facts.toplevel or "")))
    if facts.toplevel is not None:
        branch_problem = ("the main checkout has a detached HEAD" if facts.branch is None else
                          f"the main checkout is on {facts.branch}, not {settings.branch}"
                          if facts.branch != settings.branch else None)
        checks.append(_check("branch", branch_problem, facts.branch or ""))
        checks.append(_check("operation", f"an operation is under way in the main checkout: "
                                          f"{', '.join(facts.operations)}" if facts.operations else None, "none"))
        checks.append(_check("ignored", facts.ignore_error or (
            f"git ignores {', '.join(facts.ignored)}" if facts.ignored else None), "none of the memory paths"))
    where = worktree_root(settings, runtime_root)
    checks.append(_check("worktree_root", "no runtime root to put the worktree folder in" if where is None else
                         _worktree_problem(where, facts.toplevel), str(where or "")))
    checks.append(_check("push_channel", await _push_channel_problem(settings.push_channel, db),
                         settings.push_channel or "not set: candidates show on the memory page only"))
    return {"configured": True, "enabled": settings.enabled, "ok": all(item["ok"] for item in checks),
            "checks": checks}


async def confirmation_impact(body: Mapping[str, Any], *, runtime_root: Path | None, db: Any = None) -> str | None:
    """What the operator confirms before a save takes effect (RFC section 9.1): turning writing on, or pointing the
    pushes at another bot while it is on. None when the save needs no confirmation, or cannot be saved at all (the
    save itself then refuses it in words)."""
    if body.get("enabled") is not True:
        return None
    raw = await stored(db)
    if raw is not None and raw.get("enabled") is True and body.get("push_channel") == raw.get("push_channel"):
        return None
    config, _ = await vault.load()
    if config is None:
        return None
    try:
        settings = model.check_settings(body, config)
    except (ValueError, OSError):
        return None
    facts = await asyncio.to_thread(git_facts, config, settings)
    worktrees = worktree_root(settings, runtime_root)
    changing = raw is not None and raw.get("enabled") is True
    return "\n".join((
        "Change where memory proposals are pushed." if changing else
        "Turn on memory writing: memories you confirm are written into your knowledge vault as git commits.",
        f"Memory folder: {settings.root or '(the whole vault)'}",
        "Branch: " + (settings.branch or facts.branch or "(the main checkout's current branch)"),
        f"Worktree folder: {worktrees if worktrees is not None else '(under the runtime folder)'}",
        f"Push target: {settings.push_channel or 'none (the memory page only)'}",
        f"Daily drafting budget: ${settings.daily_budget_usd:g}"))
