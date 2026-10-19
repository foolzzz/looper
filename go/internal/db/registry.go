package db

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"github.com/foolzzz/looper/go/internal/registry"
)

const listRuntimesSQL = `SELECT r.id, r.name, r.type, r.executable, r.version, r.capabilities, r.probed_at, ` +
	`r.probe_error, r.builtin, r.created_at, r.updated_at, ` +
	`(SELECT count(*) FROM agent_profiles a WHERE a.runtime_id = r.id) AS profile_count ` +
	`FROM agent_runtimes r ORDER BY r.builtin DESC, r.name`

// listProvidersSQL does not read llm_provider_secrets.api_key. api_key_set is
// whether a row exists.
const listProvidersSQL = `SELECT p.id, p.name, p.protocols, p.auth, p.oauth_runtime_type, p.base_url, ` +
	`p.models, p.env, p.max_concurrency, p.builtin, p.created_at, p.updated_at, ` +
	`s.provider_id IS NOT NULL AS api_key_set, ` +
	`(SELECT count(*) FROM agent_profiles a WHERE a.provider_id = p.id) AS profile_count ` +
	`FROM llm_providers p LEFT JOIN llm_provider_secrets s ON s.provider_id = p.id ` +
	`ORDER BY p.builtin DESC, p.name`

const usageLayersSQL = `SELECT l.profile_id, l.scope, l.role, l.project_id, p.name AS project_name ` +
	`FROM agent_setting_layers l LEFT JOIN projects p ON p.id = l.project_id ` +
	`WHERE l.profile_id IS NOT NULL AND ($1::text IS NULL OR l.profile_id = $1) ` +
	`ORDER BY array_position(ARRAY['system', 'role', 'project', 'role_project'], l.scope), p.name, ` +
	`array_position(ARRAY['manager', 'planner', 'worker', 'reviewer'], l.role)`

const usageTasksSQL = `SELECT profile_id, count(DISTINCT task_id) AS n FROM task_profile_overrides ` +
	`WHERE $1::text IS NULL OR profile_id = $1 GROUP BY profile_id`

const usageSchedulesSQL = `SELECT profile_id, count(DISTINCT schedule_id) AS n FROM schedule_profile_overrides ` +
	`WHERE $1::text IS NULL OR profile_id = $1 GROUP BY profile_id`

const usageAllowedSQL = `SELECT l.profile_id, p.name FROM project_allowed_profiles l ` +
	`JOIN projects p ON p.id = l.project_id WHERE $1::text IS NULL OR l.profile_id = $1 ORDER BY p.name`

// Registry is profile_registry.registry_view. claudeBin empty means "claude".
// environ nil means the process environment, which is what a real server
// probes with. A test passes its own slice so the probe never stats
// ~/.codex or ~/.grok. A login probe failure is a login state, not an error.
// A database error reading the registry rows is returned.
func (s *Store) Registry(ctx context.Context, claudeBin string, environ []string) (map[string]any, error) {
	if claudeBin == "" {
		claudeBin = "claude"
	}
	if environ == nil {
		environ = os.Environ()
	}
	runtimes, err := s.query(ctx, "runtimes", listRuntimesSQL)
	if err != nil {
		return nil, err
	}
	providers, err := s.query(ctx, "providers", listProvidersSQL)
	if err != nil {
		return nil, err
	}
	profiles, err := s.profileRows(ctx, "profiles", listProfilesSQL)
	if err != nil {
		return nil, err
	}
	usage, err := s.profileUsage(ctx)
	if err != nil {
		return nil, err
	}
	logins := s.loginViews(ctx, providers, environ)
	runtimeViews := make([]any, 0, len(runtimes))
	for _, row := range runtimes {
		runtimeViews = append(runtimeViews, runtimeView(row, claudeBin, environ))
	}
	providerViews := make([]any, 0, len(providers))
	for _, row := range providers {
		view, err := providerView(row, logins)
		if err != nil {
			return nil, fmt.Errorf("providers: %w", err)
		}
		providerViews = append(providerViews, view)
	}
	profileViews := make([]any, 0, len(profiles))
	for _, row := range profiles {
		profileViews = append(profileViews, profileView(row, usage[asString(row["id"])]))
	}
	return map[string]any{
		"runtime_types":    registry.TypeViews(),
		"runtimes":         runtimeViews,
		"providers":        providerViews,
		"profiles":         profileViews,
		"provider_presets": registry.PresetsView(),
		"prices":           pricesView(),
		"limits":           registry.Limits(),
	}, nil
}

func (s *Store) loginViews(ctx context.Context, providers []map[string]any, environ []string) map[string]map[string]string {
	want := map[string]bool{}
	for _, row := range providers {
		if asString(row["auth"]) != "oauth" {
			continue
		}
		kind := asString(row["oauth_runtime_type"])
		if kind == "codex" || kind == "grok" {
			want[kind] = true
		}
	}
	out := map[string]map[string]string{}
	for _, kind := range []string{"codex", "grok"} {
		if !want[kind] {
			continue
		}
		problem, err := s.recordedProblem(ctx, registry.LoginForkKey(kind))
		out[kind] = registry.LoginView(ctx, kind, environ, problem, err)
	}
	return out
}

func (s *Store) recordedProblem(ctx context.Context, key string) (*registry.Problem, error) {
	rows, err := s.query(ctx, "login problem", `SELECT value FROM system_settings WHERE key = $1`, key)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	raw, ok := rows[0]["value"].(string)
	if !ok {
		return &registry.Problem{Unreadable: true}, nil
	}
	var value any
	if err := json.Unmarshal([]byte(raw), &value); err != nil {
		return &registry.Problem{Unreadable: true}, nil
	}
	obj, ok := value.(map[string]any)
	if !ok {
		return &registry.Problem{Unreadable: true}, nil
	}
	problem := &registry.Problem{}
	if at, ok := obj["at"].(string); ok {
		problem.At = at
	}
	detail, exists := obj["detail"]
	switch text := detail.(type) {
	case nil:
		problem.Detail = "None"
	case string:
		problem.Detail = text
	default:
		if !exists {
			problem.Detail = "None"
			break
		}
		problem.Detail = fmt.Sprint(detail)
	}
	return problem, nil
}

func (s *Store) profileUsage(ctx context.Context) (map[string]map[string]any, error) {
	layers, err := s.query(ctx, "profile usage layers", usageLayersSQL, nil)
	if err != nil {
		return nil, err
	}
	tasks, err := s.query(ctx, "profile usage tasks", usageTasksSQL, nil)
	if err != nil {
		return nil, err
	}
	schedules, err := s.query(ctx, "profile usage schedules", usageSchedulesSQL, nil)
	if err != nil {
		return nil, err
	}
	allowed, err := s.query(ctx, "profile usage allowed", usageAllowedSQL, nil)
	if err != nil {
		return nil, err
	}
	uses := map[string]map[string]any{}
	use := func(id string) map[string]any {
		row, ok := uses[id]
		if !ok {
			row = map[string]any{
				"layers":         []any{},
				"task_count":     0,
				"schedule_count": 0,
				"allowed_in":     []any{},
			}
			uses[id] = row
		}
		return row
	}
	for _, row := range layers {
		item := use(asString(row["profile_id"]))
		item["layers"] = append(item["layers"].([]any), map[string]any{
			"scope":        row["scope"],
			"role":         row["role"],
			"project_id":   row["project_id"],
			"project_name": row["project_name"],
		})
	}
	for _, row := range tasks {
		use(asString(row["profile_id"]))["task_count"] = numberOrZero(row["n"])
	}
	for _, row := range schedules {
		use(asString(row["profile_id"]))["schedule_count"] = numberOrZero(row["n"])
	}
	for _, row := range allowed {
		item := use(asString(row["profile_id"]))
		item["allowed_in"] = append(item["allowed_in"].([]any), row["name"])
	}
	return uses, nil
}

func runtimeView(row map[string]any, claudeBin string, environ []string) map[string]any {
	runtimeType := asString(row["type"])
	adapter, known := registry.AdapterByType(runtimeType)
	label := runtimeType
	var defaultExe any
	protocols := []any{}
	if known {
		label = adapter.Label
		defaultExe = adapter.DefaultExecutable
		protocols = sortedProtocols(adapter.Protocols)
	}
	caps := textList(row["capabilities"])
	return map[string]any{
		"runtime_id":         row["id"],
		"name":               row["name"],
		"type":               row["type"],
		"type_label":         label,
		"executable":         row["executable"],
		"default_executable": defaultExe,
		"path":               registry.RuntimePath(runtimeType, asString(row["executable"]), claudeBin, environ),
		"protocols":          protocols,
		"version":            row["version"],
		"capabilities":       stringAny(caps),
		"roles":              servableRoles(caps),
		"probed_at":          row["probed_at"],
		"probe_error":        row["probe_error"],
		"builtin":            asBool(row["builtin"]),
		"profile_count":      numberOrZero(row["profile_count"]),
		"created_at":         row["created_at"],
		"updated_at":         row["updated_at"],
	}
}

func providerView(row map[string]any, logins map[string]map[string]string) (map[string]any, error) {
	oauthType := ""
	if row["oauth_runtime_type"] != nil {
		oauthType = asString(row["oauth_runtime_type"])
	}
	adapter, known := registry.AdapterByType(oauthType)
	oauth := asString(row["auth"]) == "oauth" && known
	models, err := registry.CatalogEntries(asAnySlice(row["models"]))
	if err != nil {
		return nil, err
	}
	suggested := []any{}
	var login any
	if oauth {
		for _, model := range adapter.LoginModels {
			suggested = append(suggested, model)
		}
		if found, ok := logins[adapter.Type]; ok {
			login = found
		}
	}
	var apiKey any
	if asBool(row["api_key_set"]) {
		apiKey = registry.SecretMask
	}
	return map[string]any{
		"provider_id":        row["id"],
		"name":               row["name"],
		"protocols":          stringAny(textList(row["protocols"])),
		"auth":               row["auth"],
		"oauth_runtime_type": row["oauth_runtime_type"],
		"base_url":           row["base_url"],
		"models":             models,
		"suggested_models":   suggested,
		"login":              login,
		"env":                asAnySlice(row["env"]),
		"max_concurrency":    row["max_concurrency"],
		"api_key":            apiKey,
		"builtin":            asBool(row["builtin"]),
		"profile_count":      numberOrZero(row["profile_count"]),
		"created_at":         row["created_at"],
		"updated_at":         row["updated_at"],
	}, nil
}

func profileView(row map[string]any, used map[string]any) map[string]any {
	if used == nil {
		used = notUsed()
	}
	spec := registry.RuntimeSpecFrom(
		asString(row["runtime_id"]), asString(row["runtime_name"]), asString(row["runtime_type"]),
		asString(row["runtime_executable"]), textList(row["runtime_capabilities"]),
	)
	provider := registry.ProviderSpecFrom(
		asString(row["provider_id"]), asString(row["provider_name"]), asString(row["provider_auth"]),
		textList(row["provider_protocols"]), asString(row["provider_oauth_runtime_type"]),
		asString(row["provider_base_url"]),
	)
	custom := ""
	if row["custom_prompt"] != nil {
		custom = asString(row["custom_prompt"])
	}
	return map[string]any{
		"profile_id":    row["id"],
		"name":          row["name"],
		"model":         row["model"],
		"effort":        row["effort"],
		"runtime_id":    row["runtime_id"],
		"runtime_name":  spec.Name,
		"runtime_type":  spec.Type,
		"provider_id":   row["provider_id"],
		"provider_name": provider.Name,
		"builtin":       asBool(row["builtin"]),
		"agent_teams":   asBool(row["agent_teams"]),
		"custom_prompt": custom,
		"roles":         servableRoles(spec.Capabilities),
		"used_by":       used,
		"cost":          costBasis(row),
	}
}

func notUsed() map[string]any {
	return map[string]any{
		"layers":         []any{},
		"task_count":     0,
		"schedule_count": 0,
		"allowed_in":     []any{},
	}
}

// costWarnings is cost_warnings. The registry GET shows cost_basis, not this
// list. Callers that register a profile use it.
func costWarnings(row map[string]any) []string {
	basis := costBasis(row)
	warnings := []string{}
	if items, ok := basis["unpriced"].([]any); ok {
		for _, item := range items {
			warnings = append(warnings, registry.PriceWarning(fmt.Sprint(item), asString(row["provider_name"])))
		}
	}
	adapter, ok := registry.AdapterByType(asString(row["runtime_type"]))
	if ok && basis["source"] == "runtime" && !adapter.RuntimeCostCapped {
		warnings = append(warnings, registry.RuntimeCostWarning(adapter.Label))
	}
	return warnings
}

func pricesView() map[string]any {
	models := make(map[string]any, len(builtinPrices))
	fields := []string{"input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok"}
	for name, price := range builtinPrices {
		entry := make(map[string]any, len(fields))
		for i, field := range fields {
			entry[field] = price[i]
		}
		models[name] = entry
	}
	return map[string]any{
		"as_of":   registry.PricesAsOf(),
		"sources": registry.PriceSources(),
		"models":  models,
	}
}

func sortedProtocols(values []string) []any {
	cp := append([]string(nil), values...)
	// Protocols in a type view are sorted. A stored provider protocol list is not.
	for i := 1; i < len(cp); i++ {
		for j := i; j > 0 && cp[j] < cp[j-1]; j-- {
			cp[j], cp[j-1] = cp[j-1], cp[j]
		}
	}
	return stringAny(cp)
}

func stringAny(values []string) []any {
	if values == nil {
		return []any{}
	}
	out := make([]any, len(values))
	for i, value := range values {
		out[i] = value
	}
	return out
}

func asAnySlice(v any) []any {
	items, ok := v.([]any)
	if !ok || items == nil {
		return []any{}
	}
	return items
}

func numberOrZero(v any) any {
	switch v.(type) {
	case int, int32, int64:
		return v
	default:
		if _, ok := asIntOK(v); ok {
			return asInt(v)
		}
		return 0
	}
}
