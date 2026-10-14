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


if __name__ == "__main__":
    raise SystemExit(main())
