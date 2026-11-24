"""Bounded, silent profile read for local session-start hooks. No schema or cache writes."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from typing import Any

# Include imports, ops-file reads and synchronous vault I/O in the deadline, not only PostgreSQL awaits.
TIMEOUT_SECONDS = 1.0
MAX_OUTPUT_BYTES = 6000  # the compiler's 1500 Unicode characters, at most four bytes each


def read_text(args: Any) -> str:
    """Read the same resident text for every local CLI consumer; no writes or model calls."""
    from .. import child_env

    if os.environ.get(child_env.AGENT_RUN_ENV):
        return ""
    command = [sys.executable, "-m", __name__]
    for flag in ("runtime_root", "config"):
        if value := getattr(args, flag, None):
            command += ["--" + flag.replace("_", "-"), str(value)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS, check=False)
        if result.returncode == 0 and len(result.stdout) <= MAX_OUTPUT_BYTES:
            return result.stdout.decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    return ""


def show(args: Any) -> int:
    sys.stdout.buffer.write(read_text(args).encode("utf-8"))
    return 0


async def _read(args: Any) -> str:
    from .. import ops_config
    from ..db import connection
    from .operator_profile import resident_text

    pool = None
    try:
        async with asyncio.timeout(0.7):
            ops = ops_config.load(ops_config.Flags(runtime_root=args.runtime_root, config=args.config))
            pool = await connection.init_pool(ops.database_url, min_size=1, max_size=1)
            return await resident_text()
    except Exception:
        return ""
    finally:
        if pool is not None:
            pool.terminate()  # no shutdown wait can hold up a hook


if __name__ == "__main__":
    from ..cli import build_parser

    logging.disable(logging.CRITICAL)
    arguments = build_parser().parse_args(["profile", "show", *sys.argv[1:]])
    sys.stdout.buffer.write(asyncio.run(_read(arguments)).encode("utf-8"))
