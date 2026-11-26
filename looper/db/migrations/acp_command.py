"""Pair a command-owned OAuth identity only with the ACP Reviewer runtime."""

name = "acp_command"
order = 1120
pending_order = 1120
fresh_ddl = """
ALTER TABLE llm_providers
    DROP CONSTRAINT llm_providers_vendor_type_check,
    DROP CONSTRAINT llm_providers_check1,
    ADD CONSTRAINT llm_providers_vendor_type_check CHECK (vendor_type IN ('glm-bigmodel', 'glm-zai', 'deepseek', 'kimi-moonshot-cn', 'kimi-moonshot',
                          'cpa', 'opencode', 'sub2api', 'claude-subscription', 'codex-login', 'grok-login', 'cursor-login', 'coco-login', 'acp-command', 'custom')),
    ADD CONSTRAINT llm_providers_check1 CHECK (
        (vendor_type = 'claude-subscription' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'claude-code')
        OR (vendor_type = 'codex-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'codex')
        OR (vendor_type = 'grok-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'grok')
        OR (vendor_type = 'cursor-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'cursor-agent')
        OR (vendor_type = 'coco-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'coco')
        OR (vendor_type = 'acp-command' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'acp')
        OR (vendor_type NOT IN ('claude-subscription', 'codex-login', 'grok-login', 'cursor-login', 'coco-login', 'acp-command')
            AND auth = 'api_key' AND oauth_runtime_type IS NULL)
    );
"""


async def pending(conn):
    if not await conn.fetchval("SELECT to_regclass('llm_providers')"):
        return False
    definition = await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = to_regclass('llm_providers') AND conname = 'llm_providers_vendor_type_check'")
    return not definition or 'acp-command' not in definition


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
    return did_work
