package db

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// Database-free projection coverage; TestCatalogGoldenParity separately checks
// PostgreSQL queries and HTTP routes. These joins are fixture preparation only.
func catalogGoldenRows(t *testing.T) (map[string][]map[string]any, []map[string]any, []map[string]any) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", "catalog_fixture_input.json"))
	if err != nil {
		t.Fatal(err)
	}
	var tables map[string][]map[string]any
	if err := json.Unmarshal(data, &tables); err != nil {
		t.Fatal(err)
	}
	projects, servers := map[string]map[string]any{}, map[string]map[string]any{}
	for _, row := range tables["projects"] {
		projects[asString(row["id"])] = row
	}
	for _, row := range tables["library_mcp_servers"] {
		servers[asString(row["item_id"])] = row
	}
	for _, rows := range tables {
		for _, row := range rows {
			for _, field := range []string{"created_at", "updated_at", "imported_at"} {
				if text, ok := row[field].(string); ok {
					row[field] = strings.TrimSuffix(text, "Z") + "+00:00"
				}
			}
		}
	}
	items, installs := tables["library_items"], tables["library_installs"]
	for _, row := range items {
		row["owner_project_name"] = projects[asString(row["owner_project_id"])]["name"]
		server := servers[asString(row["id"])]
		for output, field := range map[string]string{"mcp_transport": "transport", "mcp_command": "command", "mcp_args": "args", "mcp_url": "url"} {
			row[output] = server[field]
		}
		for output, field := range map[string]string{"mcp_env_keys": "env", "mcp_header_keys": "headers"} {
			keys := []string{}
			if values, ok := server[field].(map[string]any); ok {
				for key := range values {
					keys = append(keys, key)
				}
			}
			sort.Strings(keys)
			row[output] = keys
		}
	}
	for _, row := range installs {
		row["project_name"] = projects[asString(row["project_id"])]["name"]
	}
	sort.SliceStable(items, func(i, j int) bool {
		a, b := items[i], items[j]
		for _, field := range []string{"kind", "name", "owner_project_id"} {
			if a[field] != b[field] {
				return asString(a[field]) < asString(b[field])
			}
		}
		return false
	})
	sort.SliceStable(installs, func(i, j int) bool {
		a, b := installs[i], installs[j]
		for _, field := range []string{"scope", "project_name", "id"} {
			if a[field] != b[field] {
				return asString(a[field]) < asString(b[field])
			}
		}
		return false
	})
	return tables, items, installs
}

func catalogGolden(t *testing.T, name string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", name+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatal(err)
	}
	return result
}

func assertCatalogProjection(t *testing.T, got, want any) {
	t.Helper()
	body, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	for _, sentinel := range []string{"MCP_ENV_SECRET_SENTINEL", "MCP_HEADER_SECRET_SENTINEL", "MCP_AUTH_SECRET_SENTINEL",
		"PLUGIN_FILE_CONTENT_SENTINEL", "UPLOADED_PLUGIN_CONTENT_SENTINEL", "REFERENCE_FILE_CONTENT_SENTINEL"} {
		if bytes.Contains(body, []byte(sentinel)) {
			t.Fatalf("projection leaked %q", sentinel)
		}
	}
	var normalized any
	if err := json.Unmarshal(body, &normalized); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(normalized, want) {
		expected, _ := json.Marshal(want)
		t.Fatalf("Python catalog golden differs\n got %s\nwant %s", body, expected)
	}
}

func TestCatalogViewsPythonGoldens(t *testing.T) {
	tables, items, installs := catalogGoldenRows(t)
	t.Run("catalog", func(t *testing.T) {
		assertCatalogProjection(t, catalogView(items, installs, tables["system_settings"]), catalogGolden(t, "catalog"))
	})
	for _, item := range items {
		id := asString(item["id"])
		t.Run(id, func(t *testing.T) {
			selected := []map[string]any{}
			for _, row := range installs {
				if row["item_id"] == id {
					selected = append(selected, row)
				}
			}
			expected := catalogGolden(t, "catalog-"+id)
			assertCatalogProjection(t, catalogItemView(item, selected), expected["item"])
			if item["kind"] == "skill" {
				var text, body any
				for _, file := range tables["library_files"] {
					if file["item_id"] == id && file["path"] == "SKILL.md" {
						text = file["content"]
						body = catalogSkillBody(asString(text))
					}
				}
				assertCatalogProjection(t, text, expected["skill_md"])
				assertCatalogProjection(t, body, expected["content"])
			}
		})
	}
	for _, suffix := range []string{"01", "02", "03"} {
		t.Run("installs"+suffix, func(t *testing.T) {
			projectID := "proj-catalog" + suffix
			selected := []map[string]any{}
			for _, row := range installs {
				if row["scope"] == "global" || row["project_id"] == projectID {
					selected = append(selected, row)
				}
			}
			assertCatalogProjection(t, projectInstallsView(projectID, items, selected), catalogGolden(t, "catalog-installs"+suffix))
		})
	}
	for _, phase := range []struct {
		name     string
		settings []map[string]any
	}{
		{"catalog-invalid-reports", []map[string]any{{"key": "library.last_import", "value": "{bad"}, {"key": "library.last_mcp_import", "value": "["}}},
		{"catalog-scalar-reports", []map[string]any{{"key": "library.last_import", "value": "7"}, {"key": "library.last_mcp_import", "value": "[true, null]"}}},
		{"catalog-no-reports", nil},
	} {
		t.Run(phase.name, func(t *testing.T) {
			assertCatalogProjection(t, catalogView(items, installs, phase.settings), catalogGolden(t, phase.name))
		})
	}
	t.Run("empty", func(t *testing.T) {
		assertCatalogProjection(t, catalogView(nil, nil, nil), catalogGolden(t, "catalog-empty"))
	})
}
