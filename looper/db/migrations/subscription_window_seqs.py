"""GIN index for exact membership in ``subscription_runs.window_seqs``.

The table grows with every subscription run. A fresh database creates the index
with the rest of the schema. An upgrade records the migration, then builds the
same index concurrently so the build does not hold a writer lock until commit.
"""

from . import OnlineStep

name = "subscription_window_seqs"
order = 1030
pending_order = 1030

_INDEX = (
    "idx_subscription_runs_window_seqs",
    "ON subscription_runs USING gin (window_seqs)",
)
fresh_ddl = f"CREATE INDEX IF NOT EXISTS {_INDEX[0]} {_INDEX[1]};\n"


async def pending(conn) -> bool:
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    # A fresh database already applied fresh_ddl and records only the baseline marker.
    return not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", _INDEX[0])


async def apply(conn, *, sql, state):
    if not await pending(conn):
        return
    # The column already exists. The index is built in the online step, outside this transaction.
    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)


online_order = 1030
online = (OnlineStep(name, f"{name}_online", indexes=(_INDEX,)),)
