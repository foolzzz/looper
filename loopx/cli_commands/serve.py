"""``loopx serve``: start the three-tier agent system."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path


def register_serve_command(
    subparsers: argparse._SubParsersAction,
    add_subcommand_format: Callable[[argparse.ArgumentParser], None],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "serve",
        help="Start the LoopX agent system.",
        description=(
            "Start the LoopX agent system. Telegram and Lark need no flags: "
            "enable and configure them on the dashboard Settings page, then "
            "restart the server."
        ),
    )
    add_subcommand_format(parser)
    parser.add_argument(
        "--host", default=None, help="Bind host (default 127.0.0.1)."
    )
    parser.add_argument("--port", type=int, default=None, help="Port (default 8767).")
    parser.add_argument(
        "--verbose", action="store_true", help="Enable debug logging."
    )
    parser.add_argument(
        "--runtime-root",
        help="Override the default ~/.loopx state directory.",
    )
    return parser


def handle_serve_command(args, _print_payload) -> int | None:
    if getattr(args, "command", None) != "serve":
        return None
    return _run_serve(args)


def _run_serve(args) -> int:
    from ..agents.serve import serve_agent
    from ..paths import default_runtime_root

    runtime_root_override = getattr(args, "runtime_root", None)
    runtime_root = Path(runtime_root_override) if runtime_root_override else default_runtime_root()

    serve_agent(
        host=getattr(args, "host", None) or "127.0.0.1",
        port=getattr(args, "port", None) or 8767,
        runtime_root=runtime_root,
        work_dir=Path.cwd(),
        verbose=getattr(args, "verbose", False),
    )
    return 0
