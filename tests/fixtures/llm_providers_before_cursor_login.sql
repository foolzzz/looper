-- The provider table as released before the Cursor login provider.
CREATE TABLE IF NOT EXISTS llm_providers (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    auth                TEXT NOT NULL CHECK (auth IN ('oauth', 'api_key')),
    oauth_runtime_type  TEXT,
    -- At most this many runs at once on the provider; NULL = no limit.
    max_concurrency     INTEGER CHECK (max_concurrency > 0),
    builtin             BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((auth = 'oauth') = (oauth_runtime_type IS NOT NULL)),
    env JSONB NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(env) = 'array'),
    vendor_type TEXT NOT NULL
    CHECK (vendor_type IN ('glm-bigmodel', 'glm-zai', 'deepseek', 'kimi-moonshot-cn', 'kimi-moonshot',
                          'cpa', 'opencode', 'sub2api', 'claude-subscription', 'codex-login', 'grok-login', 'custom'))
    CHECK (
        (vendor_type = 'claude-subscription' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'claude-code')
        OR (vendor_type = 'codex-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'codex')
        OR (vendor_type = 'grok-login' AND auth = 'oauth' AND oauth_runtime_type IS NOT NULL
            AND oauth_runtime_type = 'grok')
        OR (vendor_type NOT IN ('claude-subscription', 'codex-login', 'grok-login')
            AND auth = 'api_key' AND oauth_runtime_type IS NULL)
    )
);
