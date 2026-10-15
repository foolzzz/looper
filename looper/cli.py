"""The ``looper`` command: ``serve`` starts the agent system, ``service`` runs it in the background, ``doctor``
checks what it needs, ``mcp-server`` serves the Manager's platform tools, ``version`` prints the version.
``loopx``, its name before the rename, is kept as an alias for one release."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__

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
    serve.add_argument("--host", help="Bind host (default 127.0.0.1).")
    serve.add_argument("--port", type=int, help="Port (default 8767).")
    serve.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    serve.add_argument("--runtime-root", help="Override the default ~/.looper state directory.")
    serve.add_argument(
        "--skip-backup", action="store_true",
        help="Change the database schema without the pg_dump backup otherwise taken first into "
             "<runtime root>/backups (logged as a warning).",
    )
    commands.add_parser(
        "doctor",
        allow_abbrev=False,
        help="Check what Looper needs: PostgreSQL, the Claude Code CLI and its login, the enabled channels "
             "and pg_dump.",
        description=(
            "Check PostgreSQL (LOOPER_DATABASE_URL), the Claude Code CLI and its login, the enabled channels, and "
            "the pg_dump that backs the database up before a schema change; print each result with its fix. "
            "Exits 1 when a check failed. Makes no model call."
        ),
    )
    config = commands.add_parser(
        "config",
        allow_abbrev=False,
        help="Export this installation's agent configuration to one file, or import one.",
        description=(
            "Move an installation's agent configuration to another machine: settings layers and role prompts, "
            "the catalog with its files and MCP servers, global installs, channels and, optionally, projects. "
            "Both commands use the database directly (LOOPER_DATABASE_URL); the server need not run."
        ),
    )
    config_commands = config.add_subparsers(dest="config_command", required=True)
    export = config_commands.add_parser(
        "export", allow_abbrev=False, help="Write the configuration file (reads the database only).",
        description="Write the configuration file. Secrets are left out as placeholders unless --include-secrets.",
    )
    export.add_argument("file", help="The file to write (JSON).")
    export.add_argument("--include-projects", action="store_true",
                        help="Also export projects with their settings, knowledge, own skills and installs.")
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
            "current directory, with the variables it needs copied from this environment: PATH, locale and proxy "
            "settings, CLAUDE_CONFIG_DIR, every LOOPER_*, ANTHROPIC_* and LARK_* variable (and, for one release, "
            "LOOPX_*), and each --env NAME. The unit file is private (mode 0600) because LOOPER_DATABASE_URL can "
            "hold a password. Running it again updates the unit and restarts the service. With the default "
            "--label it first stops and removes com.loopx.serve, the unit from before the rename, and moves "
            "~/.loopx to ~/.looper when the service uses the default runtime root."
        ),
    )
    install.add_argument("--port", type=int, default=8767, help="Port for `looper serve` (default 8767).")
    install.add_argument("--runtime-root",
                         help="State directory for `looper serve` (default LOOPER_RUNTIME_ROOT, else ~/.looper).")
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
    if args.command == "doctor":
        from .agents.self_check import doctor

        return doctor()
    if args.command == "config":
        return _config(args)
    if args.command == "service":
        return _service(args)
    if args.command == "mcp-server":
        from .agents import mcp_server

        return mcp_server.main()
    # Imported here so that `looper version` and `--version` stay instant.
    from .agents.serve import serve_agent
    from .paths import default_runtime_root

    serve_agent(
        host=args.host or "127.0.0.1",
        port=args.port or 8767,
        runtime_root=Path(args.runtime_root) if args.runtime_root else default_runtime_root(),
        work_dir=Path.cwd(),
        verbose=args.verbose,
        skip_backup=args.skip_backup,
    )
    return 0


def legacy_main(argv: list[str] | None = None) -> int:
    """``loopx``, kept for one release: a deprecation note on stderr, then the same command as ``looper``."""
    print(LEGACY_COMMAND_NOTE, file=sys.stderr)
    return main(argv)


def _config(args: argparse.Namespace) -> int:
    import asyncio

    import asyncpg

    from .agents import config_transfer
    from .db.connection import configured_dsn

    path = Path(args.file)
    try:
        if args.config_command == "export":
            if args.include_secrets:
                print(f"warning: {path} will hold secrets (LLM provider API keys, MCP environment and header values, "
                      "channel secrets) in plain text; keep it private and delete it once imported", file=sys.stderr)
            summary = asyncio.run(config_transfer.export_to_file(
                configured_dsn(), path, include_projects=args.include_projects, include_secrets=args.include_secrets))
            print(config_transfer.format_export(summary, path, include_secrets=args.include_secrets))
            return 0
        plan = asyncio.run(config_transfer.import_file(configured_dsn(), path, apply=args.apply))
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
    from .paths import default_runtime_root

    host = service.Host.current()
    try:
        if args.service_command == "install":
            root = Path(args.runtime_root) if args.runtime_root else default_runtime_root()
            print(service.install(host, label=args.label, port=args.port, runtime_root=root,
                                  extra_env=args.env, timeout=args.timeout))
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
