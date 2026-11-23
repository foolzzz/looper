"""Construct historical upgrade fixtures before the platform subagents migration."""

from typing import Any
from unittest.mock import patch

from looper.db import connection, migrations


async def ensure_before_platform_subagents(conn: Any) -> None:
    """Initialize an empty schema, then restore the real migration registry for its upgrade."""
    modules = tuple(module for module in connection._MIGRATIONS if module.name != "platform_subagents")
    with patch.multiple(connection, _MIGRATIONS=modules,
                        _PENDING_MIGRATIONS=migrations.pending_migrations(modules),
                        _ONLINE_STEPS=migrations.online_steps(modules)):
        await connection.ensure_schema(conn)
