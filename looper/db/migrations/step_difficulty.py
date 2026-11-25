"""The step_difficulty migration, in its released execution order."""

from . import OnlineStep

name = "step_difficulty"
order = 380
pending_order = 340


async def pending(conn):
    from .. import connection as legacy

    return await legacy._step_difficulty_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._step_difficulty_pending(conn):
        did_work = True
        await conn.execute("ALTER TABLE task_steps ADD COLUMN difficulty TEXT")
        await conn.execute(
            "ALTER TABLE task_steps ADD CONSTRAINT task_steps_difficulty_check "
            "CHECK (difficulty IN ('routine', 'hard', 'critical')) NOT VALID"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._STEP_DIFFICULTY
        )
    return did_work


online_order = 20
online = (
    OnlineStep(
        migration="step_difficulty",
        marker="step_difficulty_online",
        validate=(("task_steps", "task_steps_difficulty_check"),),
        indexes=(),
    ),
)
