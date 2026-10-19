package db

import (
	"strings"
	"testing"
)

func catalogProfile(id, model string, effort any) map[string]any {
	return map[string]any{
		"id": id, "name": "claude-code/" + model, "model": model, "effort": effort,
		"runtime_id": "rt-claude-code", "runtime_name": "claude-code", "runtime_type": "claude-code",
		"provider_id": "prov-claude-subscription", "provider_name": "claude-subscription", "provider_auth": "oauth",
	}
}

func envEntries(kv ...any) []any {
	out := make([]any, 0, len(kv)/2)
	for i := 0; i < len(kv); i += 2 {
		out = append(out, map[string]any{"key": kv[i], "value": kv[i+1]})
	}
	return out
}

func settingRow(scope, role, project string, fields map[string]any) map[string]any {
	row := map[string]any{"scope": scope, "role": nil, "project_id": nil}
	if role != "" {
		row["role"] = role
	}
	if project != "" {
		row["project_id"] = project
	}
	for key, value := range fields {
		row[key] = value
	}
	return row
}

func seedSettingRows() []map[string]any {
	return []map[string]any{
		settingRow("system", "", "", map[string]any{
			"profile":        catalogProfile("agent-layer-system", "layer-system", nil),
			"harness_prompt": "System harness.",
			"env":            envEntries("LAYER_ORIGIN", "system", "SHARED", "from-system", "DROP_ME", "system-drop"),
			"run_budget_usd": 1.25,
			"boundaries":     "Custom boundary line.",
		}),
		settingRow("role", "planner", "", map[string]any{
			"profile":        catalogProfile("agent-layer-planner", "layer-planner", "medium"),
			"harness_prompt": " \n\t",
		}),
		settingRow("role", "worker", "", map[string]any{
			"profile":            catalogProfile("agent-layer-worker", "layer-worker", "high"),
			"harness_prompt":     "Worker role harness.",
			"env":                envEntries("LAYER_ORIGIN", "worker-role", "WORKER_ONLY", "w"),
			"run_budget_usd":     2.5,
			"max_concurrency":    4,
			"system_prompt_mode": "replace",
		}),
		settingRow("role", "reviewer", "", map[string]any{
			"profile":            catalogProfile("agent-layer-reviewer", "layer-reviewer", "xhigh"),
			"env":                envEntries("DROP_ME", nil, "REVIEWER_ONLY", "r"),
			"max_concurrency":    5,
			"system_prompt_mode": "replace",
		}),
		settingRow("role", "manager", "", map[string]any{
			"harness_prompt":     "",
			"system_prompt_mode": "append",
		}),
		settingRow("project", "", "proj-alpha0001", map[string]any{
			"profile":        catalogProfile("agent-layer-project", "layer-project", "max"),
			"harness_prompt": "Alpha project harness.",
			"env":            envEntries("LAYER_ORIGIN", "project", "PROJECT_ONLY", "p"),
			"run_budget_usd": 3.5,
		}),
		settingRow("role_project", "planner", "proj-alpha0001", map[string]any{
			"profile":        catalogProfile("agent-layer-rp-plan", "layer-rp-plan", "low"),
			"harness_prompt": " \n",
		}),
		settingRow("role_project", "worker", "proj-alpha0001", map[string]any{
			"profile":        catalogProfile("agent-layer-rp-work", "layer-rp-work", "medium"),
			"harness_prompt": "Alpha worker harness.",
			"env":            envEntries("LAYER_ORIGIN", "role-project", "DROP_ME", nil),
			"run_budget_usd": 4.75,
		}),
		settingRow("role_project", "reviewer", "proj-alpha0001", map[string]any{
			"profile": catalogProfile("agent-layer-rp-rev", "layer-rp-rev", "high"),
			"env":     envEntries("REVIEWER_PROJECT", ""),
		}),
		settingRow("project", "", "proj-blank0001", map[string]any{}),
		settingRow("role_project", "worker", "proj-blank0001", map[string]any{
			"harness_prompt": "",
		}),
		settingRow("role_project", "reviewer", "proj-blank0001", map[string]any{
			"env": envEntries("SHARED", nil),
		}),
	}
}

func nested(t *testing.T, root map[string]any, path ...string) map[string]any {
	t.Helper()
	cur := root
	for _, key := range path {
		next, ok := cur[key].(map[string]any)
		if !ok {
			t.Fatalf("path %v missing %s in %#v", path, key, cur[key])
		}
		cur = next
	}
	return cur
}

func assertNoKey(t *testing.T, m map[string]any, key string) {
	t.Helper()
	if _, ok := m[key]; ok {
		t.Fatalf("unexpected %s", key)
	}
}

type envSpec struct {
	key    string
	value  string
	source string
}

func assertEffectiveEnv(t *testing.T, view map[string]any, want []envSpec) {
	t.Helper()
	raw, ok := view["env"].([]any)
	if !ok || raw == nil {
		t.Fatalf("env %#v", view["env"])
	}
	if len(raw) != len(want) {
		t.Fatalf("env %#v", raw)
	}
	for i, item := range want {
		got, ok := raw[i].(map[string]any)
		if !ok || got["key"] != item.key || got["value"] != item.value || got["source"] != item.source {
			t.Fatalf("env[%d] %#v", i, raw[i])
		}
	}
}

func TestEmptyAgentSettings(t *testing.T) {
	view := settingsView(map[layerKey]layerValues{})
	limits := view["limits"].(map[string]any)
	if limits["harness_max_chars"] != harnessMaxChars || limits["env_max_entries"] != envMaxEntries ||
		limits["builtin_model"] != "opus" || limits["budget_max_usd"] != budgetMaxUSD ||
		limits["concurrency_max"] != concurrencyMax || limits["builtin_harness_prompt"] != defaultHarnessPrompt ||
		limits["builtin_boundaries"] != harnessBoundaries {
		t.Fatalf("limits %#v", limits)
	}
	profile := limits["builtin_profile"].(map[string]any)
	if profile["profile_id"] != "agent-claude-code-opus" || profile["name"] != "claude-code/opus" {
		t.Fatalf("builtin profile %#v", profile)
	}
	system := nested(t, view, "system", "effective")
	harness := system["harness_prompt"].(map[string]any)
	if harness["value"] != defaultHarnessPrompt || harness["source"] != "builtin" {
		t.Fatalf("harness %#v", harness)
	}
	assertNoKey(t, system, "max_concurrency")
	assertNoKey(t, system, "boundaries")
	if system["system_prompt_mode"] != "append" {
		t.Fatalf("system mode %#v", system["system_prompt_mode"])
	}
	effort := system["effort"].(map[string]any)
	if effort["value"] != nil || effort["source"] != "builtin" {
		t.Fatalf("effort %#v", effort)
	}
	budget := system["run_budget_usd"].(map[string]any)
	if budget["value"] != nil || budget["source"] != "builtin" {
		t.Fatalf("budget %#v", budget)
	}
	gotProfile := system["profile"].(map[string]any)
	if gotProfile["source"] != "builtin" || gotProfile["value"].(map[string]any)["profile_id"] != "agent-claude-code-opus" {
		t.Fatalf("profile %#v", gotProfile)
	}
	env, ok := system["env"].([]any)
	if !ok || env == nil || len(env) != 0 {
		t.Fatalf("env %#v", system["env"])
	}
	bounds := nested(t, view, "system", "boundaries")
	if bounds["value"] != harnessBoundaries || bounds["source"] != "builtin" {
		t.Fatalf("boundaries %#v", bounds)
	}
	values := nested(t, view, "system", "values")
	for _, key := range []string{"harness_prompt", "profile_id", "run_budget_usd", "max_concurrency", "boundaries"} {
		if values[key] != nil {
			t.Fatalf("values %s %#v", key, values[key])
		}
	}
	assertNoKey(t, values, "system_prompt_mode")
	if values["env"] == nil {
		t.Fatal("empty env encoded as null")
	}
	worker := nested(t, view, "roles", "worker", "effective")
	reviewer := nested(t, view, "roles", "reviewer", "effective")
	if worker["max_concurrency"].(map[string]any)["value"] != builtinWorkerLimit ||
		worker["max_concurrency"].(map[string]any)["source"] != "builtin" ||
		worker["system_prompt_mode"] != "append" {
		t.Fatalf("worker %#v", worker["max_concurrency"])
	}
	if reviewer["max_concurrency"].(map[string]any)["value"] != builtinReviewerLimit ||
		reviewer["system_prompt_mode"] != "append" {
		t.Fatalf("reviewer %#v", reviewer["max_concurrency"])
	}
	if nested(t, view, "roles", "manager", "effective")["system_prompt_mode"] != "replace" ||
		nested(t, view, "roles", "planner", "effective")["system_prompt_mode"] != "replace" {
		t.Fatal("builtin prompt mode")
	}
	assertNoKey(t, nested(t, view, "roles", "manager", "effective"), "max_concurrency")
	assertNoKey(t, nested(t, view, "roles", "planner", "effective"), "max_concurrency")
}

func TestSeededAgentSettings(t *testing.T) {
	rows := seedSettingRows()
	global := settingsView(indexLayers(rows, ""))
	alpha := projectSettingsView("proj-alpha0001", indexLayers(rows, "proj-alpha0001"))
	beta := projectSettingsView("proj-beta00002", indexLayers(rows, "proj-beta00002"))
	blank := projectSettingsView("proj-blank0001", indexLayers(rows, "proj-blank0001"))

	if global["limits"].(map[string]any)["builtin_harness_prompt"] != defaultHarnessPrompt {
		t.Fatal("limits harness")
	}
	sysValues := nested(t, global, "system", "values")
	if sysValues["harness_prompt"] != "System harness." || sysValues["boundaries"] != "Custom boundary line." ||
		sysValues["profile_id"] != "agent-layer-system" || sysValues["run_budget_usd"] != 1.25 {
		t.Fatalf("system values %#v", sysValues)
	}
	assertNoKey(t, sysValues, "system_prompt_mode")
	bounds := nested(t, global, "system", "boundaries")
	if bounds["value"] != "Custom boundary line." || bounds["source"] != "system" {
		t.Fatalf("boundaries %#v", bounds)
	}
	sysEff := nested(t, global, "system", "effective")
	if sysEff["system_prompt_mode"] != "append" || sysEff["harness_prompt"].(map[string]any)["source"] != "system" {
		t.Fatalf("system effective %#v", sysEff["harness_prompt"])
	}
	if sysEff["effort"].(map[string]any)["value"] != nil || sysEff["effort"].(map[string]any)["source"] != "builtin" {
		t.Fatalf("system effort %#v", sysEff["effort"])
	}
	assertNoKey(t, sysEff, "max_concurrency")
	assertNoKey(t, sysEff, "boundaries")

	manager := nested(t, global, "roles", "manager", "effective")
	if manager["system_prompt_mode"] != "append" || manager["profile"].(map[string]any)["source"] != "system" {
		t.Fatalf("manager %#v", manager["profile"])
	}
	assertNoKey(t, manager, "max_concurrency")
	managerValues := nested(t, global, "roles", "manager", "values")
	if managerValues["harness_prompt"] != nil || managerValues["system_prompt_mode"] != "append" {
		t.Fatalf("manager values %#v", managerValues)
	}
	plannerValues := nested(t, global, "roles", "planner", "values")
	assertNoKey(t, plannerValues, "system_prompt_mode")
	if plannerValues["harness_prompt"] != nil {
		t.Fatalf("planner harness %#v", plannerValues["harness_prompt"])
	}
	planner := nested(t, global, "roles", "planner", "effective")
	if planner["harness_prompt"].(map[string]any)["value"] != "System harness." ||
		planner["harness_prompt"].(map[string]any)["source"] != "system" ||
		planner["system_prompt_mode"] != "replace" {
		t.Fatalf("planner %#v", planner["harness_prompt"])
	}

	worker := nested(t, global, "roles", "worker", "effective")
	assertEffectiveEnv(t, worker, []envSpec{
		{"LAYER_ORIGIN", "worker-role", "role"},
		{"SHARED", "from-system", "system"},
		{"DROP_ME", "system-drop", "system"},
		{"WORKER_ONLY", "w", "role"},
	})
	if worker["harness_prompt"].(map[string]any)["source"] != "role" ||
		worker["run_budget_usd"].(map[string]any)["value"] != 2.5 ||
		worker["run_budget_usd"].(map[string]any)["source"] != "role" ||
		worker["max_concurrency"].(map[string]any)["value"] != 4 ||
		worker["max_concurrency"].(map[string]any)["source"] != "role" ||
		worker["system_prompt_mode"] != "replace" ||
		worker["effort"].(map[string]any)["value"] != "high" {
		t.Fatalf("worker budget %#v concurrency %#v", worker["run_budget_usd"], worker["max_concurrency"])
	}
	reviewer := nested(t, global, "roles", "reviewer", "effective")
	assertEffectiveEnv(t, reviewer, []envSpec{
		{"LAYER_ORIGIN", "system", "system"},
		{"SHARED", "from-system", "system"},
		{"REVIEWER_ONLY", "r", "role"},
	})
	revHarness := reviewer["harness_prompt"].(map[string]any)
	if revHarness["value"] != "Worker role harness." || revHarness["source"] != "role" || revHarness["from_role"] != "worker" {
		t.Fatalf("reviewer harness %#v", revHarness)
	}
	if reviewer["run_budget_usd"].(map[string]any)["value"] != 1.25 ||
		reviewer["max_concurrency"].(map[string]any)["value"] != 5 {
		t.Fatalf("reviewer budget %#v", reviewer["run_budget_usd"])
	}
	revValues := nested(t, global, "roles", "reviewer", "values")["env"].([]any)
	if len(revValues) != 2 || revValues[0].(map[string]any)["value"] != nil {
		t.Fatalf("reviewer values env %#v", revValues)
	}

	if _, ok := alpha["roles"].(map[string]any)["manager"]; ok || len(alpha["roles"].(map[string]any)) != 3 {
		t.Fatalf("alpha roles %#v", alpha["roles"])
	}
	alphaWorker := nested(t, alpha, "roles", "worker", "effective")
	assertNoKey(t, alphaWorker, "max_concurrency")
	assertEffectiveEnv(t, alphaWorker, []envSpec{
		{"LAYER_ORIGIN", "role-project", "role_project"},
		{"SHARED", "from-system", "system"},
		{"WORKER_ONLY", "w", "role"},
		{"PROJECT_ONLY", "p", "project"},
	})
	alphaProfile := alphaWorker["profile"].(map[string]any)
	if alphaProfile["source"] != "role_project" ||
		alphaProfile["value"].(map[string]any)["profile_id"] != "agent-layer-rp-work" ||
		alphaWorker["harness_prompt"].(map[string]any)["from_role"] != nil ||
		alphaWorker["run_budget_usd"].(map[string]any)["value"] != 4.75 ||
		alphaWorker["system_prompt_mode"] != "replace" {
		t.Fatalf("alpha worker %#v %#v", alphaProfile, alphaWorker["run_budget_usd"])
	}
	alphaReviewer := nested(t, alpha, "roles", "reviewer", "effective")
	assertEffectiveEnv(t, alphaReviewer, []envSpec{
		{"LAYER_ORIGIN", "project", "project"},
		{"SHARED", "from-system", "system"},
		{"REVIEWER_ONLY", "r", "role"},
		{"PROJECT_ONLY", "p", "project"},
		{"REVIEWER_PROJECT", "", "role_project"},
	})
	alphaRevHarness := alphaReviewer["harness_prompt"].(map[string]any)
	if alphaRevHarness["value"] != "Alpha worker harness." || alphaRevHarness["source"] != "role_project" ||
		alphaRevHarness["from_role"] != "worker" ||
		alphaReviewer["run_budget_usd"].(map[string]any)["value"] != 3.5 ||
		alphaReviewer["run_budget_usd"].(map[string]any)["source"] != "project" {
		t.Fatalf("alpha reviewer %#v %#v", alphaRevHarness, alphaReviewer["run_budget_usd"])
	}
	alphaPlanner := nested(t, alpha, "roles", "planner", "effective")
	if alphaPlanner["harness_prompt"].(map[string]any)["value"] != "Alpha project harness." ||
		alphaPlanner["harness_prompt"].(map[string]any)["source"] != "project" ||
		alphaPlanner["profile"].(map[string]any)["value"].(map[string]any)["profile_id"] != "agent-layer-rp-plan" {
		t.Fatalf("alpha planner %#v", alphaPlanner["harness_prompt"])
	}
	assertNoKey(t, nested(t, alpha, "roles", "planner", "values"), "system_prompt_mode")

	betaWorker := nested(t, beta, "roles", "worker", "effective")
	assertNoKey(t, betaWorker, "max_concurrency")
	assertEffectiveEnv(t, betaWorker, []envSpec{
		{"LAYER_ORIGIN", "worker-role", "role"},
		{"SHARED", "from-system", "system"},
		{"DROP_ME", "system-drop", "system"},
		{"WORKER_ONLY", "w", "role"},
	})
	if betaWorker["profile"].(map[string]any)["source"] != "role" {
		t.Fatalf("beta worker %#v", betaWorker["profile"])
	}
	betaValues := nested(t, beta, "project", "values")
	if betaValues["harness_prompt"] != nil || len(betaValues["env"].([]any)) != 0 {
		t.Fatalf("beta project %#v", betaValues)
	}
	blankReviewer := nested(t, blank, "roles", "reviewer", "effective")
	assertEffectiveEnv(t, blankReviewer, []envSpec{
		{"LAYER_ORIGIN", "system", "system"},
		{"REVIEWER_ONLY", "r", "role"},
	})
	blankHarness := blankReviewer["harness_prompt"].(map[string]any)
	if blankHarness["source"] != "role" || blankHarness["from_role"] != "worker" {
		t.Fatalf("blank reviewer harness %#v", blankHarness)
	}
	blankEnv := nested(t, blank, "roles", "reviewer", "values")["env"].([]any)
	if len(blankEnv) != 1 || blankEnv[0].(map[string]any)["key"] != "SHARED" || blankEnv[0].(map[string]any)["value"] != nil {
		t.Fatalf("blank reviewer values %#v", blankEnv)
	}
	if nested(t, blank, "roles", "worker", "effective")["harness_prompt"].(map[string]any)["source"] != "role" {
		t.Fatal("blank worker harness")
	}
	if nested(t, beta, "roles", "reviewer", "effective")["env"].([]any)[1].(map[string]any)["key"] != "SHARED" {
		t.Fatal("beta reviewer lost SHARED")
	}
}

func TestReviewerHarnessOverridesWorker(t *testing.T) {
	tests := []struct {
		name      string
		scope     string
		projectID string
		value     string
	}{
		{name: "role", scope: "role", value: "Reviewer role harness."},
		{name: "role project", scope: "role_project", projectID: "proj-alpha0001", value: "Alpha reviewer harness."},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rows := append(seedSettingRows(), settingRow(tt.scope, "reviewer", tt.projectID, map[string]any{
				"harness_prompt": tt.value,
			}))
			layers := indexLayers(rows, tt.projectID)
			view := settingsView(layers)
			if tt.projectID != "" {
				view = projectSettingsView(tt.projectID, layers)
			}
			harness := nested(t, view, "roles", "reviewer", "effective", "harness_prompt")
			if harness["value"] != tt.value || harness["source"] != tt.scope {
				t.Fatalf("reviewer harness %#v, want %q from %s", harness, tt.value, tt.scope)
			}
			assertNoKey(t, harness, "from_role")
		})
	}
}

func TestHarnessEdges(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		want string
	}{
		{name: "blank lines", raw: "\nBe careful.\n", want: "Be careful."},
		{name: "whitespace", raw: " \n\t", want: ""},
		{name: "newline", raw: " \n", want: ""},
		{name: "kept indent", raw: "  Keep  \n", want: "  Keep"},
		{name: "form feed line", raw: "\x1c\nKeep", want: "\x1c\nKeep"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := normalizeHarness(tt.raw); got != tt.want {
				t.Fatalf("got %q", got)
			}
		})
	}

	padded := "  " + defaultHarnessPrompt
	cases := []struct {
		name   string
		rows   []map[string]any
		source string
		value  string
		stored any
	}{
		{
			name:   "system builtin",
			rows:   []map[string]any{{"scope": "system", "harness_prompt": defaultHarnessPrompt}},
			source: "builtin", value: defaultHarnessPrompt, stored: defaultHarnessPrompt,
		},
		{
			name:   "system padded builtin",
			rows:   []map[string]any{{"scope": "system", "harness_prompt": padded}},
			source: "builtin", value: defaultHarnessPrompt, stored: padded,
		},
		{
			name:   "system old wording",
			rows:   []map[string]any{{"scope": "system", "harness_prompt": oldBuiltinHarness}},
			source: "builtin", value: defaultHarnessPrompt, stored: oldBuiltinHarness,
		},
		{
			name:   "role keeps builtin text",
			rows:   []map[string]any{{"scope": "role", "role": "worker", "harness_prompt": defaultHarnessPrompt}},
			source: "role", value: defaultHarnessPrompt, stored: defaultHarnessPrompt,
		},
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			view := settingsView(indexLayers(tt.rows, ""))
			values := nested(t, view, "system", "values")
			eff := nested(t, view, "roles", "worker", "effective")
			if tt.source == "builtin" {
				eff = nested(t, view, "system", "effective")
			}
			harness := eff["harness_prompt"].(map[string]any)
			if harness["source"] != tt.source || harness["value"] != tt.value {
				t.Fatalf("effective %#v", harness)
			}
			if tt.source == "builtin" && values["harness_prompt"] != tt.stored {
				t.Fatalf("stored %#v", values["harness_prompt"])
			}
		})
	}
}

func TestSystemResolveIgnoresLaterLayers(t *testing.T) {
	system := "System harness."
	task := "Task harness."
	project := "Alpha project harness."
	layers := map[layerKey]layerValues{
		{scope: "system"}:  {harness: &system},
		{scope: "task"}:    {harness: &task},
		{scope: "project"}: {harness: &project},
	}
	got := resolveAgentSettings("", layers, "proj-alpha0001")
	if got.harnessText != system || got.harnessSource != "system" || got.fromRole != "" {
		t.Fatalf("%s from %s", got.harnessText, got.harnessSource)
	}
}

func TestEnvOrderAndParse(t *testing.T) {
	first, second, updated, added := "1", "2", "3", "4"
	layers := map[layerKey]layerValues{
		{scope: "system"}: {env: []envBinding{{key: "A", value: &first}, {key: "B", value: &second}}},
		{scope: "role", role: "worker"}: {env: []envBinding{
			{key: "A", value: &updated}, {key: "B", value: nil}, {key: "C", value: &added},
		}},
	}
	assertEffectiveEnv(t, effectiveView("worker", layers, ""), []envSpec{{"A", "3", "role"}, {"C", "4", "role"}})

	tests := []struct {
		name  string
		scope string
		env   any
		want  int
		null  bool
	}{
		{name: "unset", scope: "role", env: envEntries("A", nil), want: 1, null: true},
		{name: "empty string", scope: "role", env: envEntries("A", ""), want: 1},
		{name: "system null", scope: "system", env: envEntries("A", nil), want: -1},
		{name: "credential", scope: "role", env: envEntries("API_KEY", "secret-value", "SHARED", "kept"), want: -1},
		{name: "duplicate", scope: "role", env: envEntries("A", "1", "A", "2"), want: -1},
		{name: "extra", scope: "role", env: []any{map[string]any{"key": "A", "value": "1", "extra": true}}, want: -1},
		{name: "not list", scope: "role", env: map[string]any{}, want: -1},
		{name: "number", scope: "role", env: envEntries("A", 1), want: -1},
		{name: "nul", scope: "role", env: envEntries("A", "bad\x00"), want: -1},
		{name: "name", scope: "role", env: envEntries("lower", "1"), want: -1},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			row := layerFromRow(map[string]any{
				"scope": tt.scope, "profile": catalogProfile("agent-ok", "opus", "low"), "env": tt.env,
			})
			if row.profile == nil || row.profile["profile_id"] != "agent-ok" {
				t.Fatal("profile dropped")
			}
			if tt.want < 0 {
				if row.env != nil {
					t.Fatalf("env %#v", row.env)
				}
				return
			}
			if len(row.env) != tt.want {
				t.Fatalf("env %#v", row.env)
			}
			if tt.null && row.env[0].value != nil {
				t.Fatal("expected null")
			}
			if tt.name == "empty string" && (row.env[0].value == nil || *row.env[0].value != "") {
				t.Fatal("empty string dropped")
			}
		})
	}

	many := make([]any, 0, 51)
	for i := 0; i < 51; i++ {
		many = append(many, map[string]any{"key": "V" + strings.Repeat("A", i), "value": "1"})
	}
	if env, err := parseEnv(many, true); err == nil || env != nil {
		t.Fatal("51 entries accepted")
	}
	long := map[string]any{"key": "NOTE", "value": strings.Repeat("x", envValueMaxChars+1)}
	if _, err := parseEnv([]any{long}, true); err == nil {
		t.Fatal("long value accepted")
	}
	row := layerFromRow(map[string]any{
		"scope": "system", "profile_id": "agent-raw",
		"profile":        catalogProfile("agent-parsed", "opus", "low"),
		"harness_prompt": 12, "run_budget_usd": true,
	})
	if row.view()["profile_id"] != "agent-parsed" || row.harness != nil || row.budget != nil {
		t.Fatalf("profile %#v harness %v", row.view()["profile_id"], row.harness)
	}
	bad := layerFromRow(map[string]any{"scope": "system", "profile_id": "agent-raw", "profile": map[string]any{"id": "nope"}})
	if bad.view()["profile_id"] != nil {
		t.Fatalf("invalid profile %#v", bad.view()["profile_id"])
	}
	blank := layerFromRow(map[string]any{"scope": "role", "role": "planner", "harness_prompt": " \n\t"})
	if blank.harness != nil || blank.view()["harness_prompt"] != nil {
		t.Fatal("whitespace harness stored")
	}
	assertNoKey(t, blank.view(), "system_prompt_mode")
	emptyEffort := layerFromRow(map[string]any{
		"scope": "role", "role": "worker", "profile": catalogProfile("agent-x", "opus", ""),
	})
	view := effectiveView("worker", map[layerKey]layerValues{{scope: "role", role: "worker"}: emptyEffort}, "")
	if view["effort"].(map[string]any)["value"] != nil || view["effort"].(map[string]any)["source"] != "builtin" ||
		view["profile"].(map[string]any)["value"].(map[string]any)["effort"] != "" {
		t.Fatalf("empty effort %#v", view["effort"])
	}
}

func TestResolveTaskConcurrency(t *testing.T) {
	roleLimit := 4
	taskLimit := 3
	projectLimit := 9
	tests := []struct {
		name   string
		role   string
		layers map[layerKey]layerValues
		value  int
		source string
	}{
		{name: "worker builtin", role: "worker", value: builtinWorkerLimit, source: "builtin"},
		{name: "reviewer builtin", role: "reviewer", value: builtinReviewerLimit, source: "builtin"},
		{
			name: "role", role: "worker", value: 4, source: "role",
			layers: map[layerKey]layerValues{{scope: "role", role: "worker"}: {limit: &roleLimit}},
		},
		{
			name: "task beats role", role: "worker", value: 3, source: "task",
			layers: map[layerKey]layerValues{
				{scope: "role", role: "worker"}: {limit: &roleLimit},
				{scope: "task", role: "worker"}: {limit: &taskLimit},
			},
		},
		{
			name: "project ignored", role: "worker", value: builtinWorkerLimit, source: "builtin",
			layers: map[layerKey]layerValues{
				{scope: "project"}:                      {limit: &projectLimit},
				{scope: "planner_task", role: "worker"}: {limit: &projectLimit},
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			value, source := resolveTaskConcurrency(tt.role, tt.layers)
			if value != tt.value || source != tt.source {
				t.Fatalf("got %d %s", value, source)
			}
			applied := concurrencyView(tt.role, tt.layers)
			if applied["value"] != tt.value || applied["source"] != tt.source {
				t.Fatalf("view %#v", applied)
			}
		})
	}
	layers := map[layerKey]layerValues{{scope: "role", role: "worker"}: {limit: &roleLimit}}
	assertNoKey(t, effectiveView("worker", layers, "proj-alpha0001"), "max_concurrency")
	got := effectiveView("worker", layers, "")["max_concurrency"].(map[string]any)
	if got["value"] != 4 || got["source"] != "role" {
		t.Fatalf("default %#v", got)
	}
}

func TestTaskLayerStillResolves(t *testing.T) {
	layers := indexLayers(seedSettingRows(), "proj-alpha0001")
	layers[layerKey{scope: "task", role: "worker"}] = layerFromRow(map[string]any{
		"scope": "task", "role": "worker", "max_concurrency": 3,
		"profile": catalogProfile("agent-layer-task-work", "layer-task-work", "low"),
	})
	view := effectiveView("worker", layers, "proj-alpha0001")
	profile := view["profile"].(map[string]any)
	if profile["source"] != "task" || profile["value"].(map[string]any)["profile_id"] != "agent-layer-task-work" ||
		view["system_prompt_mode"] != "replace" {
		t.Fatalf("profile %#v mode %#v", profile, view["system_prompt_mode"])
	}
	layers[layerKey{scope: "task", role: "worker"}] = layerFromRow(map[string]any{
		"scope": "task", "role": "worker", "system_prompt_mode": "append",
		"profile": catalogProfile("agent-layer-task-work", "layer-task-work", "low"),
	})
	if effectiveView("worker", layers, "proj-alpha0001")["system_prompt_mode"] != "append" {
		t.Fatal("task mode ignored")
	}
}
