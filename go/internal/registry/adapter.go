// Package registry is the static half of the Agent Profile registry: runtime
// types, presets, limits, and login probes. It does not import the database.
// Rows and SQL live in package db.
package registry

// SecretMask is what the API shows in place of a stored provider API key.
const SecretMask = "***"

// Capability names, in the order servable roles are decided. A stored name
// outside this set is ignored, the way RuntimeSpec drops an unknown value.
var knownCapability = map[string]bool{
	"run": true, "steer": true, "resident_session": true, "resume": true,
	"structured_output": true, "claude_plugins": true, "mcp": true, "add_dirs": true,
	"compaction_events": true, "autonomous_turns": true, "read_tools": true, "cost_report": true,
}

// roleNeeds is ROLE_CAPABILITIES. The order is the order servable roles return.
var roleNeeds = []struct {
	role  string
	needs []string
}{
	{"manager", []string{"resident_session", "resume"}},
	{"planner", []string{"resident_session", "resume"}},
	{"worker", []string{"run"}},
	{"reviewer", []string{"run"}},
}

// Adapter is one runtime type in ADAPTERS order. Views sort protocols,
// capabilities, and auth kinds; login models stay in the adapter's order.
type Adapter struct {
	Type              string
	Label             string
	DefaultExecutable string
	Protocols         []string
	Capabilities      []string
	AuthKinds         []string
	LoginModels       []string
	EnvOwned          []string
	CanReplace        bool
	RuntimeCostCapped bool
	Cost              string // "claude", "runtime", or "provider"
}

// Adapters is runtimes.ADAPTERS: Claude Code, Codex, OMP, Grok.
var Adapters = []Adapter{
	{
		Type: "claude-code", Label: "Claude Code", DefaultExecutable: "claude",
		Protocols:    []string{"anthropic-messages"},
		Capabilities: []string{"run", "resident_session", "resume", "claude_plugins", "mcp", "add_dirs", "compaction_events", "autonomous_turns", "cost_report"},
		AuthKinds:    []string{"oauth", "api_key"},
		EnvOwned: []string{
			"ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
			"ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
			"ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
		},
		CanReplace: true, RuntimeCostCapped: true, Cost: "claude",
	},
	{
		Type: "codex", Label: "Codex", DefaultExecutable: "codex",
		Protocols:    []string{"openai-responses"},
		Capabilities: []string{"run", "resident_session", "resume", "structured_output", "mcp", "add_dirs"},
		AuthKinds:    []string{"api_key", "oauth"},
		LoginModels:  []string{"gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"},
		EnvOwned:     []string{"LOOPER_PROVIDER_API_KEY", "CODEX_HOME"},
		CanReplace:   true, RuntimeCostCapped: true, Cost: "provider",
	},
	{
		Type: "omp", Label: "OMP", DefaultExecutable: "omp",
		Protocols:    []string{"openai-responses", "openai-chat", "anthropic-messages"},
		Capabilities: []string{"run", "resident_session", "resume", "claude_plugins", "mcp", "add_dirs", "compaction_events", "read_tools"},
		AuthKinds:    []string{"api_key"},
		EnvOwned: []string{
			"LOOPER_PROVIDER_API_KEY", "PI_SHELL_PREFIX", "OMP_PROFILE", "PI_PROFILE",
			"PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_CONFIG_DIR", "PI_CONFIG_FILES",
		},
		CanReplace: true, RuntimeCostCapped: true, Cost: "provider",
	},
	{
		Type: "grok", Label: "Grok", DefaultExecutable: "grok",
		Protocols:    []string{"openai-responses"},
		Capabilities: []string{"run", "resident_session", "resume", "mcp", "compaction_events", "cost_report"},
		AuthKinds:    []string{"oauth"},
		LoginModels:  []string{"grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5"},
		EnvOwned: []string{
			"GROK_HOME", "GROK_CONFIG", "GROK_CONFIG_PATH", "GROK_AGENT", "GROK_SANDBOX",
			"HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CLAUDE_CONFIG_DIR",
			"XAI_API_KEY", "GROK_CODE_XAI_API_KEY", "GROK_AUTH",
			"GROK_DISABLE_AUTOUPDATER", "GROK_MEMORY",
			"GROK_CLAUDE_SKILLS_ENABLED", "GROK_CLAUDE_RULES_ENABLED", "GROK_CLAUDE_AGENTS_ENABLED", "GROK_CLAUDE_MCPS_ENABLED", "GROK_CLAUDE_HOOKS_ENABLED",
			"GROK_CURSOR_SKILLS_ENABLED", "GROK_CURSOR_RULES_ENABLED", "GROK_CURSOR_AGENTS_ENABLED", "GROK_CURSOR_MCPS_ENABLED", "GROK_CURSOR_HOOKS_ENABLED",
		},
		CanReplace: true, RuntimeCostCapped: false, Cost: "runtime",
	},
}

// AdapterByType returns the adapter for a runtime type. The second result is
// false for a type this version cannot run.
func AdapterByType(runtimeType string) (Adapter, bool) {
	for _, adapter := range Adapters {
		if adapter.Type == runtimeType {
			return adapter, true
		}
	}
	return Adapter{}, false
}

// ServableRoles is servable_roles: the roles whose needs are a subset of
// capabilities, in ROLE_CAPABILITIES order. The result is never nil.
func ServableRoles(capabilities []string) []string {
	have := map[string]bool{}
	for _, cap := range capabilities {
		if knownCapability[cap] {
			have[cap] = true
		}
	}
	out := []string{}
	for _, role := range roleNeeds {
		ok := true
		for _, need := range role.needs {
			if !have[need] {
				ok = false
				break
			}
		}
		if ok {
			out = append(out, role.role)
		}
	}
	return out
}

// KnownCapabilities keeps capability names this version knows, in the stored
// order. RuntimeSpec uses it.
func KnownCapabilities(capabilities []string) []string {
	out := []string{}
	for _, cap := range capabilities {
		if knownCapability[cap] {
			out = append(out, cap)
		}
	}
	return out
}

// RuntimeSpec is a runtime row as a profile check sees it.
type RuntimeSpec struct {
	ID           string
	Name         string
	Type         string
	Executable   string
	Capabilities []string
}

// RuntimeSpecFrom is runtime_spec. An unknown capability is dropped.
func RuntimeSpecFrom(id, name, runtimeType, executable string, capabilities []string) RuntimeSpec {
	return RuntimeSpec{
		ID: id, Name: name, Type: runtimeType, Executable: executable,
		Capabilities: KnownCapabilities(capabilities),
	}
}

// ProviderSpec is a provider row as a profile check sees it. The API key is
// not a field: the registry view never receives it.
type ProviderSpec struct {
	ID           string
	Name         string
	Auth         string
	Protocols    []string
	OAuthRuntime string
	BaseURL      string
}

// ProviderSpecFrom is provider_spec without the catalog. Cost and the view
// read the catalog from the stored JSON so integers stay integers.
func ProviderSpecFrom(id, name, auth string, protocols []string, oauthRuntime, baseURL string) ProviderSpec {
	if protocols == nil {
		protocols = []string{}
	}
	return ProviderSpec{
		ID: id, Name: name, Auth: auth, Protocols: protocols,
		OAuthRuntime: oauthRuntime, BaseURL: baseURL,
	}
}
