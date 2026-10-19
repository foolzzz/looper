package db

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"math"
	"strings"
	"testing"
	"time"
)

func TestBuildChatTargets(t *testing.T) {
	tasks := []map[string]any{
		{"id": "task-old", "requirement": "older", "title": "Old title", "status": "completed", "created_at": "2020-01-01T00:00:00+00:00"},
		{"id": "task-b", "requirement": "bee", "goal_id": "goal-b", "status": "running", "created_at": "2020-06-01T00:00:00+00:00"},
		{"id": "task-a", "requirement": "aye", "status": "init", "created_at": "2020-06-01T00:00:00+00:00"},
	}
	steps := []map[string]any{
		{"id": "step-1", "task_id": "task-b", "step_index": int32(0), "instruction": "first", "worker_id": "worker-1"},
		{"id": "step-2", "task_id": "task-b", "step_index": int32(1), "instruction": "second", "worker_id": "worker-1"},
		{"id": "step-3", "task_id": "task-b", "step_index": int32(2), "instruction": "by id", "worker_id": ""},
	}
	runs := []map[string]any{
		{"agent_id": "worker-1", "task_id": "task-b", "status": "running", "created_at": "2020-05-01T00:00:00+00:00", "step_id": nil},
		{"agent_id": "worker-1", "task_id": "task-b", "status": "completed", "created_at": "2020-05-02T00:00:00+00:00", "step_id": ""},
		{
			"agent_id": "reviewer-1", "task_id": "task-b", "status": "completed",
			"created_at": "2020-05-03T00:00:00+00:00", "step_id": "missing",
			"task_role": map[string]any{"id": "backend", "kind": "work", "version": float64(2)},
		},
		{
			"agent_id": "worker-role", "task_id": "task-b", "status": "completed",
			"created_at": "2020-05-04T00:00:00+00:00", "step_id": "step-3",
			"task_role": map[string]any{"id": "qa", "kind": "other", "version": float64(1)},
		},
		{"agent_id": "worker-other", "task_id": "task-old", "status": "completed", "created_at": "2020-01-02T00:00:00+00:00"},
	}
	got := buildChatTargets(tasks, steps, runs)
	if len(got) != 1+3+4 {
		t.Fatalf("len %d", len(got))
	}
	if got[0]["kind"] != "manager" || got[0]["id"] != "manager" || got[0]["can_send"] != true {
		t.Fatalf("manager %#v", got[0])
	}
	// Same created_at keeps the incoming order: task-b then task-a, then the older task.
	wantIDs := []string{"task-b", "task-a", "task-old"}
	planners := []map[string]any{got[1], got[5], got[6]}
	for i, row := range planners {
		if row["kind"] != "planner" || row["id"] != "planner-"+wantIDs[i] || row["task_id"] != wantIDs[i] || row["can_send"] != false {
			t.Fatalf("planner %d %#v", i, row)
		}
	}
	if planners[0]["name"] != "goal-b" || planners[0]["requirement"] != "bee" || planners[0]["status"] != "running" {
		t.Fatalf("named planner %#v", planners[0])
	}
	if planners[2]["name"] != "Old title" {
		t.Fatalf("title %#v", planners[2])
	}
	worker := got[2]
	if worker["id"] != "worker-1" || worker["kind"] != "worker" || worker["status"] != "completed" || worker["started_at"] != "2020-05-01T00:00:00+00:00" {
		t.Fatalf("worker %#v", worker)
	}
	if worker["step_index"] != int32(1) || worker["instruction"] != "second" || worker["can_send"] != false {
		t.Fatalf("worker step %#v", worker)
	}
	if _, ok := worker["task_role"]; ok {
		t.Fatal("worker kept task_role")
	}
	reviewer := got[3]
	role, _ := reviewer["task_role"].(map[string]any)
	if reviewer["kind"] != "reviewer" || reviewer["step_index"] != nil || reviewer["instruction"] != "" || role["id"] != "backend" || role["version"] != 2 {
		t.Fatalf("reviewer %#v", reviewer)
	}
	byID := got[4]
	if byID["id"] != "worker-role" || byID["instruction"] != "by id" || byID["step_index"] != int32(2) {
		t.Fatalf("step id %#v", byID)
	}
	if _, ok := byID["task_role"]; ok {
		t.Fatal("bad kind kept task_role")
	}
	if got[7]["id"] != "worker-other" || got[7]["step_index"] != nil || got[7]["instruction"] != "" {
		t.Fatalf("old worker %#v", got[7])
	}
}

func TestChatRunRoleVersionMatchesPythonInt(t *testing.T) {
	tests := []struct {
		name    string
		version any
		want    any
		ok      bool
	}{
		{name: "string", version: "2", want: 2, ok: true},
		{name: "spaces and sign", version: " \n+2\t", want: 2, ok: true},
		{name: "underscores", version: "2_000", want: 2000, ok: true},
		{name: "leading zeros", version: "02", want: 2, ok: true},
		{name: "negative", version: "-3", want: -3, ok: true},
		{name: "unicode digits", version: "１", want: 1, ok: true},
		{name: "unicode negative", version: "  -２  ", want: -2, ok: true},
		{name: "zeros at the digit limit", version: strings.Repeat("0", pythonIntMaxDigits), want: 0, ok: true},
		{name: "float", version: 2.9, want: 2, ok: true},
		{name: "negative float", version: -2.9, want: -2, ok: true},
		{name: "finite float past int", version: math.Ldexp(1, 63), want: json.Number("9223372036854775808"), ok: true},
		{name: "bool", version: true, want: 1, ok: true},
		{name: "json number", version: json.Number("2"), want: 2, ok: true},
		{name: "json number fraction", version: json.Number("2.9"), want: 2, ok: true},
		// int(float("9"*40 + ".0")) in CPython. The token is a float, so the
		// integer is the truncated float, and the role is kept.
		{name: "json number whole fraction past int", version: json.Number(strings.Repeat("9", 40) + ".0"), want: json.Number("10000000000000000303786028427003666890752"), ok: true},
		{name: "json number does not fit in int", version: json.Number(strings.Repeat("9", 40)), want: json.Number(strings.Repeat("9", 40)), ok: true},
		{name: "negative json number does not fit in int", version: json.Number("-" + strings.Repeat("9", 40)), want: json.Number("-" + strings.Repeat("9", 40)), ok: true},
		{name: "decimal string", version: "2.0", ok: false},
		{name: "space after sign", version: "+ 2", ok: false},
		{name: "trailing underscore", version: "2_", ok: false},
		{name: "file separator is not stripped", version: "\x1c2", ok: false},
		{name: "past the digit limit", version: strings.Repeat("0", pythonIntMaxDigits+1), ok: false},
		{name: "does not fit in int", version: strings.Repeat("9", 40), want: json.Number(strings.Repeat("9", 40)), ok: true},
		{name: "negative does not fit in int", version: "-" + strings.Repeat("9", 40), want: json.Number("-" + strings.Repeat("9", 40)), ok: true},
		{name: "nan", version: math.NaN(), ok: false},
		{name: "blank", version: "  ", ok: false},
		{name: "nil", version: nil, ok: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			role := chatRunRole(map[string]any{"id": "backend", "kind": "work", "version": tt.version})
			if !tt.ok {
				if role != nil {
					t.Fatalf("role %#v", role)
				}
				return
			}
			if role == nil || role["id"] != "backend" || role["kind"] != "work" || role["version"] != tt.want {
				t.Fatalf("role %#v, want version %#v", role, tt.want)
			}
			if number, ok := tt.want.(json.Number); ok {
				body, err := json.Marshal(role)
				if err != nil {
					t.Fatal(err)
				}
				if !bytes.Contains(body, []byte(`"version":`+string(number))) || bytes.Contains(body, []byte(`"version":"`)) {
					t.Fatalf("marshal %s", body)
				}
			}
		})
	}
}

func TestChatTargetsKeepsTasksWhenLaterReadsFail(t *testing.T) {
	dsn := withDisposableDB(t, "loopgo_g1efix_chat")
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
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
		`INSERT INTO projects (id, name) VALUES ('proj-1', 'P')`,
		`INSERT INTO tasks (id, requirement, project_id) VALUES ('task-1', 'ship it', 'proj-1')`,
		`INSERT INTO task_steps (id, task_id, step_index, instruction, worker_id) VALUES ('step-1', 'task-1', 0, 'do it', 'worker-1')`,
		`INSERT INTO llm_records (agent_id, task_id, metadata) VALUES ('worker-1', 'task-1', '{"prompt_layers":{"task_role":{"id":"backend","kind":"work","version":"2"}}}'::jsonb)`,
	}
	for _, stmt := range statements {
		if _, err := sqlDB.ExecContext(ctx, stmt); err != nil {
			t.Fatal(err)
		}
	}
	var logs bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })

	store := NewStore(sqlDB)
	got, err := store.ChatTargets(ctx)
	if err != nil {
		t.Fatal(err)
	}
	assertChatPlanner(t, got)
	worker := findChatTarget(got, "worker-1")
	role, _ := worker["task_role"].(map[string]any)
	version, versionOK := role["version"].(int)
	if worker == nil || role["id"] != "backend" || role["kind"] != "work" || !versionOK || version != 2 {
		t.Fatalf("worker %#v", worker)
	}

	if _, err := sqlDB.ExecContext(ctx, `ALTER TABLE llm_records RENAME TO llm_records_hidden`); err != nil {
		t.Fatal(err)
	}
	got, err = store.ChatTargets(ctx)
	if err != nil {
		t.Fatalf("run query failure discarded tasks: %v", err)
	}
	assertChatPlanner(t, got)
	if findChatTarget(got, "worker-1") != nil {
		t.Fatal("worker survived a failed run query")
	}
	if !strings.Contains(logs.String(), "42P01") || strings.Contains(logs.String(), "postgres:postgres@") {
		t.Fatalf("log %q", logs.String())
	}

	if _, err := sqlDB.ExecContext(ctx, `ALTER TABLE task_steps RENAME TO task_steps_hidden`); err != nil {
		t.Fatal(err)
	}
	got, err = store.ChatTargets(ctx)
	if err != nil {
		t.Fatalf("step query failure discarded tasks: %v", err)
	}
	assertChatPlanner(t, got)

	if _, err := sqlDB.ExecContext(ctx, `ALTER TABLE projects RENAME TO projects_hidden`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ChatTargets(ctx); err == nil {
		t.Fatal("task query failure was kept")
	}
}

func TestChatTargetKeepsBareJSONRoleVersion(t *testing.T) {
	version := strings.Repeat("9", 40)
	body, err := json.Marshal(map[string]any{
		"prompt_layers": map[string]any{
			"task_role": map[string]any{"id": "backend", "kind": "work", "version": json.Number(version)},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	dsn := withDisposableDB(t, "loopgo_g1efix_role")
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	if err := EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	sqlDB, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	statements := []struct {
		sql  string
		args []any
	}{
		{sql: `INSERT INTO projects (id, name) VALUES ('proj-1', 'P')`},
		{sql: `INSERT INTO tasks (id, requirement, project_id) VALUES ('task-1', 'ship it', 'proj-1')`},
		{sql: `INSERT INTO llm_records (agent_id, task_id, metadata) VALUES ('worker-1', 'task-1', $1::jsonb)`, args: []any{string(body)}},
	}
	for _, stmt := range statements {
		if _, err := sqlDB.ExecContext(ctx, stmt.sql, stmt.args...); err != nil {
			t.Fatal(err)
		}
	}
	got, err := NewStore(sqlDB).ChatTargets(ctx)
	if err != nil {
		t.Fatal(err)
	}
	worker := findChatTarget(got, "worker-1")
	role, _ := worker["task_role"].(map[string]any)
	number, ok := role["version"].(json.Number)
	if worker == nil || role["id"] != "backend" || role["kind"] != "work" || !ok || string(number) != version {
		t.Fatalf("worker %#v", worker)
	}
	encoded, err := json.Marshal(role)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(encoded, []byte(`"version":`+version)) || bytes.Contains(encoded, []byte(`"version":"`)) {
		t.Fatalf("marshal %s", encoded)
	}
}

func assertChatPlanner(t *testing.T, rows []map[string]any) {
	t.Helper()
	row := findChatTarget(rows, "planner-task-1")
	if row == nil || row["kind"] != "planner" || row["requirement"] != "ship it" || row["can_send"] != false {
		t.Fatalf("planner %#v", row)
	}
}

func findChatTarget(rows []map[string]any, id string) map[string]any {
	for _, row := range rows {
		if asString(row["id"]) == id {
			return row
		}
	}
	return nil
}
