package db

import (
	"bytes"
	"context"
	"log/slog"
	"os"
	"strconv"
	"strings"
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

// TestLoadScheduleMinIntervalMatchesPython is scheduler_settings.load.
// A stored whole number from 1 wins, including over a variable that does
// not parse. Anything else falls through to the variable, then to 300.
// Gated fires use min(60, that value), so 30 is 30/30 and 120 is 120/60.
func TestLoadScheduleMinIntervalMatchesPython(t *testing.T) {
	dsn := withDisposableDB(t, "loopgo_g1rfix_load")
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

	prev := scheduleMinIntervalSeconds
	warned := scheduleMinIntervalEnvWarned
	looper, looperSet := os.LookupEnv("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS")
	loopx, loopxSet := os.LookupEnv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS")
	var logs bytes.Buffer
	logger := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	t.Cleanup(func() {
		slog.SetDefault(logger)
		scheduleMinIntervalSeconds = prev
		scheduleMinIntervalEnvWarned = warned
		restoreEnv("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", looper, looperSet)
		restoreEnv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", loopx, loopxSet)
	})

	str := func(s string) *string { return &s }
	tests := []struct {
		name      string
		stored    *string
		looperSet bool
		looper    string
		loopxSet  bool
		loopx     string
		wantPlain int
		wantGated int
		wantErr   bool
		stay      int
		badText   string
	}{
		{name: "stored 30", stored: str("30"), wantPlain: 30, wantGated: 30},
		{name: "stored beats env", stored: str("30"), looperSet: true, looper: "45", wantPlain: 30, wantGated: 30},
		{name: "stored beats bad env", stored: str("30"), looperSet: true, looper: "soon", wantPlain: 30, wantGated: 30},
		{name: "stored spaces", stored: str(" 30 "), wantPlain: 30, wantGated: 30},
		{name: "stored plus", stored: str("+30"), wantPlain: 30, wantGated: 30},
		{name: "stored underscores", stored: str("1_20"), wantPlain: 120, wantGated: 60},
		{name: "no row uses built-in", wantPlain: 300, wantGated: 60},
		{name: "no row uses env", looperSet: true, looper: "45", wantPlain: 45, wantGated: 45},
		{name: "invalid stored uses env", stored: str("nope"), looperSet: true, looper: "40", wantPlain: 40, wantGated: 40, badText: "nope"},
		{name: "zero stored uses built-in", stored: str("0"), wantPlain: 300, wantGated: 60},
		{name: "fraction stored ignored", stored: str("60.0"), wantPlain: 300, wantGated: 60, badText: "60.0"},
		{name: "negative stored ignored", stored: str("-5"), wantPlain: 300, wantGated: 60, badText: "-5"},
		{name: "bad env keeps previous", looperSet: true, looper: "soon", wantErr: true, stay: 17},
		{name: "invalid stored and bad env", stored: str("nope"), looperSet: true, looper: "soon", wantErr: true, stay: 17, badText: "nope"},
		{name: "empty env ignores legacy", looperSet: true, looper: "", loopxSet: true, loopx: "30", wantPlain: 300, wantGated: 60},
		{name: "legacy only", loopxSet: true, loopx: "30", wantPlain: 30, wantGated: 30},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			setOrUnset("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", tt.looper, tt.looperSet)
			setOrUnset("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", tt.loopx, tt.loopxSet)
			if _, err := sqlDB.ExecContext(ctx, `DELETE FROM system_settings WHERE key = 'scheduler.min_interval_seconds'`); err != nil {
				t.Fatal(err)
			}
			if tt.stored != nil {
				if _, err := sqlDB.ExecContext(ctx, `INSERT INTO system_settings (key, value) VALUES ('scheduler.min_interval_seconds', $1)`, *tt.stored); err != nil {
					t.Fatal(err)
				}
			}
			if tt.wantErr {
				scheduleMinIntervalSeconds = tt.stay
			}
			logs.Reset()
			err := LoadScheduleMinInterval(ctx, sqlDB)
			if tt.wantErr {
				if err == nil {
					t.Fatal("expected an error")
				}
				if scheduleMinInterval(false) != tt.stay {
					t.Fatalf("interval changed to %d", scheduleMinInterval(false))
				}
				if tt.badText != "" && strings.Contains(logs.String(), tt.badText) {
					t.Fatalf("log %q", logs.String())
				}
				return
			}
			if tt.name == "stored beats bad env" && strings.Contains(logs.String(), "soon") {
				t.Fatalf("log %q", logs.String())
			}
			if err != nil {
				t.Fatal(err)
			}
			if scheduleMinInterval(false) != tt.wantPlain || scheduleMinInterval(true) != tt.wantGated {
				t.Fatalf("limits %d/%d, want %d/%d", scheduleMinInterval(false), scheduleMinInterval(true), tt.wantPlain, tt.wantGated)
			}
			if tt.badText != "" {
				logged := logs.String()
				if !strings.Contains(logged, "ignoring it") || strings.Contains(logged, tt.badText) {
					t.Fatalf("log %q", logged)
				}
			}
		})
	}

	t.Run("env warning once", func(t *testing.T) {
		setOrUnset("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", "123", true)
		setOrUnset("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", "", false)
		if _, err := sqlDB.ExecContext(ctx, `DELETE FROM system_settings WHERE key = 'scheduler.min_interval_seconds'`); err != nil {
			t.Fatal(err)
		}
		scheduleMinIntervalEnvWarned = false
		logs.Reset()
		if err := LoadScheduleMinInterval(ctx, sqlDB); err != nil {
			t.Fatal(err)
		}
		if err := LoadScheduleMinInterval(ctx, sqlDB); err != nil {
			t.Fatal(err)
		}
		logged := logs.String()
		if strings.Count(logged, "will be ignored in the next version") != 1 || strings.Contains(logged, "123") ||
			!strings.Contains(logged, "LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS is deprecated") {
			t.Fatalf("log %q", logged)
		}
		if scheduleMinInterval(false) != 123 || scheduleMinInterval(true) != 60 {
			t.Fatalf("limits %d/%d, want 123/60", scheduleMinInterval(false), scheduleMinInterval(true))
		}
	})

	t.Run("legacy name in the warning", func(t *testing.T) {
		setOrUnset("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", "", false)
		setOrUnset("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", "77", true)
		if _, err := sqlDB.ExecContext(ctx, `DELETE FROM system_settings WHERE key = 'scheduler.min_interval_seconds'`); err != nil {
			t.Fatal(err)
		}
		scheduleMinIntervalEnvWarned = false
		logs.Reset()
		if err := LoadScheduleMinInterval(ctx, sqlDB); err != nil {
			t.Fatal(err)
		}
		logged := logs.String()
		if !strings.Contains(logged, "LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS is deprecated") || strings.Contains(logged, "77") {
			t.Fatalf("log %q", logged)
		}
		if scheduleMinInterval(false) != 77 || scheduleMinInterval(true) != 60 {
			t.Fatalf("limits %d/%d, want 77/60", scheduleMinInterval(false), scheduleMinInterval(true))
		}
	})
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

// TestScheduleTimeoutMatchesPython is schedules._timeout_view and limits().
// inherit is the string "default" plus the system default a run would get.
// A stored number and an explicit none stay that number and null. Project
// limits include run_timeout_default_seconds, null when nothing valid is stored.
func TestScheduleTimeoutMatchesPython(t *testing.T) {
	dsn := withDisposableDB(t, "loopgo_g1r_timeout")
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
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO projects (id, name) VALUES ('proj-1', 'P')`); err != nil {
		t.Fatal(err)
	}
	if _, err := sqlDB.ExecContext(ctx, `
		INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, run_timeout_mode, run_timeout_seconds)
		VALUES
		('sched-inherit', 'proj-1', 'Inherit', '0 9 * * *', 'UTC', 'follow', 'inherit', NULL),
		('sched-seconds', 'proj-1', 'Seconds', '0 10 * * *', 'UTC', 'own', 'seconds', 120),
		('sched-none', 'proj-1', 'None', '0 11 * * *', 'UTC', 'open', 'none', NULL)`); err != nil {
		t.Fatal(err)
	}
	store := NewStore(sqlDB)

	assertTimeout := func(id string, wantShown, wantEffective any) {
		t.Helper()
		body, err := store.GetSchedule(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		schedule, _ := body["schedule"].(map[string]any)
		if _, ok := schedule["run_timeout_effective_seconds"]; !ok {
			t.Fatalf("%s omits run_timeout_effective_seconds", id)
		}
		if !timeoutEqual(schedule["run_timeout_seconds"], wantShown) || !timeoutEqual(schedule["run_timeout_effective_seconds"], wantEffective) {
			t.Fatalf("%s timeout %v / %v, want %v / %v", id, schedule["run_timeout_seconds"], schedule["run_timeout_effective_seconds"], wantShown, wantEffective)
		}
	}
	assertDefault := func(want any) {
		t.Helper()
		body, err := store.ListProjectSchedules(ctx, "proj-1")
		if err != nil {
			t.Fatal(err)
		}
		limits, _ := body["limits"].(map[string]any)
		got, ok := limits["run_timeout_default_seconds"]
		if !ok {
			t.Fatal("limits omit run_timeout_default_seconds")
		}
		if !timeoutEqual(got, want) {
			t.Fatalf("run_timeout_default_seconds %v, want %v", got, want)
		}
	}

	assertTimeout("sched-inherit", "default", nil)
	assertTimeout("sched-seconds", 120, 120)
	assertTimeout("sched-none", nil, nil)
	assertDefault(nil)

	put := `INSERT INTO system_settings (key, value) VALUES ('scheduler.run_timeout_seconds', $1)
		ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`
	for _, tc := range []struct {
		value    string
		wantSeen any
	}{
		{value: "900", wantSeen: 900},
		{value: " 900 ", wantSeen: 900},
		{value: "2_592_000", wantSeen: 2592000},
		{value: "nope", wantSeen: nil},
		{value: "0", wantSeen: nil},
		{value: "2592001", wantSeen: nil},
		{value: "", wantSeen: nil},
	} {
		if _, err := sqlDB.ExecContext(ctx, put, tc.value); err != nil {
			t.Fatal(err)
		}
		assertTimeout("sched-inherit", "default", tc.wantSeen)
		assertTimeout("sched-seconds", 120, 120)
		assertTimeout("sched-none", nil, nil)
		assertDefault(tc.wantSeen)
	}
}

func timeoutEqual(got, want any) bool {
	if want == nil {
		return got == nil
	}
	if text, ok := want.(string); ok {
		return got == text
	}
	return timeoutNumber(got) == want.(int)
}

func timeoutNumber(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int32:
		return int(n)
	case int64:
		return int(n)
	case float64:
		return int(n)
	default:
		return -1
	}
}
