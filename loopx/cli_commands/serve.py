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
    parser.add_argument(
        "--enable-telegram",
        action="store_true",
        help="Enable Telegram channel adapter (requires LOOPX_TELEGRAM_TOKEN).",
    )
    parser.add_argument(
        "--enable-lark",
        action="store_true",
        help="Enable Lark channel adapter.",
    )
    parser.add_argument(
        "--lark-app-id",
        default="",
        help="Lark app ID for the Lark channel adapter.",
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
        enable_telegram=getattr(args, "enable_telegram", False),
        enable_lark=getattr(args, "enable_lark", False),
        lark_app_id=getattr(args, "lark_app_id", ""),
    )
    return 0
