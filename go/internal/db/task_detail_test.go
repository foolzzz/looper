package db

import (
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestRoomByStep(t *testing.T) {
	steps := []map[string]any{
		{"id": "step-s1", "step_key": "s1", "kind": "work", "role_key": "backend", "review": true, "reviewer_id": "reviewer-1"},
		{"id": "step-s2", "step_key": "s2", "kind": "work"},
		{"id": "step-g1", "step_key": "g1", "kind": "gate"},
		{"id": "step-s4", "step_key": "s4", "kind": "work", "status": "failed"},
		{"id": "step-s5", "step_key": "s5", "kind": "work", "role_key": "backend", "status": "skipped"},
	}
	messages := []map[string]any{
		{"id": 1, "sender_seat": "backend@s1", "audience": []string{"worker@s2"}, "kind": "question"},
		{"id": 2, "sender_seat": "worker@s2", "audience": []string{"backend@s1"}, "kind": "answer", "reply_to": 1},
		{"id": 3, "sender_seat": "backend@s1", "audience": []string{"worker@s4"}, "kind": "question"},
		{"id": 4, "sender_seat": "planner", "audience": []string{}, "kind": "info"},
	}
	got := roomByStep(steps, messages)
	want := map[string]any{
		"s1": map[string]any{"sent": 2, "received": 1, "unanswered": 1},
		"s2": map[string]any{"sent": 1, "received": 1, "unanswered": 0},
		"s4": map[string]any{"sent": 0, "received": 1, "unanswered": 1},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("by_step %#v", got)
	}
}

func TestGateWaitingOn(t *testing.T) {
	now := time.Date(2026, 6, 15, 12, 0, 0, 0, time.UTC)
	base := func(state string) map[string]any {
		return map[string]any{
			"state": state, "step_key": "g1", "step_id": "step-g", "topic": "Ship?",
			"turns": 0, "reminders": 0, "not_delivered": 4,
		}
	}
	tests := []struct {
		name       string
		row        map[string]any
		waitingOn  any
		delivered  int
		retryIsNil bool
	}{
		{
			name: "planner holds an undelivered user message",
			row: func() map[string]any {
				row := base("discussing")
				row["waiting_for_planner"] = true
				row["failures"] = 0
				return row
			}(),
			waitingOn: "planner", delivered: 4, retryIsNil: true,
		},
		{
			name: "retry is due",
			row: func() map[string]any {
				row := base("open")
				row["waiting_for_planner"] = false
				row["failures"] = 1
				row["failed_at"] = pyTime(now.Add(-2 * time.Minute))
				return row
			}(),
			waitingOn: "planner", delivered: 4, retryIsNil: false,
		},
		{
			name: "retry is still ahead",
			row: func() map[string]any {
				row := base("open")
				row["waiting_for_planner"] = false
				row["failures"] = 1
				row["failed_at"] = pyTime(now)
				return row
			}(),
			waitingOn: "user", delivered: 4, retryIsNil: false,
		},
		{
			name: "closed gate waits on nobody",
			row: func() map[string]any {
				row := base("passed")
				row["waiting_for_planner"] = true
				row["failures"] = 1
				row["failed_at"] = pyTime(now.Add(-time.Hour))
				row["summary"] = map[string]any{"decisions": "ship"}
				return row
			}(),
			waitingOn: nil, delivered: 0, retryIsNil: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := gateRecord(tt.row, nil, now)
			if got["waiting_on"] != tt.waitingOn || got["not_delivered"] != tt.delivered {
				t.Fatalf("waiting_on %v not_delivered %v", got["waiting_on"], got["not_delivered"])
			}
			if (got["retry_at"] == nil) != tt.retryIsNil {
				t.Fatalf("retry_at %v", got["retry_at"])
			}
			if got["channel_posts"] != nil {
				t.Fatal("channel_posts set without a chat")
			}
		})
	}
}

func TestCostSource(t *testing.T) {
	tests := []struct {
		name, runtime, auth, base, source string
		known                             bool
	}{
		{name: "claude oauth", runtime: "claude-code", auth: "oauth", source: "runtime", known: true},
		{name: "claude official api", runtime: "claude-code", auth: "api_key", base: "https://api.anthropic.com", source: "runtime", known: true},
		{name: "claude empty base", runtime: "claude-code", auth: "api_key", source: "runtime", known: true},
		{name: "claude other host", runtime: "claude-code", auth: "api_key", base: "https://example.test/v1", source: "provider_price", known: true},
		{name: "grok", runtime: "grok", auth: "api_key", base: "https://example.test", source: "runtime", known: true},
		{name: "codex", runtime: "codex", auth: "oauth", source: "provider_price", known: true},
		{name: "omp", runtime: "omp", auth: "api_key", source: "provider_price", known: true},
		{name: "unknown", runtime: "other", auth: "oauth"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			source, known := costSource(tt.runtime, tt.auth, tt.base)
			if source != tt.source || known != tt.known {
				t.Fatalf("source %q known %v", source, known)
			}
		})
	}
	priced := costBasis(map[string]any{
		"runtime_type": "claude-code", "provider_auth": "api_key",
		"provider_base_url": "https://example.test", "model": "claude-opus-5-5",
	})
	if priced["source"] != "provider_price" || priced["equivalent"] != false || len(priced["unpriced"].([]any)) != 0 {
		t.Fatalf("priced basis %#v", priced)
	}
	missing := costBasis(map[string]any{
		"runtime_type": "claude-code", "provider_auth": "api_key",
		"provider_base_url": "https://example.test", "model": "opus",
	})
	if !reflect.DeepEqual(missing["unpriced"], []any{"opus"}) {
		t.Fatalf("unpriced %#v", missing["unpriced"])
	}
}

func TestAgentsViewFrom(t *testing.T) {
	empty := agentsViewFrom(nil, "", "completed")
	if empty["concurrency_max"] != 64 || empty["editable"] != false {
		t.Fatalf("empty %#v", empty)
	}
	roles := empty["roles"].(map[string]any)
	for _, role := range []string{"planner", "worker", "reviewer"} {
		profile := roles[role].(map[string]any)["profile"].(map[string]any)
		if profile["source"] != "builtin" {
			t.Fatalf("%s source %v", role, profile["source"])
		}
		value := profile["value"].(map[string]any)
		if value["profile_id"] != "agent-claude-code-opus" || value["runtime"] != "claude-code" || value["effort"] != nil {
			t.Fatalf("%s profile %#v", role, value)
		}
	}
	worker := roles["worker"].(map[string]any)["concurrency"].(map[string]any)
	reviewer := roles["reviewer"].(map[string]any)["concurrency"].(map[string]any)
	if worker["value"] != 16 || worker["task"] != nil || reviewer["value"] != 8 {
		t.Fatalf("limits worker %#v reviewer %#v", worker, reviewer)
	}

	profile := map[string]any{
		"id": "agent-claude-code-opus", "name": "claude-code/opus", "model": "opus",
		"runtime_id": "rt-claude-code", "runtime_name": "claude-code", "runtime_type": "claude-code",
		"provider_id": "prov-claude-subscription", "provider_name": "claude-subscription", "provider_auth": "oauth",
	}
	got := agentsViewFrom([]map[string]any{
		{"scope": "task", "role": "worker", "profile": profile, "max_concurrency": 3},
		{"scope": "planner_task", "role": "reviewer", "profile": profile},
	}, "proj-alpha0001", "executing")
	if got["editable"] != true {
		t.Fatal("executing is not editable")
	}
	gotRoles := got["roles"].(map[string]any)
	gotWorker := gotRoles["worker"].(map[string]any)
	if gotWorker["profile"].(map[string]any)["source"] != "task" {
		t.Fatalf("worker profile %#v", gotWorker["profile"])
	}
	conc := gotWorker["concurrency"].(map[string]any)
	if conc["value"] != 3 || conc["source"] != "task" || conc["task"] != 3 {
		t.Fatalf("worker concurrency %#v", conc)
	}
	fallback := conc["default"].(map[string]any)
	if fallback["value"] != 16 || fallback["source"] != "builtin" {
		t.Fatalf("worker default %#v", fallback)
	}
	gotReviewer := gotRoles["reviewer"].(map[string]any)
	if gotReviewer["profile"].(map[string]any)["source"] != "planner_task" {
		t.Fatalf("reviewer profile %#v", gotReviewer["profile"])
	}
	if gotReviewer["profile"].(map[string]any)["value"].(map[string]any)["profile_id"] != "agent-claude-code-opus" {
		t.Fatalf("reviewer value %#v", gotReviewer["profile"])
	}
	revConc := gotReviewer["concurrency"].(map[string]any)
	if revConc["value"] != 8 || revConc["source"] != "builtin" || revConc["task"] != nil {
		t.Fatalf("reviewer concurrency %#v", revConc)
	}
}

func TestAgentsViewPrecedence(t *testing.T) {
	layer := func(scope, role, project, id, model, effort string, limit any) map[string]any {
		row := map[string]any{
			"scope": scope, "role": role, "project_id": project,
			"profile": map[string]any{
				"id": id, "name": "claude-code/" + model, "model": model, "effort": effort,
				"runtime_id": "rt-claude-code", "runtime_name": "claude-code", "runtime_type": "claude-code",
				"provider_id": "prov-claude-subscription", "provider_name": "claude-subscription", "provider_auth": "oauth",
			},
		}
		if role == "" {
			row["role"] = nil
		}
		if project == "" {
			row["project_id"] = nil
		}
		if limit != nil {
			row["max_concurrency"] = limit
		}
		return row
	}
	shared := []map[string]any{
		layer("system", "", "", "agent-layer-system", "layer-system", "low", nil),
		layer("role", "planner", "", "agent-layer-planner", "layer-planner", "medium", nil),
		layer("role", "worker", "", "agent-layer-worker", "layer-worker", "high", 4),
		layer("role", "reviewer", "", "agent-layer-reviewer", "layer-reviewer", "xhigh", 5),
		layer("project", "", "proj-alpha0001", "agent-layer-project", "layer-project", "max", nil),
		layer("role_project", "planner", "proj-alpha0001", "agent-layer-rp-plan", "layer-rp-plan", "low", nil),
		layer("role_project", "worker", "proj-alpha0001", "agent-layer-rp-work", "layer-rp-work", "medium", nil),
		layer("role_project", "reviewer", "proj-alpha0001", "agent-layer-rp-rev", "layer-rp-rev", "high", nil),
	}
	richRows := append(append([]map[string]any{}, shared...),
		layer("planner_task", "worker", "", "agent-layer-pt-work", "layer-pt-work", "xhigh", nil),
		layer("planner_task", "reviewer", "", "agent-layer-pt-rev", "layer-pt-rev", "max", nil),
		layer("task", "worker", "", "agent-layer-task-work", "layer-task-work", "low", 3),
	)
	rich := agentsViewFrom(richRows, "proj-alpha0001", "executing")["roles"].(map[string]any)
	assertProfile(t, rich, "planner", "role_project", "agent-layer-rp-plan", "low")
	assertProfile(t, rich, "worker", "task", "agent-layer-task-work", "low")
	assertProfile(t, rich, "reviewer", "planner_task", "agent-layer-pt-rev", "max")
	worker := rich["worker"].(map[string]any)["concurrency"].(map[string]any)
	if worker["value"] != 3 || worker["source"] != "task" || worker["task"] != 3 {
		t.Fatalf("rich worker concurrency %#v", worker)
	}
	fallback := worker["default"].(map[string]any)
	if fallback["value"] != 4 || fallback["source"] != "role" {
		t.Fatalf("rich worker default %#v", fallback)
	}
	reviewer := rich["reviewer"].(map[string]any)["concurrency"].(map[string]any)
	if reviewer["value"] != 5 || reviewer["source"] != "role" || reviewer["task"] != nil {
		t.Fatalf("rich reviewer concurrency %#v", reviewer)
	}

	// No project layer applies outside Alpha, and this task has no Planner
	// choice, so the role layer beats system and the built-in profile.
	bare := agentsViewFrom(shared, "", "init")["roles"].(map[string]any)
	assertProfile(t, bare, "planner", "role", "agent-layer-planner", "medium")
	assertProfile(t, bare, "worker", "role", "agent-layer-worker", "high")
	assertProfile(t, bare, "reviewer", "role", "agent-layer-reviewer", "xhigh")
}

func assertProfile(t *testing.T, roles map[string]any, role, source, id, effort string) {
	t.Helper()
	profile := roles[role].(map[string]any)["profile"].(map[string]any)
	value := profile["value"].(map[string]any)
	if profile["source"] != source || value["profile_id"] != id || value["model"] != strings.TrimPrefix(id, "agent-") || value["effort"] != effort {
		t.Fatalf("%s profile %#v", role, profile)
	}
}

func TestUndoCommands(t *testing.T) {
	got := undoCommands("task-rich00001", "refs/heads/main", "abc1234dead", [][2]string{{"s1", "deadbeef1"}})
	var commands []string
	for _, item := range got {
		commands = append(commands, item.(map[string]any)["command"].(string))
	}
	want := []string{
		"git log --first-parent --grep='^Looper-Task: task-rich00001$' main",
		"git switch main && git revert -m 1 deadbeef1",
		"git switch main && git reset abc1234dead",
		"git switch main && git reset --hard abc1234dead",
		"git switch -c main-task-rich00001 main && git branch -f main abc1234dead",
	}
	if !reflect.DeepEqual(commands, want) {
		t.Fatalf("commands %#v", commands)
	}
	if shortBranch("refs/heads/main") != "main" || shortBranch("main") != "main" {
		t.Fatal("shortBranch")
	}
}

func TestSHA256Text(t *testing.T) {
	const want = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
	if sha256Text("abc") != want {
		t.Fatalf("sha256 %s", sha256Text("abc"))
	}
}

func TestTaskBudgetUSD(t *testing.T) {
	tests := []struct {
		name string
		raw  any
		want any
	}{
		{name: "nil", raw: nil, want: nil},
		{name: "blank", raw: "", want: nil},
		{name: "project cap", raw: 1.5, want: 1.5},
		{name: "over the cap", raw: 200000, want: nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := taskBudgetUSD("proj-alpha0001", tt.raw); got != tt.want {
				t.Fatalf("budget %v, want %v", got, tt.want)
			}
		})
	}
}
