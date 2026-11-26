"""The run_tree migration, in its released execution order."""

from . import OnlineStep

name = "run_tree"
order = 370
pending_order = 330


async def pending(conn):
    from .. import connection as legacy

    return await legacy._run_tree_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._run_tree_pending(conn):
        did_work = True
        for ddl in legacy._run_tree_ddl():
            await conn.execute(ddl)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._RUN_TREE
        )
    return did_work


online_order = 10
online = (
    OnlineStep(
        migration="run_tree",
        marker="run_tree_online",
        validate=(
            ("llm_records", "llm_records_run_kind_check"),
            ("llm_records", "llm_records_parent_record_id_fkey"),
        ),
        indexes=(
            (
                "idx_llm_records_parent",
                "ON llm_records(parent_record_id) WHERE parent_record_id IS NOT NULL",
            ),
        ),
    ),
)
