"""The model_name_parameters migration, in its released execution order."""

from looper.agents.model_names import MODEL_MAX_CHARS

name = "model_name_parameters"
order = 10
pending_order = 50


async def pending(conn):
    from .. import connection as legacy

    return await legacy._model_name_parameters_pending(conn)


async def apply(conn, *, sql, state):
    from .. import connection as legacy

    if await legacy._model_name_parameters_pending(conn):
        # Use the application's grammar rather than maintaining a second
        # regex in SQL. NOT VALID preserves historical malformed names on
        # upgrade; all new inserts/updates still enforce the new grammar.
        pattern = legacy.MODEL_NAME_PATTERN.replace("'", "''")
        for field in ("name", "background_model"):
            constraint = f"llm_models_{field}_check"
            check = f"length({field}) BETWEEN 1 AND {MODEL_MAX_CHARS} AND {field} ~ '{pattern}'"
            await conn.execute(
                f"ALTER TABLE llm_models DROP CONSTRAINT {constraint}, "
                f"ADD CONSTRAINT {constraint} CHECK ({check}) NOT VALID"
            )
            # Clean upgrades must have the same validated catalog as fresh databases.
            if state == "empty" or not await conn.fetchval(
                f"SELECT EXISTS (SELECT 1 FROM llm_models WHERE NOT ({check}))"
            ):
                await conn.execute(
                    f"ALTER TABLE llm_models VALIDATE CONSTRAINT {constraint}"
                )
        if state != "empty":
            await conn.execute(
                "INSERT INTO schema_migrations (name) VALUES ($1)",
                legacy._MODEL_NAME_PARAMETERS,
            )
