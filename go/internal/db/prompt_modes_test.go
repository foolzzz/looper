package db

import (
	"context"
	"testing"
	"time"
)

func TestPromptModesAndAllowList(t *testing.T) {
	dsn := withDisposableDB(t, "loopgo_g1i_modes")
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	if err := EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	sqlDB, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	statements := []string{
		`INSERT INTO projects (id, name) VALUES ('proj-modes0001', 'Modes'), ('proj-open000001', 'Open')`,
		`INSERT INTO tasks (id, requirement, project_id) VALUES
		 ('task-modes0001', 'modes', 'proj-modes0001'),
		 ('task-open000001', 'open', 'proj-open000001')`,
		`INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES
		 ('agent-listed0001', 'listed', 'rt-claude-code', 'prov-claude-subscription', 'opus'),
		 ('agent-offlist001', 'off-list', 'rt-claude-code', 'prov-claude-subscription', 'sonnet')`,
		`INSERT INTO agent_runtimes (id, name, type, executable, capabilities)
		 VALUES ('rt-no-run', 'no-run', 'custom', '', ARRAY['mcp'])`,
		`INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model)
		 VALUES ('agent-norun0001', 'no-run', 'rt-no-run', 'prov-claude-subscription', 'opus')`,
		`INSERT INTO agent_setting_layers (scope, role, profile_id, system_prompt_mode)
		 VALUES ('role', 'worker', 'agent-listed0001', 'replace')`,
		`INSERT INTO task_planner_profiles (task_id, role, profile_id) VALUES
		 ('task-modes0001', 'worker', 'agent-offlist001'),
		 ('task-open000001', 'worker', 'agent-offlist001')`,
		`INSERT INTO project_allowed_profiles (project_id, profile_id) VALUES ('proj-modes0001', 'agent-listed0001')`,
	}
	for _, stmt := range statements {
		if _, err := sqlDB.ExecContext(ctx, stmt); err != nil {
			t.Fatal(err)
		}
	}
	store := NewStore(sqlDB)

	restricted, err := store.taskAgents(ctx, "proj-modes0001", "task-modes0001", "executing")
	if err != nil {
		t.Fatal(err)
	}
	worker := restricted["roles"].(map[string]any)["worker"].(map[string]any)
	if id := worker["profile"].(map[string]any)["value"].(map[string]any)["profile_id"]; id != "agent-listed0001" {
		t.Fatalf("off-list planner choice counted: %#v", worker["profile"])
	}
	mode := worker["system_prompt_mode"].(map[string]any)
	if mode["value"] != "replace" || mode["source"] != "role" {
		t.Fatalf("inherited mode %#v", mode)
	}

	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO task_system_prompt_modes (task_id, role, system_prompt_mode) VALUES ('task-modes0001', 'worker', 'append')`); err != nil {
		t.Fatal(err)
	}
	overridden, err := store.taskAgents(ctx, "proj-modes0001", "task-modes0001", "executing")
	if err != nil {
		t.Fatal(err)
	}
	mode = overridden["roles"].(map[string]any)["worker"].(map[string]any)["system_prompt_mode"].(map[string]any)
	if mode["value"] != "append" || mode["source"] != "task" {
		t.Fatalf("task mode %#v", mode)
	}
	if _, err := sqlDB.ExecContext(ctx, `DELETE FROM task_system_prompt_modes WHERE task_id = 'task-modes0001'`); err != nil {
		t.Fatal(err)
	}
	cleared, err := store.taskAgents(ctx, "proj-modes0001", "task-modes0001", "executing")
	if err != nil {
		t.Fatal(err)
	}
	mode = cleared["roles"].(map[string]any)["worker"].(map[string]any)["system_prompt_mode"].(map[string]any)
	if mode["value"] != "replace" || mode["source"] != "role" {
		t.Fatalf("cleared mode %#v", mode)
	}

	open, err := store.taskAgents(ctx, "proj-open000001", "task-open000001", "executing")
	if err != nil {
		t.Fatal(err)
	}
	if id := open["roles"].(map[string]any)["worker"].(map[string]any)["profile"].(map[string]any)["value"].(map[string]any)["profile_id"]; id != "agent-offlist001" {
		t.Fatalf("empty list dropped a planner choice: %#v", open["roles"].(map[string]any)["worker"])
	}

	alpha, err := store.AllowedProfiles(ctx, "proj-modes0001")
	if err != nil {
		t.Fatal(err)
	}
	if alpha["restricted"] != true || len(alpha["profiles"].([]any)) != 1 || len(alpha["choosable"].([]any)) != 1 {
		t.Fatalf("restricted view %#v", alpha)
	}
	if alpha["choosable"].([]any)[0].(map[string]any)["profile_id"] != "agent-listed0001" {
		t.Fatalf("choosable %#v", alpha["choosable"])
	}
	empty, err := store.AllowedProfiles(ctx, "proj-open000001")
	if err != nil {
		t.Fatal(err)
	}
	if empty["restricted"] != false || len(empty["profiles"].([]any)) != 0 {
		t.Fatalf("empty list view %#v", empty)
	}
	ids := map[string]bool{}
	for _, item := range empty["choosable"].([]any) {
		row := item.(map[string]any)
		ids[row["profile_id"].(string)] = true
		roles, _ := row["roles"].([]any)
		if len(roles) == 0 {
			t.Fatalf("choosable profile cannot serve worker or reviewer: %#v", row)
		}
	}
	if !ids["agent-listed0001"] || !ids["agent-offlist001"] || !ids["agent-claude-code-opus"] || ids["agent-norun0001"] {
		t.Fatalf("choosable ids %#v", ids)
	}
	if _, err := store.AllowedProfiles(ctx, "proj-missing01"); err != ErrProjectNotFound {
		t.Fatalf("missing project: %v", err)
	}
}
