"""The ``loopx`` command: ``serve`` starts the agent system, ``doctor`` checks what it needs, ``version`` prints the version."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loopx", description="LoopX agent system.", allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"loopx {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve",
        allow_abbrev=False,
        help="Start the LoopX agent system.",
        description=(
            "Start the LoopX agent system. Telegram and Lark need no flags: enable and configure "
            "them on the dashboard Settings page, then restart the server."
        ),
    )
    serve.add_argument("--host", help="Bind host (default 127.0.0.1).")
    serve.add_argument("--port", type=int, help="Port (default 8767).")
    serve.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    serve.add_argument("--runtime-root", help="Override the default ~/.loopx state directory.")
    commands.add_parser(
        "doctor",
        allow_abbrev=False,
        help="Check what LoopX needs: PostgreSQL, the Claude Code CLI and its login, and the enabled channels.",
        description=(
            "Check PostgreSQL (LOOPX_DATABASE_URL), the Claude Code CLI and its login, and the enabled channels; "
            "print each result with its fix. Exits 1 when a check failed. Makes no model call."
        ),
    )
    config = commands.add_parser(
        "config",
        allow_abbrev=False,
        help="Export this installation's agent configuration to one file, or import one.",
        description=(
            "Move an installation's agent configuration to another machine: settings layers and role prompts, "
            "the catalog with its files and MCP servers, global installs, channels and, optionally, projects. "
            "Both commands use the database directly (LOOPX_DATABASE_URL); the server need not run."
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
                        help="Write MCP environment and header values and channel secrets in plain text.")
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
    version = commands.add_parser("version", allow_abbrev=False, help="Print the installed LoopX version.")
    version.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Output format.")
    # serve prints no payload; --format stays accepted so existing invocations keep working.
    serve.add_argument("--format", choices=["markdown", "json"], help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        payload = {"ok": True, "schema_version": "loopx_version_v0", "name": "loopx", "version": __version__}
        print(json.dumps(payload, indent=2) if args.format == "json" else f"loopx {__version__}")
        return 0
    if args.command == "doctor":
        from .agents.self_check import doctor

        return doctor()
    if args.command == "config":
        return _config(args)
    # Imported here so that `loopx version` and `--version` stay instant.
    from .agents.serve import serve_agent
    from .paths import default_runtime_root

    serve_agent(
        host=args.host or "127.0.0.1",
        port=args.port or 8767,
        runtime_root=Path(args.runtime_root) if args.runtime_root else default_runtime_root(),
        work_dir=Path.cwd(),
        verbose=args.verbose,
    )
    return 0


def _config(args: argparse.Namespace) -> int:
    import asyncio
    import sys

    import asyncpg

    from .agents import config_transfer
    from .db.connection import configured_dsn

    path = Path(args.file)
    try:
        if args.config_command == "export":
            if args.include_secrets:
                print(f"warning: {path} will hold secrets (MCP environment and header values, channel secrets) in "
                      "plain text; keep it private and delete it once imported", file=sys.stderr)
            summary = asyncio.run(config_transfer.export_to_file(
                configured_dsn(), path, include_projects=args.include_projects, include_secrets=args.include_secrets))
            print(config_transfer.format_export(summary, path, include_secrets=args.include_secrets))
            return 0
        plan = asyncio.run(config_transfer.import_file(configured_dsn(), path, apply=args.apply))
    except config_transfer.ConfigFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(plan.view(), indent=2, ensure_ascii=False) if args.json
          else config_transfer.format_plan(plan, path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
