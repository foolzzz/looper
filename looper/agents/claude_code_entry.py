"""Opt-in SessionStart profile hook for the operator's own Claude Code user settings."""

from __future__ import annotations

import contextlib
import copy
import difflib
import io
import ipaddress
import json
import os
import shlex
import shutil
import subprocess
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from http.client import HTTPException
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..ops_config import OpsConfig

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
        return Fetched(EMPTY, reason="profile unavailable or empty (check knowledge_vault.profile_source, "
                                    "database settings and that entries carry [share:: hook])")
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


def _confirm(prompt: str, yes: bool, confirm=None, *, out=None) -> int:
    out = sys.stdout if out is None else out
    if yes:
        return OK
    if confirm is None and not sys.stdin.isatty():
        print("confirmation needed: run it in a terminal, or pass --yes after reviewing --dry-run", file=out)
        return USAGE
    accepted = confirm(prompt) if confirm is not None else input(prompt).strip().lower() in ("y", "yes")
    if not accepted:
        print("nothing written", file=out)
        return EMPTY
    return OK


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _loopback(host: str) -> str | None:
    if host == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_loopback:
        return None
    return f"[{address}]" if address.version == 6 else str(address)


def mcp_entry(runtime_root: Path, config: str | None) -> dict:
    from .service import looper_command

    argv = [*looper_command(sys.executable), "claude-code", "mcp", "--runtime-root", str(runtime_root)]
    if config:
        argv += ["--config", str(Path(config).expanduser().absolute())]
    return {"type": "stdio", "command": argv[0], "args": argv[1:]}


def _claude(args) -> str | None:
    return shutil.which(getattr(args, "claude", None) or "claude")


def _claude_mcp(claude: str, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run([claude, "mcp", *arguments], capture_output=True, text=True, timeout=30)


def edit_mcp(args, runtime_root: Path | None, *, confirm=None, out=None) -> int:
    from .claude_code_mcp import SERVER_NAME

    out = sys.stdout if out is None else out
    install = args.claude_code_command == "install"
    entry = mcp_entry(runtime_root, args.config) if install else None
    claude = _claude(args)
    command = ([claude or "claude", "mcp", "add-json", "--scope", "user", SERVER_NAME,
                json.dumps(entry, ensure_ascii=False)] if install else
               [claude or "claude", "mcp", "remove", "--scope", "user", SERVER_NAME])
    if not claude:
        print("Claude Code's claude command was not found; run this yourself:", file=out)
        print(shlex.join(command), file=out)
        return EMPTY
    try:
        registered = _claude_mcp(claude, "get", SERVER_NAME).returncode == 0
        if registered == install:
            print("looper-memory is already registered with Claude Code. To register it again (for example "
                  "after moving Looper): looper claude-code uninstall mcp, then install mcp" if install else
                  "looper-memory is not registered with Claude Code.", file=out)
            return OK
        if install:
            print("Registers an MCP server with Claude Code at user scope "
                  "(Claude Code writes it to ~/.claude.json):", file=out)
            print(json.dumps(entry, indent=2, ensure_ascii=False), file=out)
        print(shlex.join(command), file=out)
        if args.dry_run:
            return OK
        code = _confirm("Apply this MCP registration change? [y/N] ", args.yes, confirm, out=out)
        if code != OK:
            return code
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print("\n".join(result.stderr.splitlines()[-5:]) or "Claude Code MCP command failed", file=out)
            return EMPTY
        registered = _claude_mcp(claude, "get", SERVER_NAME).returncode == 0
        if registered != install:
            print("claude mcp add-json reported success but get does not find looper-memory" if install else
                  "claude mcp remove reported success but get still finds looper-memory", file=out)
            return EMPTY
        print("registered. Open a new Claude Code session; the first call of each tool asks for your permission. "
              "Undo: looper claude-code uninstall mcp" if install else "removed.", file=out)
        return OK
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Claude Code MCP command failed: {exc}", file=out)
        return EMPTY


def _retrieval_url(ops: OpsConfig) -> str:
    from . import server_address

    host, port = server_address.read(ops.runtime_root)
    # A wildcard listener is reached locally through its loopback address.
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    address = _loopback(host)
    if address is None:
        raise ValueError(f"server.host {host} is not a loopback address")
    return f"http://{address}:{port}"


def _retrieval_settings(args) -> tuple[dict, dict]:
    from .. import ops_config
    from . import operator_auth

    ops = ops_config.load(ops_config.Flags(runtime_root=args.runtime_root, config=args.config), require_database=False)
    url = _retrieval_url(ops)
    token = operator_auth.read_token(ops.runtime_root)
    if not token:
        raise ValueError("no operator token: start Looper once (looper serve)")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    answers = []
    for path in ("knowledge_vault/get", "leads/settings_get"):
        request = Request(f"{url}/api/v1/{path}", data=b"{}", method="POST",
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                   "Accept": "application/json"})
        with opener.open(request, timeout=2) as response:
            answer = json.load(response)
        if not isinstance(answer, dict) or answer.get("status") != "ok" or not isinstance(answer.get("result"), dict):
            raise ValueError("invalid retrieval settings response")
        answers.append(answer["result"])
    return answers[0], answers[1]


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
        code = _confirm(f"Write these changes to {path}? [y/N] ", yes, confirm, out=out)
        if code != OK:
            return code
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
    hook_healthy = len(handlers) == 1
    if not handlers:
        print("  profile hook: not installed\ninstall: looper claude-code install hook", file=out)
    for handler in handlers:
        executable = shlex.split(handler["command"])[0]
        exists = Path(executable).is_file() and os.access(executable, os.X_OK)
        hook_healthy &= exists
        print(f"  profile hook: installed ({handler['command']})" if exists else
              f"  profile hook: installed, but its looper is missing: {executable}", file=out)
    fetched = fetch_profile(args.runtime_root, args.config)
    if fetched.code == OK:
        print(f"Looper: profile readable locally\n  profile: {len(fetched.text)} characters", file=out)
    else:
        print(f"Looper: not answering ({fetched.reason})\n  profile: {fetched.reason}", file=out)
    claude = _claude(args)
    registered = False
    registration_known = True
    if claude is None:
        registration = "unknown (claude not found)"
    else:
        try:
            registered = _claude_mcp(claude, "get", "looper-memory").returncode == 0
            registration = "registered" if registered else "not registered"
        except (OSError, subprocess.TimeoutExpired):
            registration, registration_known = "unknown (claude failed)", False
    print(f"  retrieval MCP server (looper-memory): {registration}", file=out)
    retrieval_healthy = False
    try:
        vault, leads = _retrieval_settings(args)
        retrieval_healthy = (vault.get("configured") is True and vault.get("manager_read") is True
                            and not vault.get("problem"))
        vault_status = (f"readable (root {vault.get('path')})" if retrieval_healthy else
                        f"off: {vault.get('problem') or 'manager_read is off or the vault is not configured'}")
        print(f"  knowledge vault for the Manager: {vault_status}", file=out)
        if type(leads.get("enabled")) is not bool:
            raise ValueError("invalid message leads settings response")
        print("  message leads: " + ("on" if leads["enabled"] else "off"), file=out)
    except (ValueError, OSError, URLError, HTTPException) as exc:
        print(f"  knowledge vault for the Manager: off: {exc}\n  message leads: unknown", file=out)
        retrieval_healthy = False
    healthy = registration_known and bool(handlers or registered)
    if handlers:
        healthy &= hook_healthy and fetched.code == OK
    if registered:
        healthy &= retrieval_healthy
    return OK if healthy else EMPTY


def run(args, *, confirm=None, out=None) -> int:
    out = sys.stdout if out is None else out
    if args.claude_code_command == "mcp":
        from . import claude_code_mcp

        return claude_code_mcp.main(args.runtime_root, args.config)
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
    if any(part not in ("hook", "mcp") for part in args.parts):
        print("unknown part: expected hook or mcp", file=out)
        return USAGE
    from .. import child_env

    if os.environ.get(child_env.AGENT_RUN_ENV):
        print("refused inside a Looper agent run", file=out)
        return REFUSED
    desired = None
    runtime_root = None
    if args.claude_code_command == "install":
        from .. import ops_config

        try:
            ops = ops_config.load(ops_config.Flags(runtime_root=args.runtime_root, config=args.config),
                                  require_database=False)
            runtime_root = ops.runtime_root
            desired = desired_handler(runtime_root, args.config)
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

    first_error = OK
    for part in (args.parts or ["hook", "mcp"]):
        if part == "mcp":
            code = edit_mcp(args, runtime_root, confirm=confirm, out=out)
        else:
            code = edit_settings(change, yes=args.yes, dry_run=args.dry_run, confirm=confirm, out=out)
            if code == OK and changed and not args.dry_run:
                print("installed. Open a new Claude Code session (or /clear) to load it. "
                      "Undo: looper claude-code uninstall hook" if desired is not None else "removed.", file=out)
        if first_error == OK and code != OK:
            first_error = code
    return first_error
