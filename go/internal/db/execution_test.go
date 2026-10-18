package db

import (
	"bytes"
	"log/slog"
	"math"
	"reflect"
	"strings"
	"testing"
)

func TestExecutionViewIgnoresInvalid(t *testing.T) {
	tests := []struct {
		name string
		in   map[string]any
		key  string
		want any
		warn bool
	}{
		{name: "unset", in: map[string]any{}, key: "supervise_interval_seconds", want: nil},
		{name: "interval schema max", in: map[string]any{"id": "proj-x", "supervise_interval_seconds": int32(math.MaxInt32)}, key: "supervise_interval_seconds", want: nil, warn: true},
		{name: "interval top", in: map[string]any{"supervise_interval_seconds": 86400}, key: "supervise_interval_seconds", want: 86400},
		{name: "interval over", in: map[string]any{"supervise_interval_seconds": 86401}, key: "supervise_interval_seconds", want: nil, warn: true},
		{name: "interval never", in: map[string]any{"supervise_interval_seconds": 0}, key: "supervise_interval_seconds", want: 0},
		{name: "interval bool", in: map[string]any{"supervise_interval_seconds": true}, key: "supervise_interval_seconds", want: nil, warn: true},
		{name: "interval float", in: map[string]any{"supervise_interval_seconds": 30.0}, key: "supervise_interval_seconds", want: nil, warn: true},
		{name: "budget ok", in: map[string]any{"task_budget_usd": 1.5}, key: "task_budget_usd", want: 1.5},
		{name: "budget over cap", in: map[string]any{"task_budget_usd": 200000.0}, key: "task_budget_usd", want: nil, warn: true},
		{name: "budget zero", in: map[string]any{"task_budget_usd": 0.0}, key: "task_budget_usd", want: nil, warn: true},
		{name: "budget negative", in: map[string]any{"task_budget_usd": -1.0}, key: "task_budget_usd", want: nil, warn: true},
		{name: "budget bool", in: map[string]any{"task_budget_usd": true}, key: "task_budget_usd", want: nil, warn: true},
		{name: "budget extra decimal", in: map[string]any{"task_budget_usd": 1.23456}, key: "task_budget_usd", want: nil, warn: true},
		{name: "budget four decimals", in: map[string]any{"task_budget_usd": 1.2345}, key: "task_budget_usd", want: 1.2345},
		{name: "budget just over four decimals", in: map[string]any{"task_budget_usd": 1.00000000001}, key: "task_budget_usd", want: nil, warn: true},
		{name: "budget fifth decimal", in: map[string]any{"task_budget_usd": 0.00001}, key: "task_budget_usd", want: nil, warn: true},
		{name: "lessons bad", in: map[string]any{"distill_lessons": "sometimes"}, key: "distill_lessons", want: "off", warn: true},
		{name: "lessons true", in: map[string]any{"distill_lessons": true}, key: "distill_lessons", want: "write"},
		{name: "lessons false", in: map[string]any{"distill_lessons": false}, key: "distill_lessons", want: "off"},
		{name: "lessons empty", in: map[string]any{"distill_lessons": ""}, key: "distill_lessons", want: "off"},
		{name: "lessons draft", in: map[string]any{"distill_lessons": "draft"}, key: "distill_lessons", want: "draft"},
		{name: "links ok", in: map[string]any{"workspace_links": []string{".venv"}}, key: "workspace_links", want: []any{".venv"}},
		{name: "links overlap", in: map[string]any{"workspace_links": []string{".venv", ".venv/lib"}}, key: "workspace_links", want: nil, warn: true},
		{name: "links parent", in: map[string]any{"workspace_links": []any{"../secret"}}, key: "workspace_links", want: nil, warn: true},
		{name: "links empty", in: map[string]any{"workspace_links": []string{}}, key: "workspace_links", want: nil},
		{name: "links 100 han", in: map[string]any{"workspace_links": []string{strings.Repeat("中", 100)}}, key: "workspace_links", want: []any{strings.Repeat("中", 100)}},
		{name: "links 255 han", in: map[string]any{"workspace_links": []string{strings.Repeat("中", 255)}}, key: "workspace_links", want: []any{strings.Repeat("中", 255)}},
		{name: "links 256 han", in: map[string]any{"workspace_links": []string{strings.Repeat("中", 256)}}, key: "workspace_links", want: nil, warn: true},
		{name: "review bad", in: map[string]any{"review_policy": "none"}, key: "review_policy", want: nil, warn: true},
		{name: "review ok", in: map[string]any{"review_policy": "all"}, key: "review_policy", want: "all"},
		{name: "room low", in: map[string]any{"room_post_per_minute": 0}, key: "room_post_per_minute", want: nil, warn: true},
		{name: "room ok", in: map[string]any{"room_post_per_minute": 6}, key: "room_post_per_minute", want: 6},
		{name: "consult low", in: map[string]any{"room_consult_budget_usd": 0.001}, key: "room_consult_budget_usd", want: nil, warn: true},
		{name: "consult ok", in: map[string]any{"room_consult_budget_usd": 0.5}, key: "room_consult_budget_usd", want: 0.5},
		{name: "retries over", in: map[string]any{"max_step_retries": 21}, key: "max_step_retries", want: nil, warn: true},
		{name: "empty string inherits", in: map[string]any{"review_policy": ""}, key: "review_policy", want: nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			logs := captureDBLogs(t)
			got := executionView(tt.in)
			if !reflect.DeepEqual(got[tt.key], tt.want) {
				t.Fatalf("%s = %#v, want %#v", tt.key, got[tt.key], tt.want)
			}
			if got["distill_lessons"] == nil {
				t.Fatal("distill_lessons is null")
			}
			warned := bytes.Contains(logs.Bytes(), []byte("ignoring project execution setting"))
			if warned != tt.warn {
				t.Fatalf("warning %v, log %s", warned, logs.String())
			}
			if tt.warn && !bytes.Contains(logs.Bytes(), []byte(tt.key)) {
				t.Fatalf("log %s, want field %s", logs.String(), tt.key)
			}
		})
	}
}

func TestProjectViewUsesExecutionRules(t *testing.T) {
	row := map[string]any{
		"id": "proj-x", "name": "X", "description": "", "work_dir": "", "background": "",
		"created_at": "t", "updated_at": "t",
		"task_count": int64(1), "active_count": int64(0), "schedule_count": int64(0),
		"supervise_interval_seconds": int32(math.MaxInt32),
		"task_budget_usd":            1.5,
		"workspace_links":            []string{".venv"},
	}
	got := projectView(row)
	if got["supervise_interval_seconds"] != nil {
		t.Fatalf("interval %#v", got["supervise_interval_seconds"])
	}
	if got["task_budget_usd"] != 1.5 {
		t.Fatalf("budget %#v", got["task_budget_usd"])
	}
	if !reflect.DeepEqual(got["workspace_links"], []any{".venv"}) {
		t.Fatalf("links %#v", got["workspace_links"])
	}
	if got["distill_lessons"] != "off" || got["project_id"] != "proj-x" || got["task_count"] != 1 {
		t.Fatalf("view %#v", got)
	}
}

func captureDBLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}
