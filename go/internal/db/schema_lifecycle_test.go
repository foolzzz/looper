package db

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestFreshSchemaRepeatInit(t *testing.T) {
	dsn := withDisposableDB(t, "loopgo_g1i_fresh")
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	if err := EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	sqlDB, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	assertPromptModeSchema(t, ctx, sqlDB)
	var modes int
	if err := sqlDB.QueryRowContext(ctx, `SELECT count(*) FROM agent_setting_layers WHERE system_prompt_mode IS NOT NULL`).Scan(&modes); err != nil {
		t.Fatal(err)
	}
	if modes != 0 {
		t.Fatalf("fresh schema stored %d prompt modes", modes)
	}
}

func TestUpgradeSchemaKeepsRolePromptMode(t *testing.T) {
	dsn := withDisposableDB(t, "loopgo_g1i_upgrade")
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	old, err := os.ReadFile(prePromptModeSchemaPath(t))
	if err != nil {
		t.Fatal(err)
	}
	if err := ExecScript(ctx, dsn, string(old)); err != nil {
		t.Fatal(err)
	}
	sqlDB, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	statements := []string{
		`INSERT INTO agent_runtimes (id, name, type, executable, capabilities)
		 VALUES ('rt-replace', 'replace-runtime', 'claude-code', '', ARRAY['run', 'resident_session', 'resume'])`,
		`INSERT INTO llm_providers (id, name, protocols, auth, oauth_runtime_type)
		 VALUES ('prov-replace', 'replace-provider', ARRAY['anthropic-messages'], 'oauth', 'claude-code')`,
		`INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model, system_prompt_mode)
		 VALUES ('agent-replace', 'claude-code/replace', 'rt-replace', 'prov-replace', 'opus', 'replace')`,
		`INSERT INTO agent_setting_layers (scope, profile_id) VALUES ('system', 'agent-replace')`,
		`INSERT INTO agent_setting_layers (scope, role, profile_id) VALUES ('role', 'worker', 'agent-replace')`,
		`INSERT INTO agent_setting_layers (scope, role) VALUES ('role', 'planner')`,
	}
	for _, stmt := range statements {
		if _, err := sqlDB.ExecContext(ctx, stmt); err != nil {
			t.Fatal(err)
		}
	}
	if err := sqlDB.Close(); err != nil {
		t.Fatal(err)
	}
	if err := EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	sqlDB, err = Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	assertPromptModeSchema(t, ctx, sqlDB)
	var worker, reviewer, profile string
	if err := sqlDB.QueryRowContext(ctx, `SELECT system_prompt_mode FROM agent_setting_layers WHERE scope = 'role' AND role = 'worker'`).Scan(&worker); err != nil {
		t.Fatal(err)
	}
	if err := sqlDB.QueryRowContext(ctx, `SELECT system_prompt_mode FROM agent_setting_layers WHERE scope = 'role' AND role = 'reviewer'`).Scan(&reviewer); err != nil {
		t.Fatal(err)
	}
	var planner *string
	if err := sqlDB.QueryRowContext(ctx, `SELECT system_prompt_mode FROM agent_setting_layers WHERE scope = 'role' AND role = 'planner'`).Scan(&planner); err != nil {
		t.Fatal(err)
	}
	if err := sqlDB.QueryRowContext(ctx, `SELECT system_prompt_mode FROM agent_profiles WHERE id = 'agent-replace'`).Scan(&profile); err != nil {
		t.Fatal(err)
	}
	if worker != "replace" || reviewer != "replace" || planner != nil || profile != "append" {
		t.Fatalf("worker %s reviewer %s planner %#v profile %s", worker, reviewer, planner, profile)
	}
}

func assertPromptModeSchema(t *testing.T, ctx context.Context, sqlDB *sql.DB) {
	t.Helper()
	checks := []struct {
		name  string
		query string
	}{
		{"system_prompt_mode column", `SELECT count(*) FROM information_schema.columns WHERE table_name = 'agent_setting_layers' AND column_name = 'system_prompt_mode'`},
		{"custom_prompt column", `SELECT count(*) FROM information_schema.columns WHERE table_name = 'agent_profiles' AND column_name = 'custom_prompt'`},
		{"task_system_prompt_modes", `SELECT count(*) FROM information_schema.tables WHERE table_name = 'task_system_prompt_modes'`},
	}
	for _, check := range checks {
		var n int
		if err := sqlDB.QueryRowContext(ctx, check.query).Scan(&n); err != nil {
			t.Fatal(err)
		}
		if n != 1 {
			t.Fatalf("%s count %d", check.name, n)
		}
	}
	for _, check := range []struct {
		name  string
		query string
		want  string
	}{
		{"runtime capabilities", `SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint WHERE conrelid = 'agent_runtimes'::regclass AND contype = 'c'`, "steer"},
		{"gate channel", `SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint WHERE conrelid = 'task_user_gate_posts'::regclass AND contype = 'c'`, "lark_bot"},
		{"schedule channel", `SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint WHERE conrelid = 'schedules'::regclass AND contype = 'c'`, "lark_bot"},
	} {
		var def string
		if err := sqlDB.QueryRowContext(ctx, check.query).Scan(&def); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(def, check.want) {
			t.Fatalf("%s definition %s", check.name, def)
		}
	}
}

func prePromptModeSchemaPath(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("schema fixture path")
	}
	return filepath.Join(filepath.Dir(file), "testdata", "schema_pre_prompt_modes.sql")
}
