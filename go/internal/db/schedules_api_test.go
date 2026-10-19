package db

import (
	"context"
	"os"
	"strconv"
	"testing"
	"time"
)

func TestScheduleRunCostsMatchPython(t *testing.T) {
	dsn := withDisposableDB(t, "loopgo_g1h_cost")
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
		`INSERT INTO schedules (id, project_id, name, cron, timezone, requirement)
		 VALUES ('sched-1', 'proj-1', 'Nightly', '0 9 * * *', 'UTC', 'check')`,
		`INSERT INTO tasks (id, requirement, project_id) VALUES
		 ('task-split', 'split', 'proj-1'),
		 ('task-empty', 'empty', 'proj-1')`,
		`INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status, task_id, created_at) VALUES
		 ('run-split', 'sched-1', '2020-05-03T09:00:00Z', 'scheduled', 'running', 'task-split', '2020-05-03T09:00:00Z'),
		 ('run-empty', 'sched-1', '2020-05-03T08:00:00Z', 'scheduled', 'running', 'task-empty', '2020-05-03T08:00:00Z')`,
		// Two groups. One group would hide a float sum of the rounded subtotals.
		// The manager row is not part of the task cost.
		`INSERT INTO llm_records (agent_id, task_id, status, cost_usd, model) VALUES
		 ('planner-split', 'task-split', 'completed', 0.1, 'plan-a'),
		 ('worker-split', 'task-split', 'completed', 0.2, 'work-b'),
		 ('manager', 'task-split', 'completed', 9, 'mgr')`,
	}
	for _, stmt := range statements {
		if _, err := sqlDB.ExecContext(ctx, stmt); err != nil {
			t.Fatal(err)
		}
	}
	got, err := NewStore(sqlDB).ListScheduleRuns(ctx, "sched-1", ScheduleRunsQuery{Limit: 50})
	if err != nil {
		t.Fatal(err)
	}
	runs, _ := got["runs"].([]any)
	byID := map[string]any{}
	for _, item := range runs {
		row := item.(map[string]any)
		byID[row["run_id"].(string)] = row["cost_usd"]
	}
	if byID["run-empty"] == nil {
		t.Fatalf("task with no cost rows is null: %#v", byID)
	}
	if byID["run-empty"] != 0.0 {
		t.Fatalf("empty cost %#v", byID["run-empty"])
	}
	split, ok := byID["run-split"].(float64)
	if !ok {
		t.Fatalf("split cost %#v", byID["run-split"])
	}
	// 0.1 and 0.2 added as float64 print as 0.30000000000000004.
	if text := strconv.FormatFloat(split, 'f', -1, 64); text != "0.3" {
		t.Fatalf("split cost %s", text)
	}
}

func TestLegacyScheduleMinInterval(t *testing.T) {
	prev := scheduleMinIntervalSeconds
	t.Cleanup(func() { scheduleMinIntervalSeconds = prev })
	looper, looperSet := os.LookupEnv("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS")
	loopx, loopxSet := os.LookupEnv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS")
	t.Cleanup(func() {
		restoreEnv("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", looper, looperSet)
		restoreEnv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", loopx, loopxSet)
	})

	tests := []struct {
		name      string
		looperSet bool
		looper    string
		loopxSet  bool
		loopx     string
		wantPlain int
		wantGated int
		wantErr   bool
	}{
		{name: "legacy only", loopxSet: true, loopx: "30", wantPlain: 30, wantGated: 30},
		{name: "current wins", looperSet: true, looper: "45", loopxSet: true, loopx: "30", wantPlain: 45, wantGated: 45},
		{name: "empty current ignores legacy", looperSet: true, looper: "", loopxSet: true, loopx: "30", wantPlain: 300, wantGated: 60},
		{name: "legacy invalid", loopxSet: true, loopx: "nope", wantErr: true},
		{name: "underscore digits", looperSet: true, looper: "3_0", wantPlain: 30, wantGated: 30},
		{name: "fullwidth digits", looperSet: true, looper: "３０", wantPlain: 30, wantGated: 30},
		{name: "below one", looperSet: true, looper: "0", wantErr: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			setOrUnset("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", tt.looper, tt.looperSet)
			setOrUnset("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", tt.loopx, tt.loopxSet)
			err := ConfigureScheduleMinInterval()
			if tt.wantErr {
				if err == nil {
					t.Fatal("expected an error")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if scheduleMinInterval(false) != tt.wantPlain || scheduleMinInterval(true) != tt.wantGated {
				t.Fatalf("limits %d/%d, want %d/%d", scheduleMinInterval(false), scheduleMinInterval(true), tt.wantPlain, tt.wantGated)
			}
		})
	}
}

func setOrUnset(key, value string, set bool) {
	if set {
		_ = os.Setenv(key, value)
		return
	}
	_ = os.Unsetenv(key)
}

func restoreEnv(key, value string, set bool) {
	setOrUnset(key, value, set)
}

func TestSpentTodayIncludesMidnightAndExcludesEarlier(t *testing.T) {
	dsn := withDisposableDB(t, "loopgo_g1h2_spend")
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
	noon := time.Date(2026, 6, 15, 12, 0, 0, 0, time.Local)
	midnight := scheduleMidnight(noon)
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO projects (id, name) VALUES ('proj-1', 'P')`); err != nil {
		t.Fatal(err)
	}
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO schedules (id, project_id, name, cron, timezone, requirement)
		VALUES ('sched-1', 'proj-1', 'Nightly', '0 9 * * *', 'UTC', 'check')`); err != nil {
		t.Fatal(err)
	}
	insert := `INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status, cost_usd, created_at)
		VALUES ($1, 'sched-1', $2, $2, 'scheduled', 'completed', $3, $2)`
	if _, err := sqlDB.ExecContext(ctx, insert, "run-midnight", midnight, 1.5); err != nil {
		t.Fatal(err)
	}
	if _, err := sqlDB.ExecContext(ctx, insert, "run-before", midnight.Add(-time.Microsecond), 4.0); err != nil {
		t.Fatal(err)
	}
	store := NewStore(sqlDB)
	store.Now = func() time.Time { return noon }
	body, err := store.GetSchedule(ctx, "sched-1")
	if err != nil {
		t.Fatal(err)
	}
	schedule, _ := body["schedule"].(map[string]any)
	spent, _ := schedule["spent_today_usd"].(float64)
	if spent != 1.5 {
		t.Fatalf("spent_today_usd %v, want 1.5", schedule["spent_today_usd"])
	}
}
