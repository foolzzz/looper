"""The mid_turn_input migration, in its released execution order."""

from . import OnlineStep

name = "mid_turn_input"
order = 180
pending_order = 560


async def pending(conn):
    from .. import connection as legacy

    return await legacy._mid_turn_input_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._mid_turn_input_pending(conn):
        await conn.execute(legacy._MID_TURN_INPUT_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._MID_TURN_INPUT
        )


online_order = 80
online = (
    OnlineStep(
        migration="mid_turn_input",
        marker="mid_turn_input_online",
        validate=(("agent_runtimes", "agent_runtimes_capabilities_check"),),
        indexes=(),
    ),
)
