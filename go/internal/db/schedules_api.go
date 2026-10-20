package db

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

// ErrScheduleNotFound is a schedule id that is not in the database.
var ErrScheduleNotFound = errors.New("schedule not found")

// StatusError is a request the API refuses with an HTTP status.
// The text is the JSON error, the way projects.RequestError is.
type StatusError struct {
	Code int
	Text string
}

func (e *StatusError) Error() string { return e.Text }

// ScheduleRunsQuery is list_runs' limit, before, and whether routine skips
// are left out. Limit is already checked to be 1..200.
type ScheduleRunsQuery struct {
	Limit     int
	Before    string
	HideSkips bool
}

// ProjectWorkspace is the project fields check_workspace reads.
// Found is false when the id is not a row. TaskWorkspaces is the stored
// mode, or empty when the column is null (the task runs in place).
// NotesOff is true only when operator_notes is the string off.
type ProjectWorkspace struct {
	Found          bool
	WorkDir        string
	TaskWorkspaces string
	NotesOff       bool
}

// ActiveWorkspace is one task db.tasks.active_workspaces returns, with the
// name task_name would give it. Mode is in_place when workspace_mode is null.
type ActiveWorkspace struct {
	ID        string
	Name      string
	Status    string
	Mode      string
	Workspace string
}

func (s *Store) ProjectWorkspace(ctx context.Context, id string) (ProjectWorkspace, error) {
	row, err := s.one(ctx, "project workspace",
		`SELECT work_dir, task_workspaces, operator_notes FROM projects WHERE id = $1`, id)
	if err != nil || row == nil {
		return ProjectWorkspace{}, err
	}
	mode := asString(row["task_workspaces"])
	if mode != "in_place" && mode != "worktree" {
		mode = ""
	}
	return ProjectWorkspace{
		Found:          true,
		WorkDir:        asString(row["work_dir"]),
		TaskWorkspaces: mode,
		NotesOff:       asString(row["operator_notes"]) == "off",
	}, nil
}

func (s *Store) ActiveWorkspaces(ctx context.Context) ([]ActiveWorkspace, error) {
	rows, err := s.query(ctx, "active workspaces", `
		SELECT id, title, goal_id, requirement, status, workspace_mode,
		       COALESCE(NULLIF(workspace_dir, ''), work_dir) AS workspace
		FROM tasks
		WHERE status = ANY($1::text[])
		ORDER BY created_at`, activeStatuses)
	if err != nil {
		return nil, err
	}
	out := make([]ActiveWorkspace, 0, len(rows))
	for _, row := range rows {
		mode := asString(row["workspace_mode"])
		if mode == "" {
			mode = "in_place"
		}
		out = append(out, ActiveWorkspace{
			ID:        asString(row["id"]),
			Name:      taskName(asString(row["title"]), asString(row["goal_id"]), asString(row["requirement"]), asString(row["id"])),
			Status:    asString(row["status"]),
			Mode:      mode,
			Workspace: asString(row["workspace"]),
		})
	}
	return out, nil
}

func (s *Store) ListProjectSchedules(ctx context.Context, projectID string) (map[string]any, error) {
	ok, err := s.ProjectExists(ctx, projectID)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, ErrProjectNotFound
	}
	views, err := s.scheduleViews(ctx, projectID)
	if err != nil {
		return nil, err
	}
	limits, err := s.scheduleLimits(ctx)
	if err != nil {
		return nil, err
	}
	return map[string]any{"schedules": views, "limits": limits}, nil
}

func (s *Store) ListSchedules(ctx context.Context) (map[string]any, error) {
	views, err := s.scheduleViews(ctx, "")
	if err != nil {
		return nil, err
	}
	return map[string]any{"schedules": views}, nil
}

func (s *Store) GetSchedule(ctx context.Context, id string) (map[string]any, error) {
	one, err := s.scheduleByID(ctx, id)
	if err != nil {
		return nil, err
	}
	if one == nil {
		return nil, ErrScheduleNotFound
	}
	return map[string]any{"schedule": one}, nil
}

const scheduleByIDSQL = `
SELECT s.*, p.name AS project_name
FROM schedules s JOIN projects p ON p.id = s.project_id
WHERE s.id = $1`

func (s *Store) scheduleByID(ctx context.Context, id string) (map[string]any, error) {
	rows, err := s.query(ctx, "schedule", scheduleByIDSQL, id)
	if err != nil || len(rows) == 0 {
		return nil, err
	}
	views, err := s.attachScheduleViews(ctx, rows)
	if err != nil || len(views) == 0 {
		return nil, err
	}
	view, _ := views[0].(map[string]any)
	return view, nil
}

const scheduleListSQL = `
SELECT s.*, p.name AS project_name
FROM schedules s JOIN projects p ON p.id = s.project_id
WHERE ($1::text IS NULL OR s.project_id = $1)`

func (s *Store) scheduleViews(ctx context.Context, projectID string) ([]any, error) {
	var project any
	if projectID != "" {
		project = projectID
	}
	rows, err := s.query(ctx, "schedules", scheduleListSQL+`
		ORDER BY lower(p.name), p.id, lower(s.name), s.id`, project)
	if err != nil {
		return nil, err
	}
	return s.attachScheduleViews(ctx, rows)
}

func (s *Store) attachScheduleViews(ctx context.Context, rows []map[string]any) ([]any, error) {
	ids := make([]string, len(rows))
	for i, row := range rows {
		ids[i] = asString(row["id"])
	}
	overrides, err := s.scheduleOverrides(ctx, ids)
	if err != nil {
		return nil, err
	}
	limits, err := s.scheduleTaskLimits(ctx, ids)
	if err != nil {
		return nil, err
	}
	latest, err := s.latestRuns(ctx, ids)
	if err != nil {
		return nil, err
	}
	spent, err := s.spentSinceEach(ctx, ids, scheduleMidnight(s.now()))
	if err != nil {
		return nil, err
	}
	running := []string{}
	for _, row := range latest {
		if asString(row["task_id"]) != "" && row["cost_usd"] == nil {
			running = append(running, asString(row["task_id"]))
		}
	}
	costs, err := s.runningTaskCosts(ctx, running)
	if err != nil {
		return nil, err
	}
	defaultTimeout, err := s.defaultRunTimeout(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]any, 0, len(rows))
	for _, row := range rows {
		id := asString(row["id"])
		var last any
		if run, ok := latest[id]; ok {
			var cost any
			if c, ok := costs[asString(run["task_id"])]; ok && run["cost_usd"] == nil {
				cost = c
			}
			last = runView(run, cost, false)
		}
		out = append(out, scheduleItemView(row, overrides[id], limits[id], last, spent[id], defaultTimeout))
	}
	return out, nil
}

func (s *Store) scheduleOverrides(ctx context.Context, ids []string) (map[string]map[string]any, error) {
	out := map[string]map[string]any{}
	for _, id := range ids {
		out[id] = map[string]any{}
	}
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := s.query(ctx, "schedule overrides", `
		SELECT schedule_id, role, profile_id FROM schedule_profile_overrides
		WHERE schedule_id = ANY($1::text[]) ORDER BY role`, ids)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		out[asString(row["schedule_id"])][asString(row["role"])] = asString(row["profile_id"])
	}
	return out, nil
}

func (s *Store) scheduleTaskLimits(ctx context.Context, ids []string) (map[string]map[string]any, error) {
	out := map[string]map[string]any{}
	for _, id := range ids {
		out[id] = map[string]any{}
	}
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := s.query(ctx, "schedule task limits", `
		SELECT schedule_id, role, max_concurrency FROM schedule_task_limits
		WHERE schedule_id = ANY($1::text[]) ORDER BY role`, ids)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		out[asString(row["schedule_id"])][asString(row["role"])] = asInt(row["max_concurrency"])
	}
	return out, nil
}

func (s *Store) latestRuns(ctx context.Context, ids []string) (map[string]map[string]any, error) {
	out := map[string]map[string]any{}
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := s.query(ctx, "latest runs", `
		SELECT DISTINCT ON (r.schedule_id) r.*, t.status AS task_status
		FROM schedule_runs r LEFT JOIN tasks t ON t.id = r.task_id
		WHERE r.schedule_id = ANY($1::text[])
		ORDER BY r.schedule_id, r.created_at DESC, r.id DESC`, ids)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		out[asString(row["schedule_id"])] = row
	}
	return out, nil
}

func (s *Store) spentSinceEach(ctx context.Context, ids []string, since time.Time) (map[string]float64, error) {
	out := map[string]float64{}
	for _, id := range ids {
		out[id] = 0
	}
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := s.query(ctx, "schedule spend", `
		SELECT r.schedule_id, sum(COALESCE(r.cost_usd::numeric,
		    (SELECT sum(g.cost_usd) FROM (
		        SELECT round(sum(l.cost_usd::numeric), 6) AS cost_usd
		        FROM llm_records l WHERE l.task_id = r.task_id
		          AND CASE WHEN l.agent_id LIKE 'planner-%' THEN 'planner'
		                   WHEN l.agent_id LIKE 'worker-%' THEN 'worker'
		                   WHEN l.agent_id LIKE 'reviewer-%' THEN 'reviewer' END IS NOT NULL
		        GROUP BY CASE WHEN l.agent_id LIKE 'planner-%' THEN 'planner'
		                      WHEN l.agent_id LIKE 'worker-%' THEN 'worker'
		                      WHEN l.agent_id LIKE 'reviewer-%' THEN 'reviewer' END, l.model
		    ) g)))::float8 AS spent
		FROM schedule_runs r
		WHERE r.schedule_id = ANY($1::text[]) AND r.fired_at >= $2
		GROUP BY r.schedule_id`, ids, since)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		out[asString(row["schedule_id"])] = round6(mustFloat(row["spent"]))
	}
	return out, nil
}

func (s *Store) runningTaskCosts(ctx context.Context, ids []string) (map[string]float64, error) {
	out := make(map[string]float64, len(ids))
	for _, id := range ids {
		// Every requested task has a cost. Python's task_costs starts each at
		// Decimal(0), so a run whose task has no priced rows is 0.0, not null.
		out[id] = 0
	}
	if len(ids) == 0 {
		return out, nil
	}
	// round(..., 6) per role and model, then add those numerics. Casting each
	// subtotal to float8 before the sum turns 0.1 and 0.2 into
	// 0.30000000000000004. The float is the final sum only.
	rows, err := s.query(ctx, "running task costs", `
		SELECT task_id, sum(cost_usd) AS cost_usd FROM (
		    SELECT l.task_id, round(sum(l.cost_usd::numeric), 6) AS cost_usd
		    FROM llm_records l
		    WHERE l.task_id = ANY($1::text[])
		      AND CASE WHEN l.agent_id LIKE 'planner-%' THEN 'planner'
		               WHEN l.agent_id LIKE 'worker-%' THEN 'worker'
		               WHEN l.agent_id LIKE 'reviewer-%' THEN 'reviewer' END IS NOT NULL
		    GROUP BY l.task_id,
		             CASE WHEN l.agent_id LIKE 'planner-%' THEN 'planner'
		                  WHEN l.agent_id LIKE 'worker-%' THEN 'worker'
		                  WHEN l.agent_id LIKE 'reviewer-%' THEN 'reviewer' END,
		             l.model
		) g GROUP BY task_id`, ids)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		micro, ok := moneyMicros(row["cost_usd"])
		if !ok {
			continue
		}
		out[asString(row["task_id"])] = microsToFloat(micro)
	}
	return out, nil
}

func (s *Store) ListScheduleRuns(ctx context.Context, id string, q ScheduleRunsQuery) (map[string]any, error) {
	row, err := s.one(ctx, "schedule", `SELECT id FROM schedules WHERE id = $1`, id)
	if err != nil {
		return nil, err
	}
	if row == nil {
		return nil, ErrScheduleNotFound
	}
	if q.Before != "" {
		run, err := s.one(ctx, "schedule run", `SELECT schedule_id FROM schedule_runs WHERE id = $1`, q.Before)
		if err != nil {
			return nil, err
		}
		if run == nil || asString(run["schedule_id"]) != id {
			return nil, &StatusError{Code: 400, Text: "before must be a run of schedule " + id}
		}
	}
	without := []string{}
	if q.HideSkips {
		without = []string{"skipped_gate", "skipped_overlap"}
	}
	var before any
	if q.Before != "" {
		before = q.Before
	}
	rows, err := s.query(ctx, "schedule runs", `
		SELECT r.*, t.status AS task_status, t.title AS task_title
		FROM schedule_runs r LEFT JOIN tasks t ON t.id = r.task_id
		WHERE r.schedule_id = $1 AND NOT (r.status = ANY($3::text[]))
		  AND ($4::text IS NULL OR (r.created_at, r.id) < (
		      SELECT b.created_at, b.id FROM schedule_runs b
		      WHERE b.id = $4 AND b.schedule_id = $1))
		ORDER BY r.created_at DESC, r.id DESC
		LIMIT $2`, id, q.Limit+1, without, before)
	if err != nil {
		return nil, err
	}
	page := rows
	var next any
	if len(rows) > q.Limit {
		page = rows[:q.Limit]
		next = asString(page[len(page)-1]["id"])
	}
	running := []string{}
	for _, run := range page {
		if asString(run["task_id"]) != "" && run["cost_usd"] == nil {
			running = append(running, asString(run["task_id"]))
		}
	}
	costs, err := s.runningTaskCosts(ctx, running)
	if err != nil {
		return nil, err
	}
	views := make([]any, 0, len(page))
	for _, run := range page {
		var cost any
		if c, ok := costs[asString(run["task_id"])]; ok && run["cost_usd"] == nil {
			cost = c
		}
		views = append(views, runView(run, cost, true))
	}
	counts, err := s.runCounts(ctx, id)
	if err != nil {
		return nil, err
	}
	return map[string]any{"runs": views, "next_before": next, "routine_skips": counts}, nil
}

func (s *Store) runCounts(ctx context.Context, id string) (map[string]any, error) {
	rows, err := s.query(ctx, "run counts", `
		SELECT status, sum(firings)::bigint AS n FROM schedule_runs
		WHERE schedule_id = $1 AND status = ANY($2::text[])
		GROUP BY status`, id, []string{"skipped_gate", "skipped_overlap"})
	if err != nil {
		return nil, err
	}
	counts := map[string]int{"skipped_gate": 0, "skipped_overlap": 0}
	for _, row := range rows {
		counts[asString(row["status"])] = asInt(row["n"])
	}
	return map[string]any{
		"skipped_gate":    counts["skipped_gate"],
		"skipped_overlap": counts["skipped_overlap"],
	}, nil
}

func (s *Store) ListScheduleNotifications(ctx context.Context, id string, limit int) (map[string]any, error) {
	row, err := s.one(ctx, "schedule", `SELECT id FROM schedules WHERE id = $1`, id)
	if err != nil {
		return nil, err
	}
	if row == nil {
		return nil, ErrScheduleNotFound
	}
	rows, err := s.query(ctx, "schedule notifications", `
		SELECT * FROM schedule_notifications
		WHERE schedule_id = $1
		ORDER BY created_at DESC, id DESC
		LIMIT $2`, id, limit)
	if err != nil {
		return nil, err
	}
	notes := make([]any, 0, len(rows))
	for _, row := range rows {
		notes = append(notes, map[string]any{
			"notification_id": row["id"],
			"kind":            row["kind"],
			"run_id":          row["run_id"],
			"channel":         row["channel"],
			"chat":            row["chat"],
			"text":            row["text"],
			"status":          row["status"],
			"error":           row["error"],
			"created_at":      row["created_at"],
		})
	}
	return map[string]any{"notifications": notes}, nil
}

func (s *Store) scheduleLimits(ctx context.Context) (map[string]any, error) {
	plain := scheduleMinInterval(false)
	gated := scheduleMinInterval(true)
	rows, err := s.query(ctx, "channel configs", channelConfigSQL+` ORDER BY c.channel_id`)
	if err != nil {
		return nil, err
	}
	targets, err := notifyTargetViews(rows)
	if err != nil {
		return nil, fmt.Errorf("schedule limits: %w", err)
	}
	defaultTimeout, err := s.defaultRunTimeout(ctx)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"name_max_chars":               80,
		"requirement_max_chars":        27000,
		"min_interval_seconds":         plain,
		"min_interval_gated_seconds":   gated,
		"default_timezone":             localZoneName(),
		"run_timeout_max_seconds":      runTimeoutMaxSeconds,
		"run_timeout_default_seconds":  defaultTimeout,
		"roles":                        []any{"planner", "worker", "reviewer"},
		"gate_command_max_chars":       2000,
		"gate_timeout_default_seconds": 60,
		"gate_timeout_max_seconds":     600,
		"max_parallel_runs_limit":      10,
		"concurrency_max":              64,
		"notify_targets":               targets,
	}, nil
}

// scheduleMinIntervalSeconds is the minimum interval scheduler_settings.load
// stored at startup. A request does not read the database or the environment
// again. Tests and the parity harness leave it at the default, which is what
// the Python handlers return when the server has not called load.
const defaultMinIntervalSeconds = 300

const (
	scheduleMinIntervalEnvName   = "LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS"
	scheduleMinIntervalEnvLegacy = "LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS"
)

var scheduleMinIntervalSeconds = defaultMinIntervalSeconds

// scheduleMinIntervalEnvWarned is scheduler_settings._warned_env. The
// deprecation warning is once per process and does not include the value.
var scheduleMinIntervalEnvWarned bool

// LoadScheduleMinInterval applies scheduler_settings.load after the schema
// exists. A stored whole number of seconds from 1 wins, including over a
// variable that would not parse. A stored value that is not is logged
// without its text and ignored. Otherwise the deprecated variable applies.
// An invalid variable is an error and the previous interval stays. An empty
// or unset variable is the built-in 300.
func LoadScheduleMinInterval(ctx context.Context, sqlDB *sql.DB) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("load scheduler min interval: %w", hide(err))
	}
	row, err := NewStore(sqlDB).one(ctx, "scheduler min interval",
		`SELECT value FROM system_settings WHERE key = $1`, schedulerMinIntervalKey)
	if err != nil {
		return fmt.Errorf("load scheduler min interval: %w", hide(err))
	}
	if row != nil {
		if n, ok := parseScheduleMinInterval(asString(row["value"])); ok {
			scheduleMinIntervalSeconds = n
			return nil
		}
		slog.Warn("the stored scheduler.min_interval_seconds is not a whole number of seconds from 1; ignoring it",
			"key", schedulerMinIntervalKey)
	}
	return ConfigureScheduleMinInterval()
}

// ConfigureScheduleMinInterval reads LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS
// the way scheduler_settings._env_min_interval does when no stored setting
// wins. env.get falls back to LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS when the
// LOOPER name is unset. A value set to empty does not fall back. An empty
// result keeps 300. A value that is not a whole number of seconds from 1 is
// an error and the previous value stays.
func ConfigureScheduleMinInterval() error {
	raw, name, ok := lookupScheduleMinIntervalEnv()
	if !ok || strings.TrimSpace(raw) == "" {
		scheduleMinIntervalSeconds = defaultMinIntervalSeconds
		return nil
	}
	// int() accepts underscores between digits and Unicode decimal digits.
	// A value that does not fit in int cannot be stored, so startup fails
	// rather than keeping a truncated interval.
	value, good := parseScheduleMinInterval(raw)
	if !good {
		return fmt.Errorf("%s must be a whole number of seconds from 1; got %q", scheduleMinIntervalEnvName, raw)
	}
	noteScheduleMinIntervalEnv(name)
	scheduleMinIntervalSeconds = value
	return nil
}

func lookupScheduleMinIntervalEnv() (raw, name string, ok bool) {
	raw, ok = os.LookupEnv(scheduleMinIntervalEnvName)
	if ok {
		return raw, scheduleMinIntervalEnvName, true
	}
	legacy, legacyOK := os.LookupEnv(scheduleMinIntervalEnvLegacy)
	if !legacyOK {
		return "", "", false
	}
	slog.Warn("deprecated environment variable",
		"name", scheduleMinIntervalEnvLegacy,
		"rename", scheduleMinIntervalEnvName)
	return legacy, scheduleMinIntervalEnvLegacy, true
}

func noteScheduleMinIntervalEnv(name string) {
	if scheduleMinIntervalEnvWarned {
		return
	}
	scheduleMinIntervalEnvWarned = true
	slog.Warn(name + " is deprecated and will be ignored in the next version: set the minimum interval in Settings → Scheduler instead")
}

func parseScheduleMinInterval(raw string) (int, bool) {
	value, overflow, ok := parsePythonInt(raw)
	if !ok || overflow != 0 || value < 1 || int64(int(value)) != value {
		return 0, false
	}
	return int(value), true
}

func scheduleMinInterval(gated bool) int {
	base := scheduleMinIntervalSeconds
	if gated && base > 60 {
		return 60
	}
	return base
}

// now is the schedule clock. Store.Now nil is the wall clock, which is what
// schedules.local_midnight uses. The parity harness sets Store.Now.
func (s *Store) now() time.Time {
	if s != nil && s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

// scheduleMidnight is schedules.local_midnight: the start of today in the
// server zone. It follows the schedule clock, not the overview clock.
func scheduleMidnight(now time.Time) time.Time {
	loc := time.Local
	if name := localZoneName(); name != "" {
		if loaded, err := time.LoadLocation(name); err == nil {
			loc = loaded
		}
	}
	local := now.In(loc)
	y, m, d := local.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, loc)
}

// runTimeoutMaxSeconds is scheduler_settings.RUN_TIMEOUT_MAX_SECONDS.
const runTimeoutMaxSeconds = 30 * 86400

// defaultRunTimeout is scheduler_settings.default_run_timeout. A missing row
// is no limit. A stored value that is not a whole number of seconds from 1
// to the maximum is logged and treated as no limit. The text is not logged.
func (s *Store) defaultRunTimeout(ctx context.Context) (any, error) {
	row, err := s.one(ctx, "schedule default timeout",
		`SELECT value FROM system_settings WHERE key = $1`, schedulerRunTimeoutKey)
	if err != nil || row == nil {
		return nil, err
	}
	return storedRunTimeout(row["value"]), nil
}

func storedRunTimeout(raw any) any {
	n, overflow, ok := parsePythonInt(asString(raw))
	if !ok || overflow != 0 || n < 1 || n > runTimeoutMaxSeconds || int64(int(n)) != n {
		slog.Warn("the stored scheduler.run_timeout_seconds is not a whole number of seconds from 1 to the maximum; using no limit",
			"key", schedulerRunTimeoutKey, "maximum", runTimeoutMaxSeconds)
		return nil
	}
	return int(n)
}

// scheduleTimeoutView is schedules._timeout_view. "inherit" is the request
// value "default" plus the system default a run that starts now would get.
// "none" is no limit. Anything else is the stored number for both fields.
func scheduleTimeoutView(row map[string]any, defaultTimeout any) (shown, effective any) {
	switch asString(row["run_timeout_mode"]) {
	case "inherit":
		return "default", defaultTimeout
	case "none":
		return nil, nil
	default:
		seconds := row["run_timeout_seconds"]
		return seconds, seconds
	}
}

func scheduleItemView(row map[string]any, overrides, taskLimits map[string]any, last any, spent float64, defaultTimeout any) map[string]any {
	if overrides == nil {
		overrides = map[string]any{}
	}
	limits := map[string]any{"worker": nil, "reviewer": nil}
	for _, role := range []string{"worker", "reviewer"} {
		if taskLimits != nil {
			if value, ok := taskLimits[role]; ok {
				limits[role] = value
			}
		}
	}
	var target any
	if channel := asString(row["notify_channel"]); channel != "" {
		target = map[string]any{"channel": channel, "chat": row["notify_chat"]}
	}
	notify := asString(row["notify"])
	if notify == "" {
		notify = "failures"
	}
	timeout, effective := scheduleTimeoutView(row, defaultTimeout)
	return map[string]any{
		"schedule_id":                   row["id"],
		"project_id":                    row["project_id"],
		"project_name":                  row["project_name"],
		"name":                          row["name"],
		"cron":                          row["cron"],
		"timezone":                      row["timezone"],
		"description":                   describeCron(asString(row["cron"]), asString(row["timezone"])),
		"requirement":                   row["requirement"],
		"work_dir":                      row["work_dir"],
		"overlap":                       row["overlap"],
		"max_parallel_runs":             asInt(row["max_parallel_runs"]),
		"catch_up":                      row["catch_up"],
		"run_timeout_seconds":           timeout,
		"run_timeout_effective_seconds": effective,
		"run_budget_usd":                row["run_budget_usd"],
		"profile_overrides":             overrides,
		"task_limits":                   limits,
		"enabled":                       asBool(row["enabled"]),
		"next_fire_at":                  row["next_fire_at"],
		"last_fired_at":                 row["last_fired_at"],
		"last_success_at":               row["last_success_at"],
		"consecutive_failures":          asInt(row["consecutive_failures"]),
		"created_via":                   row["created_via"],
		"created_at":                    row["created_at"],
		"updated_at":                    row["updated_at"],
		"gate_command":                  row["gate_command"],
		"gate_timeout_seconds":          row["gate_timeout_seconds"],
		"daily_budget_usd":              row["daily_budget_usd"],
		"notify":                        notify,
		"notify_target":                 target,
		"spent_today_usd":               round6(spent),
		"alerts": map[string]any{
			"failures_since": row["failures_alert_at"],
			"stale_since":    row["stale_alert_at"],
		},
		"last_run": last,
	}
}

func runView(row map[string]any, cost any, withTitle bool) map[string]any {
	costUSD := row["cost_usd"]
	if costUSD == nil && cost != nil {
		costUSD = cost
	}
	var title any
	if withTitle {
		title = row["task_title"]
	}
	firings := asInt(row["firings"])
	if firings == 0 {
		firings = 1
	}
	return map[string]any{
		"run_id":        row["id"],
		"schedule_id":   row["schedule_id"],
		"scheduled_for": row["scheduled_for"],
		"fired_at":      row["fired_at"],
		"trigger":       row["trigger"],
		"missed":        asInt(row["missed"]),
		"status":        row["status"],
		"task_id":       row["task_id"],
		"task_status":   row["task_status"],
		"task_title":    title,
		"summary":       row["summary"],
		"cost_usd":      costUSD,
		"error":         row["error"],
		"finished_at":   row["finished_at"],
		"gate_output":   row["gate_output"],
		"carried_from":  row["carried_from"],
		"firings":       firings,
	}
}

func round6(v float64) float64 {
	text := strconv.FormatFloat(v, 'f', 6, 64)
	parsed, err := strconv.ParseFloat(text, 64)
	if err != nil {
		return v
	}
	return parsed
}
