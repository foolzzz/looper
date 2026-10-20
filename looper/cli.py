"""The ``looper`` command: ``serve`` starts the agent system, ``service`` runs it in the background, ``doctor``
checks what it needs, ``mcp-server`` serves the Manager's platform tools, ``version`` prints the version.
``loopx``, its name before the rename, is kept as an alias for one release."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:
    from .ops_config import OpsConfig

LEGACY_COMMAND_NOTE = "loopx is now called looper; the loopx alias goes away in the next release"


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
        help="Export this installation's agent configuration to one file, or import one.",
        description=(
            "Move an installation's agent configuration as looper-config/v3: providers with per-protocol endpoints, "
            "models and profiles, settings layers and role prompts, "
            "the catalog with its files and MCP servers, global installs, channels and, optionally, projects. "
            "Both commands use the database directly (the ops file's database.url); the server need not run. "
            "init-ops writes the ops file itself."
        ),
    )
    config_commands = config.add_subparsers(dest="config_command", required=True)
    export = config_commands.add_parser(
        "export", allow_abbrev=False, help="Write the configuration file (reads the database only).",
        description="Write the configuration file. Secrets are left out as placeholders unless --include-secrets.",
    )
    export.add_argument("file", help="The file to write (JSON).")
    export.add_argument("--include-projects", action="store_true",
                        help="Also export projects with their settings, knowledge, own skills, installs and "
                             "schedules (an import brings schedules in disabled).")
    export.add_argument("--include-secrets", action="store_true",
                        help="Write LLM provider API keys, MCP environment and header values and channel secrets in "
                             "plain text.")
    import_ = config_commands.add_parser(
        "import", allow_abbrev=False, help="Preview importing a configuration file; --apply writes it.",
        description=(
            "Show what importing the file would add, change, leave and skip, without writing; with --apply, write "
            "it in one transaction. Entries match by name; nothing missing from the file is removed, and a "
            "placeholder never replaces a stored secret."
        ),
    )
    import_.add_argument("file", help="The configuration file.")
    import_.add_argument("--apply", action="store_true", help="Write the changes the preview shows.")
    import_.add_argument("--json", action="store_true", help="Print the plan as JSON.")
    init_ops = config_commands.add_parser(
        "init-ops", allow_abbrev=False, help="Write the ops file from the settings in effect now (mode 0600).",
        description=(
            "Write the ops file, <runtime root>/ops/server.toml (or --config), from the settings in effect now: "
            "LOOPER_DATABASE_URL, LOOPER_PUBLIC_URL, LOOPER_PG_DUMP, LOOPER_BACKUP_CMD and "
            "LOOPER_OPERATOR_NOTES_ROOT (or their LOOPX_* names) from the environment or the checkout's .env, which "
            "Looper reads for this release only. It refuses to replace an existing file, and shows no value."
        ),
    )
    for command in (export, import_, init_ops):
        _add_location_flags(command)
    _add_service_parser(commands)
    commands.add_parser(
        "mcp-server",
        allow_abbrev=False,
        help="Serve Looper's platform tools to the Manager over MCP on stdin/stdout; the Manager's runtime starts "
             "it, with LOOPER_AGENT_URL and LOOPER_AGENT_TOKEN.",
    )
    version = commands.add_parser("version", allow_abbrev=False, help="Print the installed Looper version.")
    version.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format.")
    # serve prints no payload; --format stays accepted so existing invocations keep working.
    serve.add_argument("--format", choices=["markdown", "json"], help=argparse.SUPPRESS)
    return parser


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
            "--port only when given), and with the variables it needs copied from this environment: PATH, locale, "
            "time zone, proxy and certificate settings, the runtimes' directories and logins (CLAUDE_CONFIG_DIR, "
            "CODEX_HOME, GROK_HOME, ANTHROPIC_API_KEY and the others an agent gets), LOOPER_TELEGRAM_BOT_TOKEN, and "
            "each --env NAME. No database URL is copied: the ops file names it, and install refuses while a setting "
            "still comes from a variable (run `looper config init-ops` first). The unit file is private (mode 0600) "
            "because it can hold API keys. Running it again updates the unit and restarts the service. With the "
            "default "
            "--label it first stops and removes com.loopx.serve, the unit from before the rename, and moves "
            "~/.loopx to ~/.looper when the service uses the default runtime root."
        ),
    )
    install.add_argument("--port", type=int,
                         help="Port for `looper serve`, written into the unit (default: the ops file's, else 8767).")
    _add_location_flags(install)
    install.add_argument("--env", action="append", default=[], metavar="NAME",
                         help="Also copy this environment variable into the unit (repeatable).")
    restart = service_commands.add_parser(
        "restart", allow_abbrev=False, help="Stop and start the service; wait until it serves.")
    for command in (install, restart):
        command.add_argument("--timeout", type=float, default=120.0,
                             help="Seconds to wait for the service to answer (default 120).")
    status = service_commands.add_parser(
        "status", allow_abbrev=False,
        help="Show whether the service is installed and running, its PID, last exit and log, and GET /api/status.",
        description="Exits 0 when the service runs and answers on its port, else 1.")
    uninstall = service_commands.add_parser(
        "uninstall", allow_abbrev=False, help="Stop the service and remove its unit.")
    for command in (install, restart, status, uninstall):
        command.add_argument("--label", default="com.looper.serve",
                             help="Service name: the launchd label or systemd unit name (default com.looper.serve).")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        return _config(args)
    if args.command == "service":
        return _service(args)
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


def legacy_main(argv: list[str] | None = None) -> int:
    """``loopx``, kept for one release: a deprecation note on stderr, then the same command as ``looper``."""
    print(LEGACY_COMMAND_NOTE, file=sys.stderr)
    return main(argv)


def _load_ops(args: argparse.Namespace, port: int | None = None) -> OpsConfig | None:
    """The ops settings for this command's --runtime-root, --config (and ``port``), with their warnings printed; None
    (the error printed) when they cannot be used."""
    from . import ops_config

    try:
        ops = ops_config.load(ops_config.Flags(runtime_root=args.runtime_root, config=args.config, port=port))
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
    lines.append("then remove those variables from the environment and .env, and if `looper service` runs Looper, run "
                 "`looper service install` again: its unit then names this file and carries no database URL")
    print("\n".join(lines))
    return 0


def _config(args: argparse.Namespace) -> int:
    import asyncio

    import asyncpg

    from .agents import config_transfer

    if args.config_command == "init-ops":
        return _init_ops(args)
    ops = _load_ops(args)
    if ops is None:
        return 1
    path = Path(args.file)
    try:
        if args.config_command == "export":
            if args.include_secrets:
                print(f"warning: {path} will hold secrets (LLM provider API keys, MCP environment and header values, "
                      "channel secrets) in plain text; keep it private and delete it once imported", file=sys.stderr)
            summary = asyncio.run(config_transfer.export_to_file(
                ops.database_url, path, include_projects=args.include_projects, include_secrets=args.include_secrets))
            print(config_transfer.format_export(summary, path, include_secrets=args.include_secrets))
            return 0
        plan = asyncio.run(config_transfer.import_file(ops.database_url, path, apply=args.apply))
    except (config_transfer.ConfigFileError, config_transfer.SchemaChangePending) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(plan.view(), indent=2, ensure_ascii=False) if args.json
          else config_transfer.format_plan(plan, path))
    return 0


def _service(args: argparse.Namespace) -> int:
    from .agents import service

    host = service.Host.current()
    try:
        if args.service_command == "install":
            ops = _load_ops(args, port=args.port)
            if ops is None:
                return 1
            print(service.install(host, ops, label=args.label, port=args.port, extra_env=args.env,
                                  timeout=args.timeout))
        elif args.service_command == "restart":
            print(service.restart(host, label=args.label, timeout=args.timeout))
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
