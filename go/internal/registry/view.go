package registry

import "sort"

const (
	nameMaxChars = 240
	modelsMax    = 100
	pricesAsOf   = "2026-10-14"
)

// ModelFields is MODEL_FIELDS. catalog_entries fills a missing one with null.
var ModelFields = []string{
	"name", "context_window",
	"input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok",
	"efforts", "background_model",
}

// PriceFields is PRICE_FIELDS.
var PriceFields = []string{
	"input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok",
}

// PricesAsOf is PRICES_AS_OF.
func PricesAsOf() string { return pricesAsOf }

// PriceSources is PRICE_SOURCES, in that order.
func PriceSources() []any {
	return []any{
		"https://platform.claude.com/docs/en/about-claude/pricing",
		"https://developers.openai.com/api/docs/pricing",
		"https://docs.z.ai/guides/overview/pricing",
		"https://api-docs.deepseek.com/quick_start/pricing",
		"https://platform.kimi.ai/docs/pricing/chat.md",
	}
}

// TypeViews is registry_view's runtime_types: one type_view per adapter.
func TypeViews() []any {
	out := make([]any, 0, len(Adapters))
	for _, adapter := range Adapters {
		out = append(out, typeView(adapter))
	}
	return out
}

func typeView(adapter Adapter) map[string]any {
	var refusal any
	if !adapter.CanReplace {
		refusal = "the " + adapter.Label + " runtime cannot replace its default system prompt"
	}
	return map[string]any{
		"type":                          adapter.Type,
		"label":                         adapter.Label,
		"default_executable":            adapter.DefaultExecutable,
		"runtime_id":                    "rt-" + adapter.Type,
		"protocols":                     sortedCopy(adapter.Protocols),
		"capabilities":                  sortedCopy(adapter.Capabilities),
		"roles":                         anyStrings(ServableRoles(adapter.Capabilities)),
		"can_replace_system_prompt":     adapter.CanReplace,
		"replace_system_prompt_refusal": refusal,
		"auth_kinds":                    sortedCopy(adapter.AuthKinds),
	}
}

// Limits is _limits.
func Limits() map[string]any {
	owned := map[string]struct{}{}
	for _, adapter := range Adapters {
		for _, key := range adapter.EnvOwned {
			owned[key] = struct{}{}
		}
	}
	keys := make([]string, 0, len(owned))
	for key := range owned {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	roles := map[string]any{}
	for _, role := range roleNeeds {
		needs := append([]string(nil), role.needs...)
		sort.Strings(needs)
		roles[role.role] = anyStrings(needs)
	}
	return map[string]any{
		"name_max_chars":     nameMaxChars,
		"models_max":         modelsMax,
		"price_fields":       anyStrings(PriceFields),
		"model_fields":       anyStrings(ModelFields),
		"provider_env_owned": anyStrings(keys),
		"protocols":          []any{"anthropic-messages", "openai-responses", "openai-chat"},
		"auth_kinds":         []any{"oauth", "api_key"},
		"effort_levels":      []any{"low", "medium", "high", "xhigh", "max"},
		"role_capabilities":  roles,
		"secret_mask":        SecretMask,
	}
}

// CatalogEntries is catalog_entries. Each stored object keeps its own types.
// A value that is not an object is an error, which the registry request turns
// into 500; Python raises TypeError on the same row.
func CatalogEntries(models []any) ([]any, error) {
	if models == nil {
		return []any{}, nil
	}
	out := make([]any, 0, len(models))
	for _, item := range models {
		stored, ok := item.(map[string]any)
		if !ok {
			return nil, errCatalog(item)
		}
		entry := make(map[string]any, len(ModelFields)+len(stored))
		for _, field := range ModelFields {
			entry[field] = nil
		}
		for key, value := range stored {
			entry[key] = value
		}
		out = append(out, entry)
	}
	return out, nil
}

type catalogError struct{ got any }

func errCatalog(got any) error { return catalogError{got} }

func (e catalogError) Error() string { return "catalog entry is not an object" }

// PriceWarning is one cost_warnings line for an unpriced model.
func PriceWarning(model, provider string) string {
	return "model " + pyRepr(model) + " has no full price with provider " + pyRepr(provider) +
		" (input, output, cache read and cache write): its runs record tokens only and spend caps do not apply to them; add its prices to the provider's model catalog"
}

// RuntimeCostWarning is the cost_warnings line for a runtime that reports cost
// only when the run ends. Grok is the one of the four.
func RuntimeCostWarning(label string) string {
	return label + " reports a run's cost only when the run ends and has no spend cap of its own: per-run spend caps do not stop its runs; task spend caps count what they cost"
}

func sortedCopy(values []string) []any {
	cp := append([]string(nil), values...)
	sort.Strings(cp)
	return anyStrings(cp)
}

func anyStrings(values []string) []any {
	out := make([]any, 0, len(values))
	for _, value := range values {
		out = append(out, value)
	}
	return out
}
