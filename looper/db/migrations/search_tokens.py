"""Session search's search tokens on fresh and upgraded databases."""

from . import OnlineStep

name = "search_tokens"
order = 670
pending_order = 670
_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION looper_search_tokens(body text) RETURNS text[]
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE COST 10000 AS $$
SELECT coalesce(array_agg(DISTINCT token ORDER BY token), '{}')
FROM (SELECT lower(left(body, 8192)) AS lowered) AS doc,
LATERAL (
    SELECT left(word, 512) AS token
    FROM regexp_split_to_table(regexp_replace(doc.lowered, '[㐀-䶿一-鿿豈-﫿]+', ' ', 'g'),
                               '[^a-z0-9]+') AS word
    WHERE char_length(word) >= 2
    UNION ALL
    SELECT substr(run[1], i, 2)
    FROM regexp_matches(doc.lowered, '[㐀-䶿一-鿿豈-﫿]+', 'g') AS run,
         generate_series(1, greatest(char_length(run[1]) - 1, 1)) AS i
) AS tokens
$$;
"""
_INDEX = (
    "idx_chat_messages_search",
    "ON chat_messages USING gin (array_to_tsvector(looper_search_tokens(content))) "
    "WHERE role IN ('user', 'assistant')",
)
fresh_ddl = _FUNCTION_DDL + f"\nCREATE INDEX IF NOT EXISTS {_INDEX[0]} {_INDEX[1]};\n"


async def pending(conn):
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name = $1)", name):
        return False
    return not await conn.fetchval("SELECT to_regprocedure('looper_search_tokens(text)') IS NOT NULL")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(_FUNCTION_DDL)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)


# The concurrent build tokenizes every existing message without blocking inserts.
online_order = 140
online = (OnlineStep(name, "search_tokens_online", indexes=(_INDEX,)),)
