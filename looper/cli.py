"""The ``looper`` command: ``serve`` starts the agent system, ``service`` runs it in the background, ``doctor``
checks what it needs, ``mcp-server`` serves the Manager's platform tools, ``version`` prints the version."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:
    from .ops_config import OpsConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="looper", description="Looper agent system.", allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"looper {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve",
        allow_abbrev=False,
        help="Start the Looper agent system.",
        description=(
            "Start the Looper agent system. Telegram and Lark need no flags: enable and configure "
            "them on the dashboard Settings page, then restart the server."
        ),
    )
    serve.add_argument("--host", help="Bind host (default: the ops file's server.host, else 127.0.0.1).")
    serve.add_argument("--port", type=int, help="Port (default: the ops file's server.port, else 8767).")
    serve.add_argument("--verbose", action="store_true", help="Enable debug logging (server.log_level = \"debug\").")
    _add_location_flags(serve)
    serve.add_argument(
        "--skip-backup", action="store_true",
        help="Change the database schema without the pg_dump backup otherwise taken first into "
             "<runtime root>/backups (logged as a warning).",
    )
    auth = commands.add_parser("auth", allow_abbrev=False, help="Manage the browser sign-in password.")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    set_password = auth_commands.add_parser("set-password", allow_abbrev=False,
                                           help="Set the browser sign-in password (prompted without echo).")
    set_password.add_argument("--user", default="admin", help="User name (default admin).")
    clear_password = auth_commands.add_parser("clear-password", allow_abbrev=False,
                                             help="Remove the browser sign-in password.")
    for command in (set_password, clear_password):
        command.add_argument("--runtime-root", help="Override the default ~/.looper state directory.")
    doctor = commands.add_parser(
        "doctor",
        allow_abbrev=False,
        help="Check what Looper needs: PostgreSQL, the Claude Code CLI and its login, the enabled channels "
             "and pg_dump.",
        description=(
            "Check PostgreSQL (the ops file's database.url), the Claude Code CLI and its login, the enabled channels, "
            "and the pg_dump that backs the database up before a schema change; print each result with its fix. "
            "Exits 1 when a check failed. Makes no model call."
        ),
    )
    _add_location_flags(doctor)
    config = commands.add_parser(
        "config",
        allow_abbrev=False,
        help="Create the service's local ops file.",
        description="init-ops writes the local ops file used to start Looper.",
    )
    config_commands = config.add_subparsers(dest="config_command", required=True)
    init_ops = config_commands.add_parser(
        "init-ops", allow_abbrev=False, help="Write the ops file from the settings in effect now (mode 0600).",
        description=(
            "Write the ops file, <runtime root>/ops/server.toml (or --config), from the settings in effect, "
            "including deployment settings from the environment or the checkout's .env. Without a database URL, "
            "it writes the sample postgresql://localhost:5432/loopx. It refuses to replace an existing file."
        ),
    )
    _add_location_flags(init_ops)
    _add_service_parser(commands)
    _add_msgflow_parser(commands)
    _add_ledger_parser(commands)
    _add_leads_parser(commands)
    _add_memory_parser(commands)
    _add_decisions_parser(commands)
    _add_claude_code_parser(commands)
    profile = commands.add_parser("profile", allow_abbrev=False, help="Read the operator profile locally.")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    show = profile_sub.add_parser(
        "show", allow_abbrev=False, help="Print the compiled profile; silently exit 0 when unavailable (within 2 s).")
    _add_location_flags(show)
    commands.add_parser(
        "mcp-server",
        allow_abbrev=False,
        help="Serve Looper's platform tools to the Manager over MCP on stdin/stdout; the Manager's runtime starts "
             "it, with LOOPER_AGENT_URL and LOOPER_AGENT_TOKEN.",
    )
    version = commands.add_parser("version", allow_abbrev=False, help="Print the installed Looper version.")
    version.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format.")
    return parser


def _add_claude_code_parser(commands: argparse._SubParsersAction) -> None:
    cc = commands.add_parser(
        "claude-code", allow_abbrev=False,
        help="Give your own Claude Code the Manager's profile and read-only retrieval (opt-in).",
        description="Reuse looper profile show for your own Claude Code. Nothing is installed until install "
                    "shows the change and you confirm it.")
    sub = cc.add_subparsers(dest="claude_code_command", required=True)
    profile = sub.add_parser(
        "profile", allow_abbrev=False, help="Print the compiled operator profile.",
        description="Reads the same local profile as looper profile show (2 s at most). Exit codes: "
                    "0 printed, 1 unavailable or empty, 2 usage, 4 inside an agent run. "
                    "With --hook: print SessionStart JSON or nothing, never write stderr, always exit 0.")
    profile.add_argument("--hook", action="store_true", help="Claude Code SessionStart hook mode.")
    for name, help_text in (("install", "Add the SessionStart hook and retrieval MCP server after confirmation."),
                            ("uninstall", "Remove the SessionStart hook and retrieval MCP server after confirmation.")):
        command = sub.add_parser(name, allow_abbrev=False, help=help_text)
        # Python 3.11 argparse rejects [] with nargs="*", choices=["hook", "mcp"]. Validate in run instead.
        command.add_argument("parts", nargs="*", help="What to change: hook, mcp (default: every part).")
        command.add_argument("--yes", action="store_true", help="Write without asking (review --dry-run first).")
        command.add_argument("--dry-run", action="store_true", help="Show the change; write nothing.")
        command.add_argument("--claude", default=shutil.which("claude"), help="Claude Code executable path.")
        _add_location_flags(command)
    mcp = sub.add_parser("mcp", allow_abbrev=False,
        help="Serve read-only retrieval tools over MCP on stdin/stdout (install mcp registers it).")
    _add_location_flags(mcp)
    status = sub.add_parser("status", allow_abbrev=False, help="Show what is installed and whether it works.")
    status.add_argument("--claude", default=shutil.which("claude"), help="Claude Code executable path.")
    for command in (profile, status):
        _add_location_flags(command)


def _add_msgflow_parser(commands: argparse._SubParsersAction) -> None:
    # Only argparse here: `looper version` must not import the agent system (its code is loaded when the command runs).
    msgflow = commands.add_parser("msgflow", allow_abbrev=False, help="Work on the message stream.",
                                  description="One-off work on the message stream.")
    sub = msgflow.add_subparsers(dest="msgflow_command", required=True)
    imp = sub.add_parser(
        "import", allow_abbrev=False, help="Import the last N days from an outside collector's files.",
        description="Read the source's bridge directory from the start of every file and store the messages sent in "
                    "the last N days (a message already stored is left as it is). --dry-run stores nothing and "
                    "prints what it would store. If the server is writing the source at that moment it says so and "
                    "exits; run it again.")
    imp.add_argument("--days", type=int, required=True, help="How many days back to import (1 to 3650).")
    imp.add_argument("--dry-run", action="store_true", help="Count what would be stored; store nothing.")
    imp.add_argument("--per-file", action="store_true", help="Also print one reconciliation line per file.")
    imp.add_argument("--source", help="The source id (default: the only one).")
    _add_location_flags(imp)
    delete = sub.add_parser("delete", allow_abbrev=False, help="Delete one message after reviewing its limits.",
        description="Preview a message deletion, list copies it cannot reach, then type delete to confirm. "
                    "A tombstone prevents a later import from restoring the message.")
    delete.add_argument("--message", required=True, help="The exact message_id to delete.")
    delete.add_argument("--source", help="The source id (default: the only one).")
    delete.add_argument("--dry-run", action="store_true", help="Show the preview without asking to delete.")
    _add_location_flags(delete)

    backfill = sub.add_parser(
        "leads-backfill", allow_abbrev=False, help="Count, or turn into leads, the history before a leads subscription.",
        description="Count the in-scope messages of the last N days up to where the leads subscription started, their "
                    "windows and what they would cost (a dry run, the default: nothing is written). --run --budget-usd "
                    "X runs them oldest first through the subscription's processor and stores the leads, keeping its "
                    "progress after each window and stopping before a window would pass the budget; run again, it "
                    "resumes.")
    backfill.add_argument("--days", type=int, required=True, help="How many days back (1 to 365).")
    backfill.add_argument("--schedule", help="The leads subscription's schedule id (default: the only one).")
    backfill.add_argument("--run", action="store_true", help="Run the windows (needs --budget-usd).")
    backfill.add_argument("--budget-usd", type=float, help="With --run: the most the backfill may spend.")
    backfill.add_argument("--claude", help="The Claude Code executable for a profile that names none (default: claude).")
    _add_location_flags(backfill)
    why = sub.add_parser(
        "why", allow_abbrev=False, help="Say why a stored message was or was not reported.",
        description="Answer why one stored message was or was not told to you. The message is a Lark message id, "
                    "a message link, or its numeric seq. Reads the database directly, so it works with the service "
                    "stopped. Exit codes: 0 answered, 2 usage, 3 triage is off, 5 no such message, 6 database unusable.")
    why.add_argument("message", help="A Lark message id, a message link, or a numeric seq.")
    why.add_argument("--json", action="store_true", help="Print the typed answer as JSON.")
    _add_location_flags(why)


def _add_ledger_parser(commands: argparse._SubParsersAction) -> None:
    ledger = commands.add_parser(
        "ledger", allow_abbrev=False, help="Read and write the Manager's ledger.",
        description="The ledger lists what waits for you, what was decided and who does what. It reads and writes the "
                    "database directly, so it works with the service stopped. A write is yours: it is recorded as "
                    "you, from the session named by --session, and is refused inside an agent Looper started. "
                    "Exit codes: 0 done, 1 nothing listed, 2 usage, 3 ledger not enabled, 4 refused, 5 no such item, "
                    "6 database unusable.")
    sub = ledger.add_subparsers(dest="ledger_command", required=True)
    listing = sub.add_parser("list", allow_abbrev=False, help="List items (default: the open ones).")
    listing.add_argument("--status", default="open",
                         choices=["open", "all", "done", "cancelled", "expired", "superseded"])
    listing.add_argument("--kind", choices=["pending", "decision", "work_item"])
    listing.add_argument("--owner", help="user, manager, chat, or task:<id>.")
    listing.add_argument("--project", help="A project's id or name.")
    listing.add_argument("--limit", type=int, default=20, help="1 to 50 (default 20).")
    listing.add_argument("--json", action="store_true")
    show = sub.add_parser("show", allow_abbrev=False, help="An item with all its events.")
    show.add_argument("item", help="L-0012, L-12 or 12.")
    show.add_argument("--json", action="store_true")
    add = sub.add_parser("add", allow_abbrev=False, help="Add an item as yourself.")
    add.add_argument("--kind", required=True, choices=["pending", "decision", "work_item"])
    add.add_argument("--title", required=True, help="Up to 200 characters; it must make sense alone.")
    add.add_argument("--detail")
    add.add_argument("--quote", help="The words that were said, kept on the event.")
    add.add_argument("--due", help="YYYY-MM-DD, or an ISO 8601 time (this machine's zone when none is given).")
    add.add_argument("--project", help="A project's id or name.")
    add.add_argument("--ref", action="append", help="L-<number> or <kind>:<ref> (task, gate, pr, doc, vault, url); "
                                                    "up to 10.")
    close = sub.add_parser("close", allow_abbrev=False, help="Close an own pending or work item as done.")
    close.add_argument("item")
    close.add_argument("--cancel", action="store_true", help="Cancel it instead (any kind).")
    confirm = sub.add_parser("confirm", allow_abbrev=False, help="Confirm a decision that was only relayed.")
    confirm.add_argument("item")
    for command in (add, close, confirm):
        command.add_argument("--session", help="Required: the interactive session this is run from, such as "
                                               "'<tool> <date> <topic>'.")
    for command in (close, confirm):
        command.add_argument("--note")
    for command in (listing, show, add, close, confirm):
        _add_location_flags(command)


def _add_leads_parser(commands: argparse._SubParsersAction) -> None:
    leads = commands.add_parser(
        "leads", allow_abbrev=False, help="Search and administer sourced message leads.",
        description="Read and write message leads directly in PostgreSQL, also with the service stopped. "
                    "Exit codes: 0 done, 1 no results, 2 usage, 3 disabled, 4 refused (inside an agent run, or an "
                    "export that could be committed), 5 no such lead, 6 database unusable.")
    sub = leads.add_subparsers(dest="leads_command", required=True)
    search = sub.add_parser("search", allow_abbrev=False, help="Search unconfirmed message leads.")
    search.add_argument("words", nargs="*")
    search.add_argument("--chat")
    search.add_argument("--person")
    dates = search.add_mutually_exclusive_group()
    dates.add_argument("--since", help="YYYY-MM-DD, from midnight in this machine's zone.")
    dates.add_argument("--days", type=int, help="1 to 3650 days back.")
    search.add_argument("--kind", help="Lead kind (decision, commitment, request, status, issue, fact).")
    search.add_argument("--state", default="active", help="Lead state, or all (default active).")
    search.add_argument("--limit", type=int, default=10, help="1 to 50 (default 10).")
    search.add_argument("--json", action="store_true")
    mark = sub.add_parser("mark", allow_abbrev=False, help="Mark a lead inaccurate.")
    mark.add_argument("lead", help="lead-123 or 123.")
    mark.add_argument("--inaccurate", action="store_true", required=True)
    mark.add_argument("--note", help="Up to 500 characters.")
    delete = sub.add_parser("delete", allow_abbrev=False, help="Delete a lead and its sources.")
    delete.add_argument("lead", help="lead-123 or 123.")
    export = sub.add_parser(
        "eval-export", allow_abbrev=False, help="Write the leads check's eval set to a local JSONL file.",
        description="Every lead marked inaccurate, and a random sample of active ones, with the text of their source "
                    "messages, one JSON object per line; for `python -m evals.processor --cases-file`. The file must "
                    "be outside any git work tree or ignored by git (evals/runs/ is).")
    export.add_argument("--out", required=True, help="The JSONL file to write (mode 600).")
    export.add_argument("--sample", type=int, default=20, help="Active leads to add, 0 to 500 (default 20).")
    export.add_argument("--chat", help="Chat id or a uniquely matching name; requires --days and --intents.")
    export.add_argument("--days", type=int, help="1 to 30 days for intents (default horizon 30); requires --chat and --intents.")
    export.add_argument("--intents", action="store_true", help="Export anonymous intent windows and labels; requires --chat and --days.")
    for command in (search, mark, delete, export):
        _add_location_flags(command)


def _add_memory_parser(commands: argparse._SubParsersAction) -> None:
    memory = commands.add_parser(
        "memory", allow_abbrev=False, help="Read and manage memory candidates; check what a write needs.",
        description="Read memory candidates (what Looper may write to the knowledge vault once the operator confirms "
                    "it) directly in PostgreSQL, also with the service stopped. Confirming is the dashboard's or the "
                    "operator's direct chat's only. Exit codes: 0 done, 1 nothing found or a check failed, 2 usage, "
                    "3 not enabled, 4 refused (inside an agent run), 5 no such candidate, 6 database unusable, "
                    "7 state does not allow the action.")
    sub = memory.add_subparsers(dest="memory_command", required=True)
    listing = sub.add_parser("list", allow_abbrev=False, help="List memory candidates, newest first.")
    listing.add_argument("--state", default="open",
                         help="open (default: not written, rejected or dropped), all, or one state.")
    listing.add_argument("--kind", help="preference, feedback, decision, lesson, fact, commitment, person or project.")
    listing.add_argument("--limit", type=int, default=50, help="1 to 200 (default 50).")
    listing.add_argument("--json", action="store_true")
    show = sub.add_parser("show", allow_abbrev=False,
                          help="Show a candidate: its current revision's changes, its sources and events.")
    show.add_argument("candidate", help="M-0042 or M-42.")
    show.add_argument("--json", action="store_true")
    check = sub.add_parser("check", allow_abbrev=False,
                           help="Check what a write needs now (setting, git, branch, worktree folder); changes nothing.")
    check.add_argument("--json", action="store_true")
    writes = []
    for name, help_text in (("reject", "Reject an unwritten memory candidate."),
                            ("retry", "Retry a blocked write, preserving its confirmed content."),
                            ("delete", "Delete a candidate, leaving its fingerprint tombstone and vault notes.")):
        command = sub.add_parser(name, allow_abbrev=False, help=help_text)
        command.add_argument("candidate", help="M-0042 or M-42.")
        if name == "reject":
            command.add_argument("--reason", help="Why this memory is not useful (at most 300 characters).")
        writes.append(command)
    for command in (listing, show, check, *writes):
        _add_location_flags(command)


def _add_decisions_parser(commands: argparse._SubParsersAction) -> None:
    decisions = commands.add_parser(
        "decisions", allow_abbrev=False, help="Replay a task's decision snapshots.",
        description="Decision snapshots record what a task's dispatch passes and its Planner's step decisions saw and "
                    "decided, while the system setting decision_snapshots.enabled is true. This reads them from the "
                    "database directly, so it works with the service stopped, and writes nothing. Exit codes: 0 every "
                    "snapshot replays to its recorded decision, 1 none recorded, 3 some differ, 6 database unusable.")
    sub = decisions.add_subparsers(dest="decisions_command", required=True)
    replay = sub.add_parser("replay", allow_abbrev=False,
                            help="Run the decision code again on each snapshot's inputs and compare.")
    replay.add_argument("task", help="The task's id.")
    replay.add_argument("--json", action="store_true")
    _add_location_flags(replay)


def _add_location_flags(command: argparse.ArgumentParser) -> None:
    command.add_argument("--runtime-root", help="The state directory (default ~/.looper).")
    command.add_argument("--config", help="The ops file (default <runtime root>/ops/server.toml).")


def _add_service_parser(commands: argparse._SubParsersAction) -> None:
    service = commands.add_parser(
        "service",
        allow_abbrev=False,
        help="Run `looper serve` as a background service: started at login and again after a crash.",
        description=(
            "Run `looper serve` as a background service: a launchd LaunchAgent on macOS, a systemd user unit on "
            "Linux. It starts at login, starts again after a crash, and logs to <runtime root>/logs/serve.log."
        ),
    )
    service_commands = service.add_subparsers(dest="service_command", required=True)
    install = service_commands.add_parser(
        "install", allow_abbrev=False, help="Install (or update) the service and start it.",
        description=(
            "Write the unit and (re)start the service, then wait until it serves. The unit runs this looper in the "
            "current directory with the ops file and the runtime root (absolute --config and --runtime-root; "
            "--port when given, else the installed unit's port when server.port is unset), and with the variables "
            "it needs copied from this environment: PATH, locale, "
            "time zone, proxy and certificate settings, the runtimes' directories and logins (CLAUDE_CONFIG_DIR, "
            "CODEX_HOME, GROK_HOME, ANTHROPIC_API_KEY and the others an agent gets), LOOPER_TELEGRAM_BOT_TOKEN, and "
            "each --env NAME. No deployment setting is copied: the ops file names it (run `looper config init-ops` "
            "first). The unit file is private (mode 0600) because it can hold API keys. Running it again updates "
            "the unit and restarts the service."
        ),
    )
    install.add_argument("--port", type=int,
                         help="Port for `looper serve`, written into the unit (default: the ops file's, else 8767).")
    _add_location_flags(install)
    probe = service_commands.add_parser(
        "probe", allow_abbrev=False,
        help="Check once whether the service answers and alert through Lark when it has not for a while "
             "(run by the probe unit every minute).",
    )
    probe.add_argument("--port", type=int, help="Port to check (default: the ops file's server.port, else 8767).")
    _add_location_flags(probe)
    install.add_argument("--env", action="append", default=[], metavar="NAME",
                         help="Also copy this environment variable into the unit (repeatable).")
    restart = service_commands.add_parser(
        "restart", allow_abbrev=False, help="Stop and start the service; wait until it serves.")
    for command in (install, restart):
        command.add_argument("--timeout", type=float, default=120.0,
                             help="Seconds to wait for the service to answer (default 120).")
    upgrade = service_commands.add_parser(
        "upgrade", allow_abbrev=False, help="Plan, drain, install and restart the changed components.")
    upgrade.add_argument("--dry-run", action="store_true", help="Print the upgrade plan only.")
    upgrade.add_argument("--to", metavar="REF_OR_WHEEL", help="Target Git ref (default origin/main) or wheel path.")
    upgrade.add_argument("--start-timeout", "--timeout", dest="timeout", type=float, default=300.0,
                         metavar="SECONDS", help="Seconds to wait for the restarted service to answer (default 300).")
    upgrade.add_argument("--turns-timeout", type=float, default=180.0,
                         help="Seconds to wait once only turns block a control-plane upgrade; "
                              "resets if a non-keepable run reappears (default 180, capped by --drain-timeout).")
    upgrade.add_argument("--preflight-wait", type=float, default=0.0,
                         help="With --no-force on a control-plane upgrade, wait for non-keepable runs "
                              "before draining, polling every 15 seconds (default 0, disabled).")
    restart.add_argument("--drain", action="store_true", help="Drain active runs before restarting.")
    for command in (upgrade, restart):
        command.add_argument("--drain-timeout", type=float, default=1200.0,
                             help="Seconds to drain before stopping (default 1200).")
        command.add_argument("--no-force", action="store_true", help="Cancel the drain at its deadline.")
    status = service_commands.add_parser(
        "status", allow_abbrev=False,
        help="Show whether the service is installed and running, its PID, last exit and log, and POST /api/v1/system/status.",
        description="Exits 0 when the service runs and answers on its port, else 1.")
    uninstall = service_commands.add_parser(
        "uninstall", allow_abbrev=False, help="Stop the service and remove its unit.")
    for command in (install, restart, upgrade, status, uninstall):
        command.add_argument("--label", default="com.looper.serve",
                             help="Service name: the launchd label or systemd unit name (default com.looper.serve).")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        payload = {"ok": True, "schema_version": "looper_version_v0", "name": "looper", "version": __version__}
        print(json.dumps(payload, indent=2) if args.format == "json" else f"looper {__version__}")
        return 0
    if args.command == "auth":
        from .agents import auth_commands
        from .ops_config import runtime_root

        root, warning = runtime_root(args.runtime_root)
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
        return (auth_commands.set_password(root, args.user) if args.auth_command == "set-password"
                else auth_commands.clear_password(root))
    if args.command == "doctor":
        from .agents.self_check import doctor

        ops = _load_ops(args)
        return 1 if ops is None else doctor(ops)
    if args.command == "config":
        return _init_ops(args)
    if args.command == "service":
        return _service(args)
    if args.command == "msgflow":
        from .agents import msgflow_commands

        return msgflow_commands.run(args, _load_ops)
    if args.command == "ledger":
        from .agents import ledger_commands

        return ledger_commands.run(args, _load_ops)
    if args.command == "leads":
        from .agents import leads_commands

        return leads_commands.run(args, _load_ops)
    if args.command == "memory":
        from .agents import memory_commands

        return memory_commands.run(args, _load_ops)
    if args.command == "decisions":
        from .agents import decision_commands

        return decision_commands.run(args, _load_ops)
    if args.command == "profile":
        from .agents import profile_commands

        return profile_commands.show(args)
    if args.command == "claude-code":
        from .agents import claude_code_entry

        return claude_code_entry.run(args)
    if args.command == "mcp-server":
        from .agents import mcp_server

        return mcp_server.main()
    # Imported here so that `looper version` and `--version` stay instant.
    from .agents.serve import serve_agent

    serve_agent(
        host=args.host,
        port=args.port,
        runtime_root=Path(args.runtime_root) if args.runtime_root else None,
        config=Path(args.config) if args.config else None,
        work_dir=Path.cwd(),
        verbose=args.verbose,
        skip_backup=args.skip_backup,
    )
    return 0


def _load_ops(args: argparse.Namespace, port: int | None = None, *, require_database: bool = True) -> OpsConfig | None:
    """The ops settings for this command's --runtime-root, --config (and ``port``), with their warnings printed; None
    (the error printed) when they cannot be used."""
    from . import ops_config

    try:
        ops = ops_config.load(ops_config.Flags(runtime_root=args.runtime_root, config=args.config, port=port),
                              require_database=require_database)
    except ops_config.OpsConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    for warning in ops.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return ops


def _init_ops(args: argparse.Namespace) -> int:
    from . import ops_config

    try:
        ops, notes = ops_config.init_file(ops_config.Flags(runtime_root=args.runtime_root, config=args.config))
    except (ops_config.OpsConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    lines = [f"wrote {ops.path} (mode 0600); `looper serve` reads it at every start"]
    lines += ["  values taken (not shown):", *(f"    {note}" for note in notes)] if notes else []
    print("\n".join(lines))
    return 0


def _service(args: argparse.Namespace) -> int:
    if args.service_command == "probe":
        from .agents import probe

        ops = _load_ops(args, port=args.port, require_database=False)
        if ops is None:
            return 1
        try:
            return probe.run(ops, ops.runtime_root)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    from .agents import service

    host = service.Host.current()
    try:
        if args.service_command == "install":
            ops = _load_ops(args, port=args.port)
            if ops is None:
                return 1
            print(service.install(host, ops, label=args.label, port=args.port, extra_env=args.env,
                                  timeout=args.timeout))
        elif args.service_command == "upgrade":
            print(service.upgrade(host, label=args.label, to=args.to, dry_run=args.dry_run,
                                  timeout=args.timeout, drain_timeout=args.drain_timeout,
                                  turns_timeout=args.turns_timeout, no_force=args.no_force,
                                  preflight_wait=args.preflight_wait))
        elif args.service_command == "restart":
            print(service.restart(host, label=args.label, timeout=args.timeout, drain=args.drain,
                                  drain_timeout=args.drain_timeout, no_force=args.no_force))
        elif args.service_command == "uninstall":
            print(service.uninstall(host, label=args.label))
        else:
            code, report = service.status(host, label=args.label)
            print(report)
            return code
    except (service.ServiceError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
