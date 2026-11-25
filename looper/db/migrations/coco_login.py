"""The coco_login migration, in its released execution order."""

name = "coco_login"
order = 60
pending_order = 60


async def pending(conn):
    from .. import connection as legacy

    return await legacy._coco_login_pending(conn)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    from .. import connection as legacy

    if await legacy._coco_login_pending(conn):
        did_work = True
        await conn.execute(legacy._COCO_LOGIN_DDL)
        await conn.execute(
            "INSERT INTO schema_migrations (name) VALUES ($1)", legacy._COCO_LOGIN
        )
    return did_work
