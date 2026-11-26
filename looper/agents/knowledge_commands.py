"""Operator CLI for a dry-run-first, non-destructive project knowledge export."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import asyncpg

from .. import child_env
from . import knowledge_migrate


def _backup_notice(backup: dict[str, str]) -> None:
    print(f"backup: {backup['path']} sha256: {backup['sha256']}", file=sys.stderr, flush=True)


async def _command(pool: Any, args: Any) -> tuple[int, str]:
    result = await knowledge_migrate.migrate(pool, args.project, args.runtime_root_path, execute=args.execute,
                                             on_backup=_backup_notice)
    return (0 if result["state"] in {"dry_run", "committed", "unchanged"} else 1,
            json.dumps(result, ensure_ascii=False, indent=2))


async def _run(database_url: str, args: Any) -> tuple[int, str]:
    from ..db.connection import close_pool, init_pool

    try:
        pool = await init_pool(database_url, min_size=1, max_size=2)
        return await _command(pool, args)
    except ValueError as error:
        return 2, str(error)
    except (OSError, asyncpg.PostgresError, TimeoutError) as error:
        return 6, f"knowledge migration failed: {error}"
    finally:
        await close_pool()


def run(args: Any, load_ops: Any) -> int:
    from ..cli import task_manager_guard

    if (code := task_manager_guard("knowledge", args.knowledge_command)) is not None:
        return code
    if os.environ.get(child_env.AGENT_RUN_ENV):
        print("知识迁移只供操作者运行，请在自己的终端运行", file=sys.stderr)
        return 4
    ops = load_ops(args)
    if ops is None:
        return 6
    args.runtime_root_path = Path(ops.runtime_root)
    code, text = asyncio.run(_run(ops.database_url, args))
    print(text, file=sys.stdout if code == 0 or text.startswith("{") else sys.stderr)
    return code
