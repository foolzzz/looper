package db

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/foolzzz/looper/go/internal/pyjson"
	"github.com/foolzzz/looper/go/internal/registry"
)

type exportObject = pyjson.Object
type exportField = pyjson.Field

// jsonb stores object keys in byte-length then byte order. Python json.loads
// preserves that order, including inside arrays and nested objects.
func exportJSON(v any) any {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Slice(keys, func(i, j int) bool {
			if len(keys[i]) != len(keys[j]) {
				return len(keys[i]) < len(keys[j])
			}
			return keys[i] < keys[j]
		})
		out := exportObject{}
		for _, k := range keys {
			out = append(out, exportField{Key: k, Value: exportJSON(t[k])})
		}
		return out
	case []any:
		out := make([]any, 0, len(t))
		for _, v := range t {
			out = append(out, exportJSON(v))
		}
		return out
	default:
		return v
	}
}
func exportFields(row map[string]any, names ...string) exportObject {
	out := exportObject{}
	for _, name := range names {
		out = append(out, exportField{Key: name, Value: exportJSON(row[name])})
	}
	return out
}
func exportSet(obj exportObject, key string, value any) exportObject {
	for i := range obj {
		if obj[i].Key == key {
			obj[i].Value = value
			return obj
		}
	}
	return append(obj, exportField{Key: key, Value: value})
}
func exportDump(v any) ([]byte, error) {
	raw, err := pyjson.Marshal(v)
	if err != nil {
		return nil, err
	}
	// Python dumps permits NaN and Infinity. json.Compact would reject those
	// tokens, so remove only the encoder's separator spaces outside strings.
	out := raw[:0]
	inString, escaped := false, false
	for _, c := range raw {
		if inString {
			out = append(out, c)
			if escaped {
				escaped = false
			} else if c == '\\' {
				escaped = true
			} else if c == '"' {
				inString = false
			}
		} else {
			if c == '"' {
				inString = true
			}
			if c != ' ' {
				out = append(out, c)
			}
		}
	}
	return out, nil
}

var exportDrivePath = regexp.MustCompile(`^[A-Za-z]:[\\/]`)

func portableExportSource(value any) any {
	source := asString(value)
	if source == "" || strings.HasPrefix(source, "/") || strings.HasPrefix(source, "file:") || exportDrivePath.MatchString(source) {
		return nil
	}
	return source
}
func exportRuntime(row map[string]any) exportObject {
	executable := any("")
	if asString(row["executable"]) != "" {
		executable = portableExportSource(row["executable"])
	}
	return exportObject{{Key: "name", Value: row["name"]}, {Key: "type", Value: row["type"]}, {Key: "executable", Value: executable}}
}
func exportSecret(v any, include bool) any {
	if include {
		return exportJSON(v)
	}
	return exportObject{{Key: "secret", Value: "omitted"}}
}
func exportProvider(row map[string]any, include bool) (exportObject, error) {
	out := exportFields(row, "name", "protocols", "auth", "oauth_runtime_type", "base_url", "models", "env", "max_concurrency")
	models := []any{}
	for _, model := range asAnySlice(row["models"]) {
		stored, ok := model.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("export provider: catalog entry is not an object")
		}
		entry := exportObject{}
		for _, key := range registry.ModelFields {
			entry = append(entry, exportField{Key: key})
		}
		for _, field := range exportJSON(stored).(exportObject) {
			entry = exportSet(entry, field.Key, field.Value)
		}
		models = append(models, entry)
	}
	out = exportSet(out, "models", models)
	var key any
	if row["api_key"] != nil {
		key = exportSecret(row["api_key"], include)
	}
	return append(out, exportField{Key: "api_key", Value: key}), nil
}
func exportProfile(row map[string]any) exportObject {
	out := exportFields(row, "name", "runtime", "provider", "model", "effort", "agent_teams", "system_prompt_mode")
	prompt := row["custom_prompt"]
	if prompt == nil {
		prompt = ""
	}
	return append(out, exportField{Key: "custom_prompt", Value: prompt})
}
func exportLayer(row map[string]any) exportObject {
	out := exportFields(row, "scope")
	if asString(row["role"]) != "" {
		out = append(out, exportField{Key: "role", Value: row["role"]})
	}
	for _, key := range []string{"harness_prompt", "profile", "env", "run_budget_usd", "max_concurrency", "boundaries", "system_prompt_mode"} {
		value := exportJSON(row[key])
		if key == "env" && !pythonTruthy(row[key]) {
			value = []any{}
		}
		if (key == "max_concurrency" || key == "boundaries" || key == "system_prompt_mode") && value == nil {
			continue
		}
		out = append(out, exportField{Key: key, Value: value})
	}
	return out
}
func exportRole(row map[string]any) exportObject {
	var prompt any
	if asString(row["system_prompt"]) != "" && row["system_prompt"] != row["default_prompt"] {
		prompt = row["system_prompt"]
	}
	return exportObject{{Key: "role", Value: row["role"]}, {Key: "prompt", Value: prompt}, {Key: "timeout_seconds", Value: row["timeout_seconds"]}, {Key: "enabled", Value: row["enabled"]}}
}

var exportPublicKeys = map[string][]string{
	"telegram": {"allowed_user_ids", "bot_token_env", "description"},
	"lark":     {"allowed_open_ids", "allowlist_app_id", "profile", "description"},
	"lark_bot": {"allowed_open_ids", "allowlist_app_id", "app_id", "domain", "description"},
}

func exportChannel(row map[string]any, include bool) (exportObject, int, error) {
	config := map[string]any{}
	if pythonTruthy(row["config"]) {
		raw, ok := row["config"].(map[string]any)
		if !ok {
			return nil, 0, fmt.Errorf("export channel: config is not an object")
		}
		for k, v := range raw {
			config[k] = v
		}
	}
	kind := asString(row["channel_type"])
	if spec, ok := senderFormat[kind]; ok {
		if raw, exists := config[spec.field]; exists {
			if ids, err := senderIDs(kind, raw); err == nil {
				if ids == nil {
					ids = []string{}
				}
				config[spec.field] = ids
			}
		}
	}
	count := 0
	values := exportObject{}
	for _, field := range exportJSON(config).(exportObject) {
		public := false
		for _, key := range exportPublicKeys[kind] {
			if field.Key == key {
				public = true
				break
			}
		}
		if !public {
			count++
			field.Value = exportSecret(field.Value, include)
		}
		values = append(values, field)
	}
	return append(exportFields(row, "channel_type", "channel_id", "display_name", "enabled"), exportField{Key: "config", Value: values}), count, nil
}
func exportSecretValues(v any, include bool) (exportObject, int) {
	raw, _ := v.(map[string]any)
	keys := make([]string, 0, len(raw))
	for key := range raw {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := exportObject{}
	for _, key := range keys {
		out = append(out, exportField{Key: key, Value: exportSecret(raw[key], include)})
	}
	return out, len(keys)
}
func exportItem(row map[string]any, files []map[string]any, include bool) (exportObject, int) {
	out := exportFields(row, "kind", "name", "origin", "source", "version", "description", "modified")
	out = exportSet(out, "source", portableExportSource(row["source"]))
	if row["kind"] == "mcp" {
		env, n := exportSecretValues(row["env"], include)
		headers, m := exportSecretValues(row["headers"], include)
		args := row["args"]
		if args == nil {
			args = []any{}
		}
		server := exportObject{{Key: "transport", Value: row["transport"]}, {Key: "command", Value: row["command"]}, {Key: "args", Value: args}, {Key: "env", Value: env}, {Key: "url", Value: row["url"]}, {Key: "headers", Value: headers}}
		return append(out, exportField{Key: "server", Value: server}), n + m
	}
	entries := []any{}
	for _, file := range files {
		entries = append(entries, exportFields(file, "path", "executable", "sha256", "content"))
	}
	return append(out, exportField{Key: "sha256", Value: row["sha256"]}, exportField{Key: "files", Value: entries}), 0
}
func exportItemRef(row map[string]any) exportObject {
	out := exportFields(row, "kind", "name")
	if asString(row["owner_project_name"]) != "" {
		out = append(out, exportField{Key: "project", Value: row["owner_project_name"]})
	}
	return out
}
func exportInstall(row map[string]any) exportObject {
	return exportObject{{Key: "item", Value: exportItemRef(row)}, {Key: "roles", Value: row["roles"]}, {Key: "enabled", Value: row["enabled"]}}
}
func exportTemplate(row map[string]any, items map[string]map[string]any) exportObject {
	out := exportObject{{Key: "key", Value: row["template_key"]}}
	out = append(out, exportFields(row, "kind", "description", "role_prompt")...)
	for _, key := range []string{"harness", "profile"} {
		if pythonTruthy(row[key]) {
			out = append(out, exportField{Key: key, Value: row[key]})
		}
	}
	refs := []any{}
	for _, id := range asAnySlice(row["items"]) {
		if item, ok := items[asString(id)]; ok {
			refs = append(refs, exportItemRef(item))
		}
	}
	if len(refs) > 0 {
		out = append(out, exportField{Key: "items", Value: refs})
	}
	return out
}
func exportDocument(row map[string]any) exportObject {
	out := exportFields(row, "title", "summary", "source_kind", "filename", "size_bytes", "sha256")
	if row["source_kind"] == "path" {
		return append(out, exportField{Key: "path", Value: row["path"]}, exportField{Key: "repoint", Value: true})
	}
	return append(out, exportField{Key: "content", Value: row["content"]})
}

var exportScheduleFields = []string{"name", "cron", "timezone", "requirement", "overlap", "max_parallel_runs", "catch_up", "run_timeout_seconds", "run_budget_usd", "gate_command", "gate_timeout_seconds", "daily_budget_usd", "notify"}

func exportSchedule(row map[string]any, profiles map[string]string, overrides, limits map[string]any) exportObject {
	out := exportFields(row, exportScheduleFields...)
	var target any
	if asString(row["notify_channel"]) != "" {
		target = exportObject{{Key: "channel", Value: row["notify_channel"]}, {Key: "chat", Value: row["notify_chat"]}}
	}
	out = append(out, exportField{Key: "notify_target", Value: target})
	named := exportObject{}
	keys := make([]string, 0, len(overrides))
	for key := range overrides {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		id := asString(overrides[key])
		name, ok := profiles[id]
		if !ok {
			name = id
		}
		named = append(named, exportField{Key: key, Value: name})
	}
	keys = keys[:0]
	for key := range limits {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	taskLimits := exportObject{}
	for _, key := range keys {
		taskLimits = append(taskLimits, exportField{Key: key, Value: limits[key]})
	}
	out = append(out, exportField{Key: "profiles", Value: named}, exportField{Key: "task_limits", Value: taskLimits})
	if asString(row["work_dir"]) != "" {
		out = append(out, exportField{Key: "repoint", Value: true})
	}
	return out
}
