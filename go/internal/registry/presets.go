package registry

// PresetsView is provider_presets.presets_view. Prices are not part of a preset.
func PresetsView() []any {
	return []any{
		preset(
			"glm-bigmodel", "GLM (BigModel, China)", "glm",
			[]any{
				api([]any{"anthropic-messages"}, "https://open.bigmodel.cn/api/anthropic"),
				api([]any{"openai-chat"}, "https://open.bigmodel.cn/api/paas/v4"),
				api([]any{"openai-responses"}, "https://open.bigmodel.cn/api/v1"),
			},
			glmModels(), glmEnv(),
			[]any{
				"https://docs.bigmodel.cn/cn/coding-plan/tool/claude",
				"https://docs.bigmodel.cn/cn/guide/start/quick-start",
				"https://docs.bigmodel.cn/cn/guide/start/model-overview",
			},
			"BigModel bills in CNY; Looper prices these models at Z.ai's USD list prices unless the catalog says otherwise",
		),
		preset(
			"glm-zai", "GLM (Z.ai, international)", "glm-zai",
			[]any{
				api([]any{"anthropic-messages"}, "https://api.z.ai/api/anthropic"),
				api([]any{"openai-chat"}, "https://api.z.ai/api/paas/v4"),
				api([]any{"openai-responses"}, "https://api.z.ai/api/v1"),
			},
			glmModels(), glmEnv(),
			[]any{
				"https://docs.z.ai/devpack/tool/claude",
				"https://docs.z.ai/guides/overview/quick-start",
				"https://docs.z.ai/guides/llm/glm-5.3",
			},
			"",
		),
		preset(
			"deepseek", "DeepSeek", "deepseek",
			[]any{
				api([]any{"anthropic-messages"}, "https://api.deepseek.com/anthropic"),
				api([]any{"openai-chat"}, "https://api.deepseek.com"),
			},
			[]any{
				model("deepseek-flash[1m]", 1_000_000, "deepseek-flash"),
				model("deepseek-v4-pro[1m]", 1_000_000, "deepseek-flash"),
			},
			[]any{envPair("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "786432")},
			[]any{
				"https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/",
				"https://api-docs.deepseek.com/guides/anthropic_api",
				"https://api-docs.deepseek.com/",
			},
			"",
		),
		preset(
			"kimi-moonshot-cn", "Kimi (Moonshot, China)", "kimi",
			[]any{
				api([]any{"anthropic-messages"}, "https://api.moonshot.cn/anthropic"),
				api([]any{"openai-responses", "openai-chat"}, "https://api.moonshot.cn/v1"),
			},
			kimiModels(), kimiEnv(),
			[]any{
				"https://platform.kimi.com/docs/guide/claude-code-kimi.md",
				"https://platform.kimi.com/docs/api/overview",
			},
			kimiNote+"; Moonshot China bills in CNY, and Looper prices these models at the international USD list prices unless the catalog says otherwise",
		),
		preset(
			"kimi-moonshot", "Kimi (Moonshot, international)", "kimi-intl",
			[]any{
				api([]any{"anthropic-messages"}, "https://api.moonshot.ai/anthropic"),
				api([]any{"openai-responses", "openai-chat"}, "https://api.moonshot.ai/v1"),
			},
			kimiModels(), kimiEnv(),
			[]any{
				"https://platform.kimi.ai/docs/guide/claude-code-kimi.md",
				"https://platform.kimi.ai/docs/api/overview.md",
			},
			kimiNote,
		),
		preset(
			"cpa", "CLIProxyAPI on this machine", "cpa",
			[]any{
				api([]any{"openai-responses", "openai-chat"}, "http://127.0.0.1:8317/v1"),
				api([]any{"anthropic-messages"}, "http://127.0.0.1:8317"),
			},
			[]any{
				model("gpt-6.1-sol", 1_050_000, "gpt-6-luna"),
				model("gpt-5.6-sol", 1_050_000, "gpt-5.6-luna"),
				model("gpt-5.6-luna", 1_050_000, "gpt-5.6-luna"),
			},
			[]any{},
			[]any{},
			"a local proxy for development: its key is one of the api-keys in its config file, and its models are those it serves",
		),
	}
}

const kimiNote = "kimi-k2.7-code always thinks; Moonshot's Claude Code guide uses it as the background model, and asks for thinking to be on"

func glmModels() []any {
	return []any{
		model("glm-5.3[1m]", 1_000_000, "glm-5.3-flash[1m]"),
		model("glm-5.3-flash[1m]", 1_000_000, "glm-5.3-flash[1m]"),
		model("glm-5.2[1m]", 1_000_000, "glm-5.3-flash[1m]"),
		model("glm-4.7", 200_000, "glm-4.5-air"),
	}
}

func glmEnv() []any {
	return []any{
		envPair("API_TIMEOUT_MS", "3000000"),
		envPair("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000"),
		envPair("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"),
	}
}

func kimiModels() []any {
	return []any{
		model("kimi-k3[1m]", 1_048_576, "kimi-k2.7-code"),
		model("kimi-k2.7-code", 262_144, "kimi-k2.7-code"),
		model("kimi-k2.6", 262_144, "kimi-k2.6"),
	}
}

func kimiEnv() []any {
	return []any{envPair("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000")}
}

func preset(id, label, name string, apis, models, env, docs []any, note string) map[string]any {
	return map[string]any{
		"preset_id": id, "label": label, "name": name,
		"apis": apis, "models": models, "env": env, "docs": docs, "note": note,
	}
}

func api(protocols []any, baseURL string) map[string]any {
	return map[string]any{"protocols": protocols, "base_url": baseURL}
}

func model(name string, window int, background string) map[string]any {
	return map[string]any{"name": name, "context_window": window, "background_model": background}
}

func envPair(key, value string) map[string]any {
	return map[string]any{"key": key, "value": value}
}
