-- Builtin rows are initialized on every startup without overwriting saved choices.
INSERT INTO managers (id, name, tools, task_scope, idle_timeout_seconds)
    VALUES ('manager', 'Manager', 'all', 'all', 0) ON CONFLICT (id) DO NOTHING;

-- Builtin seeds never overwrite operator choices.
-- Login endpoints use the CLI's own connection; no token or URL is copied.
INSERT INTO agent_runtimes (id, name, type, executable, capabilities, builtin)
VALUES ('rt-claude-code', 'claude-code', 'claude-code', '',
        ARRAY['add_dirs', 'autonomous_turns', 'claude_plugins', 'compaction_events', 'cost_report', 'mcp',
              'resident_session', 'resume', 'run'], true)
ON CONFLICT DO NOTHING;
INSERT INTO llm_providers (id, name, vendor_type, auth, oauth_runtime_type, builtin)
VALUES ('prov-claude-subscription', 'claude-subscription', 'claude-subscription', 'oauth', 'claude-code', true),
       ('prov-codex-login', 'codex-login', 'codex-login', 'oauth', 'codex', true),
       ('prov-grok-login', 'grok-login', 'grok-login', 'oauth', 'grok', true)
ON CONFLICT DO NOTHING;
INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url)
VALUES ('prov-claude-subscription', 'anthropic-messages', ''),
       ('prov-codex-login', 'openai-responses', ''),
       ('prov-grok-login', 'openai-responses', '')
ON CONFLICT (provider_id, protocol) DO NOTHING;

-- Fixed UUIDv5 ids for the pinned login suggestions, matched by the complete
-- natural key on readback; restarting never resets metadata or allocates ids.
INSERT INTO llm_models (id, provider_id, name, protocol, catalog_order, builtin)
VALUES
    ('model-84f4b69e-12a7-5fd7-b665-a9b7d01568eb', 'prov-claude-subscription', 'opus', 'anthropic-messages', 0, true),
    ('model-c0d66d22-9bfd-5fbe-b6d5-5626efd7d46e', 'prov-claude-subscription', 'sonnet', 'anthropic-messages', 1, true),
    ('model-47a35ff0-e373-50bc-8633-e0d5f1bfbf53', 'prov-claude-subscription', 'haiku', 'anthropic-messages', 2, true),
    ('model-7268a107-dade-549c-a62f-d65c2c459488', 'prov-codex-login', 'gpt-6.1-sol', 'openai-responses', 0, true),
    ('model-c56d0439-b240-5650-aa9d-f1d561e5b577', 'prov-codex-login', 'gpt-6-sol', 'openai-responses', 1, true),
    ('model-1a5dc62a-a91f-5fe2-bac1-4db37f68854f', 'prov-codex-login', 'gpt-6-astra', 'openai-responses', 2, true),
    ('model-bc53b78f-8626-5cc6-8114-cc29b2b13c23', 'prov-codex-login', 'gpt-6-luna', 'openai-responses', 3, true),
    ('model-7bfb6763-93d9-5740-869d-435691e83fcb', 'prov-codex-login', 'gpt-5.6-sol', 'openai-responses', 4, true),
    ('model-95fdbced-a8cd-5775-80c7-5abeeb1b8be9', 'prov-codex-login', 'gpt-5.6-terra', 'openai-responses', 5, true),
    ('model-2f901fbc-ea04-5450-ac57-96c8ba5f46ca', 'prov-codex-login', 'gpt-5.6-luna', 'openai-responses', 6, true),
    ('model-a34a618b-2cfd-5151-966b-6edcfb4e5992', 'prov-grok-login', 'grok-4.7', 'openai-responses', 0, true),
    ('model-96ad8acd-53da-59e2-859f-829db99b8422', 'prov-grok-login', 'grok-4.7-build-fast', 'openai-responses', 1, true),
    ('model-600929a8-fd29-521b-ad52-ee5cc69914d8', 'prov-grok-login', 'grok-4.6', 'openai-responses', 2, true),
    ('model-e1d7d77c-276f-5e28-8a9b-2ecf480f0f91', 'prov-grok-login', 'grok-4.5', 'openai-responses', 3, true)
ON CONFLICT (provider_id, name, protocol) DO NOTHING;
INSERT INTO agent_profiles (id, name, runtime_id, model_id, builtin, agent_teams)
SELECT 'agent-claude-code-opus', 'claude-code/opus', 'rt-claude-code', id, true, true
FROM llm_models WHERE provider_id = 'prov-claude-subscription' AND name = 'opus' AND protocol = 'anthropic-messages'
ON CONFLICT DO NOTHING;
