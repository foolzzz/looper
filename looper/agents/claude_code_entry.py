"""Opt-in SessionStart profile hook for the operator's own Claude Code user settings."""

from __future__ import annotations

import contextlib
import copy
import difflib
import io
import json
import os
import shlex
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

MAX_CONTEXT_CHARS = 3000
HOOK_TIMEOUT_SECONDS = 5
HOOK_ARGS = ("claude-code", "profile", "--hook")
MANAGED_ROOTS = (Path("/Library/Application Support/ClaudeCode"), Path("/etc/claude-code"))
OK, EMPTY, USAGE, REFUSED = 0, 1, 2, 4


@dataclass(frozen=True)
class Fetched:
    code: int
    text: str = ""
    reason: str = ""


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    clipped = text[:limit - 1]
    newline = clipped.rfind("\n")
    if newline > limit // 2:
        clipped = clipped[:newline]
    return clipped.rstrip() + "…"


def fetch_profile(runtime_root: str | None, config: str | None, *,
                  environ: Mapping[str, str] = os.environ) -> Fetched:
    from .. import child_env
    from . import profile_commands

    if environ.get(child_env.AGENT_RUN_ENV):
        return Fetched(REFUSED, reason="refused inside a Looper agent run")
    text = profile_commands.read_text(SimpleNamespace(runtime_root=runtime_root, config=config))
    if not text.strip():
        return Fetched(EMPTY, reason="profile unavailable or empty (check knowledge_vault.profile_source "
                                    "and database settings)")
    return Fetched(OK, text=_cap(text, MAX_CONTEXT_CHARS))


def profile_command(args) -> int:
    fetched = fetch_profile(args.runtime_root, args.config)
    if args.hook:
        if fetched.code == OK:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                              "additionalContext": fetched.text}}, ensure_ascii=False))
        return OK
    if fetched.code == OK:
        print(fetched.text)
    else:
        print(fetched.reason, file=sys.stderr)
    return fetched.code


class SettingsError(ValueError):
    """A settings file cannot safely be changed."""


class ManagedSettingsError(SettingsError):
    """Organization-managed settings are never a write target."""


def settings_path(environ: Mapping[str, str] = os.environ) -> Path:
    base = environ.get("CLAUDE_CONFIG_DIR", "").strip()
    home = Path(environ.get("HOME", str(Path.home()))).expanduser()
    return (Path(base).expanduser() if base else home / ".claude") / "settings.json"


def _target(path: Path) -> Path:
    target = path.resolve()
    if any(target.is_relative_to(root.resolve()) for root in MANAGED_ROOTS):
        raise ManagedSettingsError(f"{path} is organization-managed Claude Code settings; Looper never edits them")
    return target


def _read_settings(target: Path) -> tuple[bytes | None, dict]:
    try:
        original = target.read_bytes()
    except FileNotFoundError:
        return None, {}
    try:
        data = json.loads(original.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SettingsError(f"cannot read {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise SettingsError("settings must be a JSON object")
    if "hooks" in data:
        hooks = data["hooks"]
        if not isinstance(hooks, dict):
            raise SettingsError("hooks must be an object")
        if "SessionStart" in hooks:
            groups = hooks["SessionStart"]
            if not isinstance(groups, list) or any(
                not isinstance(group, dict) or not isinstance(group.get("hooks"), list) for group in groups
            ):
                raise SettingsError("hooks.SessionStart must be a list of objects with a hooks list")
    return original, data


def _render(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _is_looper_handler(handler) -> bool:
    if not isinstance(handler, dict) or handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        return False
    try:
        argv = shlex.split(handler["command"])
    except ValueError:
        return False
    return any(tuple(argv[i:i + len(HOOK_ARGS)]) == HOOK_ARGS for i in range(len(argv)))


def desired_handler(runtime_root: Path, config: str | None) -> dict:
    from .service import looper_command

    argv = [*looper_command(sys.executable), *HOOK_ARGS, "--runtime-root", str(runtime_root)]
    if config:
        argv += ["--config", str(Path(config).expanduser().absolute())]
    return {"type": "command", "command": shlex.join(argv) + " 2>/dev/null || true", "timeout": HOOK_TIMEOUT_SECONDS}


def _handlers(data: dict) -> list[dict]:
    return [handler for group in data.get("hooks", {}).get("SessionStart", [])
            for handler in group["hooks"] if _is_looper_handler(handler)]


def _remove(data: dict) -> None:
    hooks = data.get("hooks", {})
    groups = []
    for group in hooks.get("SessionStart", []):
        remaining = [handler for handler in group["hooks"] if not _is_looper_handler(handler)]
        if remaining or not any(_is_looper_handler(handler) for handler in group["hooks"]):
            groups.append({**group, "hooks": remaining})
    if groups:
        hooks["SessionStart"] = groups
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        data.pop("hooks", None)


def install_change(data: dict, desired: dict) -> tuple[dict, str] | None:
    old = _handlers(data)
    if old == [desired]:
        return None
    _remove(data)
    data.setdefault("hooks", {}).setdefault("SessionStart", []).append({"hooks": [desired]})
    summary = "".join(f"Replaces the Looper hook that runs: {handler['command']}\n" for handler in old)
    return data, summary + "Adds to hooks.SessionStart:\n" + _render({"hooks": [desired]})


def uninstall_change(data: dict) -> tuple[dict, str] | None:
    old = _handlers(data)
    if not old:
        return None
    _remove(data)
    return data, "Removes from hooks.SessionStart:\n" + _render({"hooks": old})


def edit_settings(change: Callable[[dict], tuple[dict, str] | None], *, yes: bool, dry_run: bool,
                  confirm: Callable[[str], bool] | None = None, out=None, environ=os.environ) -> int:
    from .. import child_env

    out = sys.stdout if out is None else out
    if environ.get(child_env.AGENT_RUN_ENV):
        print("refused inside a Looper agent run", file=out)
        return REFUSED
    try:
        path = settings_path(environ)
        target = _target(path)
        original, data = _read_settings(target)
        changed = change(copy.deepcopy(data))
        if changed is None:
            return OK
        new_data, summary = changed
        old_text = original.decode("utf-8") if original is not None else ""
        new_text = _render(new_data)
        print(f"Claude Code user settings: {path}" + (f" (a link to {target})" if path.is_symlink() else ""), file=out)
        print(summary, file=out)
        out.writelines(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                          fromfile=str(path), tofile=f"{path} (after)"))
        if original is not None and _render(data) != old_text:
            print("note: the file is rewritten in Claude Code's own 2-space JSON format", file=out)
        if dry_run:
            return OK
        if not yes:
            prompt = f"Write these changes to {path}? [y/N] "
            if confirm is None and not sys.stdin.isatty():
                print("confirmation needed: run it in a terminal, or pass --yes after reviewing --dry-run", file=out)
                return USAGE
            accepted = confirm(prompt) if confirm is not None else input(prompt).strip().lower() in ("y", "yes")
            if not accepted:
                print("nothing written", file=out)
                return EMPTY
        current = target.read_bytes() if target.exists() else None
        if _target(path) != target or current != original:
            print("the settings file changed while waiting; nothing written; run it again", file=out)
            return EMPTY
        mode = stat.S_IMODE(target.stat().st_mode) if original is not None else 0o600
        if original is not None:
            backup = target.with_name(f"{target.name}.looper-backup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
            # Exclusive creation preserves earlier backups, including repeated edits in the same second.
            for suffix in range(1000):
                candidate = backup if suffix == 0 else backup.with_name(f"{backup.name}-{suffix}")
                try:
                    fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                    break
                except FileExistsError:
                    continue
            else:
                raise SettingsError("too many settings backups; nothing written")
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            print(f"backup: {candidate}", file=out)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_text)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), mode)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        try:
            _, readback = _read_settings(target)
        except (SettingsError, OSError) as exc:
            print(f"settings readback failed: {exc}; keep the backup and inspect the file", file=out)
            return EMPTY
        if _handlers(readback) != _handlers(new_data):
            print("settings readback failed; keep the backup and inspect the file", file=out)
            return EMPTY
        return OK
    except ManagedSettingsError as exc:
        print(str(exc), file=out)
        return REFUSED
    except (SettingsError, OSError, RuntimeError) as exc:
        print(str(exc), file=out)
        return USAGE


def status_command(args, *, out=None) -> int:
    out = sys.stdout if out is None else out
    path = settings_path()
    print(f"Claude Code user settings: {path}", file=out)
    try:
        _, data = _read_settings(_target(path))
        handlers = _handlers(data)
    except (SettingsError, OSError, RuntimeError) as exc:
        print(str(exc), file=out)
        return EMPTY
    healthy = len(handlers) == 1
    if not handlers:
        print("  profile hook: not installed\ninstall: looper claude-code install hook", file=out)
    for handler in handlers:
        executable = shlex.split(handler["command"])[0]
        exists = Path(executable).is_file() and os.access(executable, os.X_OK)
        healthy &= exists
        print(f"  profile hook: installed ({handler['command']})" if exists else
              f"  profile hook: installed, but its looper is missing: {executable}", file=out)
    fetched = fetch_profile(args.runtime_root, args.config)
    if fetched.code == OK:
        print(f"Looper: profile readable locally\n  profile: {len(fetched.text)} characters", file=out)
    else:
        print(f"Looper: not answering ({fetched.reason})\n  profile: {fetched.reason}", file=out)
    return OK if healthy and fetched.code == OK else EMPTY


def run(args, *, confirm=None, out=None) -> int:
    out = sys.stdout if out is None else out
    if args.claude_code_command == "profile":
        if args.hook:
            # Buffer both streams: an exception must never leave partial JSON or diagnostics in a hook.
            output = io.StringIO()
            try:
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(output):
                    profile_command(args)
            except BaseException:
                return OK
            sys.stdout.write(output.getvalue())
            return OK
        return profile_command(args)
    if args.claude_code_command == "status":
        return status_command(args, out=out)
    if any(part != "hook" for part in args.parts):
        print("unknown part: expected hook", file=out)
        return USAGE
    from .. import child_env

    if os.environ.get(child_env.AGENT_RUN_ENV):
        print("refused inside a Looper agent run", file=out)
        return REFUSED
    desired = None
    if args.claude_code_command == "install":
        from .. import ops_config

        try:
            ops = ops_config.load(ops_config.Flags(runtime_root=args.runtime_root, config=args.config),
                                  require_database=False)
            desired = desired_handler(ops.runtime_root, args.config)
        except (ops_config.OpsConfigError, OSError) as exc:
            print(f"ops settings unusable: {exc}", file=out)
            return USAGE
    changed = False

    def change(data):
        nonlocal changed
        result = install_change(data, desired) if desired is not None else uninstall_change(data)
        changed = result is not None
        if result is None:
            print("the SessionStart hook is already installed" if desired is not None else
                  "the SessionStart hook is not installed", file=out)
        return result

    code = edit_settings(change, yes=args.yes, dry_run=args.dry_run, confirm=confirm, out=out)
    if code == OK and changed and not args.dry_run:
        print("installed. Open a new Claude Code session (or /clear) to load it. "
              "Undo: looper claude-code uninstall hook" if desired is not None else "removed.", file=out)
    return code
