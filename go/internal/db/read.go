package db

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

// ErrNotFound is a task id that is not in the database.
var ErrNotFound = errors.New("task not found")

// TaskQuery is the Tasks page filter. Empty strings are unset.
// Statuses nil with Inside true is no group filter. Limit is the page size.
type TaskQuery struct {
	ProjectID  string
	Status     string
	ScheduleID string
	Before     string
	Statuses   []string
	Inside     bool
	Limit      int
}

// Spend is llm_records.spend_since: every agent's runs, the Manager included.
type Spend struct {
	Runs       int
	Running    int
	Unpriced   int
	Provider   float64
	Equivalent float64
}

// LogText is a database error safe to log: a server error's code and message,
// or ErrorText. The driver text and the connection string are left out.
func LogText(err error) string {
	if err == nil {
		return ""
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return ErrorText(err)
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code + " " + pgErr.Message
	}
	return ErrorText(err)
}

const taskFrom = `
SELECT t.*, p.name AS project_name, COALESCE(t.budget_usd, p.task_budget_usd) AS task_budget_usd,
       s.name AS schedule_name
FROM tasks t
JOIN projects p ON p.id = t.project_id
LEFT JOIN schedules s ON s.id = t.schedule_id`

const taskFilters = `
($1::text IS NULL OR t.project_id = $1) AND ($2::text IS NULL OR t.status = $2)
AND ($3::text[] IS NULL OR (t.status = ANY($3::text[])) = $4)
AND ($5::text IS NULL OR t.schedule_id = $5)`

// Copied from looper/db/llm_records.py. The Manager is grouped, then left out of the task total.
const costSQL = `
SELECT task_id,
       CASE WHEN agent_id = 'manager' THEN 'manager'
            ELSE CASE WHEN agent_id LIKE 'planner-%' THEN 'planner'
                      WHEN agent_id LIKE 'worker-%' THEN 'worker'
                      WHEN agent_id LIKE 'reviewer-%' THEN 'reviewer' END
       END AS role,
       model, count(*) AS runs,
       count(*) FILTER (WHERE status = 'running') AS running,
       count(*) FILTER (WHERE status <> 'running' AND cost_usd IS NULL) AS unpriced_runs,
       count(*) FILTER (WHERE status = $2) AS interrupted_runs,
       count(*) FILTER (WHERE cost_usd IS NOT NULL AND COALESCE(metadata->'profile'->'provider'->>'auth', metadata->'agent'->'provider'->>'auth', 'oauth') = 'oauth') AS equivalent_runs,
       count(*) FILTER (WHERE cost_usd IS NOT NULL AND cost_source = 'provider_price' AND NOT COALESCE(metadata->'profile'->'provider'->>'auth', metadata->'agent'->'provider'->>'auth', 'oauth') = 'oauth') AS provider_price_runs,
       COALESCE(sum(input_tokens), 0)::bigint AS input_tokens,
       COALESCE(sum(output_tokens), 0)::bigint AS output_tokens,
       COALESCE(sum(cache_read_tokens), 0)::bigint AS cache_read_tokens,
       COALESCE(sum(cache_creation_tokens), 0)::bigint AS cache_creation_tokens,
       round(sum(cost_usd::numeric), 6) AS cost_usd
FROM llm_records
WHERE task_id = ANY($1::text[])
  AND (CASE WHEN agent_id LIKE 'planner-%' THEN 'planner'
            WHEN agent_id LIKE 'worker-%' THEN 'worker'
            WHEN agent_id LIKE 'reviewer-%' THEN 'reviewer' END IS NOT NULL
       OR agent_id = 'manager')
GROUP BY 1, 2, 3`

const roleRunsSQL = `
SELECT CASE WHEN agent_id LIKE 'planner-%' THEN 'planner'
            WHEN agent_id LIKE 'worker-%' THEN 'worker'
            WHEN agent_id LIKE 'reviewer-%' THEN 'reviewer' END AS role,
       COALESCE(metadata->'prompt_layers'->'task_role'->>'id', '') AS task_role,
       metadata->>'step_id' AS step_id, metadata->>'outcome' AS outcome,
       count(*) AS runs,
       count(*) FILTER (WHERE status = 'running') AS running,
       COALESCE(sum(cost_usd), 0) AS cost_usd
FROM llm_records
WHERE task_id = $1 AND (agent_id LIKE 'worker-%' OR agent_id LIKE 'reviewer-%')
GROUP BY 1, 2, 3, 4`

const listRunsSQL = `
SELECT agent_id, task_id, status, created_at, metadata->>'step_id' AS step_id,
       metadata->'prompt_layers'->'task_role' AS task_role
FROM llm_records
WHERE task_id = ANY($1::text[]) AND (agent_id LIKE 'worker-%' OR agent_id LIKE 'reviewer-%')
ORDER BY created_at, id`

const spendSQL = `
SELECT count(*) AS runs,
       count(*) FILTER (WHERE r.status = 'running') AS running,
       count(*) FILTER (WHERE r.status <> 'running' AND r.cost_usd IS NULL) AS unpriced_runs,
       COALESCE(sum(r.cost_usd) FILTER (WHERE NOT COALESCE(r.metadata->'profile'->'provider'->>'auth', r.metadata->'agent'->'provider'->>'auth', 'oauth') = 'oauth'), 0) AS provider_priced_usd,
       COALESCE(sum(r.cost_usd) FILTER (WHERE COALESCE(r.metadata->'profile'->'provider'->>'auth', r.metadata->'agent'->'provider'->>'auth', 'oauth') = 'oauth'), 0) AS equivalent_usd
FROM llm_records r
LEFT JOIN tasks t ON t.id = r.task_id
WHERE ($1::timestamptz IS NULL OR r.created_at >= $1)
  AND ($2::timestamptz IS NULL OR r.created_at < $2)
  AND ($3::text IS NULL OR (t.project_id = $3 AND CASE WHEN r.agent_id LIKE 'planner-%' THEN 'planner'
            WHEN r.agent_id LIKE 'worker-%' THEN 'worker'
            WHEN r.agent_id LIKE 'reviewer-%' THEN 'reviewer' END IS NOT NULL))`

var (
	openScheduleStatuses = []string{"queued", "gating", "running"}
	skippedScheduleRuns  = []string{"skipped_gate", "skipped_overlap", "skipped_missed", "skipped_budget"}
)

// StatusTaskCounts is the status payload's task counts.
// running is in progress plus waiting for the user. A read error is returned.
func (s *Store) StatusTaskCounts(ctx context.Context) (total, running, completed int, err error) {
	rows, err := s.query(ctx, "status counts", `
		SELECT p.id AS project_id, p.name AS project_name, t.status, count(t.id) AS count
		FROM projects p FULL JOIN tasks t ON t.project_id = p.id
		GROUP BY p.id, p.name, t.status`)
	if err != nil {
		return 0, 0, 0, err
	}
	counts := groupCounts(rows)
	return asInt(counts["total"]), asInt(counts[groupInProgress]) + asInt(counts[groupNeedsYou]), asInt(counts[groupCompleted]), nil
}

// Overview is GET /api/overview with no running agents.
// since is the start of today; the caller chooses the clock.
func (s *Store) Overview(ctx context.Context, since time.Time) (map[string]any, error) {
	rows, err := s.query(ctx, "overview counts", `
		SELECT p.id AS project_id, p.name AS project_name, t.status, count(t.id) AS count
		FROM projects p FULL JOIN tasks t ON t.project_id = p.id
		GROUP BY p.id, p.name, t.status`)
	if err != nil {
		return nil, err
	}
	spend, err := s.SpendSince(ctx, since)
	if err != nil {
		return nil, err
	}
	schedules, err := s.scheduleOverview(ctx, since)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"tasks":         groupCounts(rows),
		"agents":        emptyAgents(),
		"projects":      overviewProjects(rows),
		"idle_projects": idleProjects(rows),
		"spend_today":   spendView(since, spend),
		"schedules":     schedules,
		"health":        map[string]any{"unhealthy": []any{}},
	}, nil
}

// SpendSince is llm_records.spend_since from since, with no upper bound and no project.
func (s *Store) SpendSince(ctx context.Context, since time.Time) (Spend, error) {
	row, err := s.one(ctx, "spend", spendSQL, since, nil, nil)
	if err != nil {
		return Spend{}, err
	}
	if row == nil {
		return Spend{}, fmt.Errorf("spend: no row")
	}
	return Spend{
		Runs:       asInt(row["runs"]),
		Running:    asInt(row["running"]),
		Unpriced:   asInt(row["unpriced_runs"]),
		Provider:   mustFloat(row["provider_priced_usd"]),
		Equivalent: mustFloat(row["equivalent_usd"]),
	}, nil
}

// Projects is GET /api/projects.
func (s *Store) Projects(ctx context.Context) (map[string]any, error) {
	rows, err := s.query(ctx, "projects", `
		SELECT p.*,
		       count(t.id) AS task_count,
		       count(t.id) FILTER (WHERE t.status = ANY($1::text[])) AS active_count,
		       (SELECT count(*) FROM schedules s WHERE s.project_id = p.id) AS schedule_count
		FROM projects p LEFT JOIN tasks t ON t.project_id = p.id
		GROUP BY p.id ORDER BY lower(p.name)`, activeStatuses)
	if err != nil {
		return nil, err
	}
	projects := make([]any, 0, len(rows))
	for _, row := range rows {
		projects = append(projects, projectView(row))
	}
	return map[string]any{"projects": projects}, nil
}

// TaskExists reports whether id is a task. The Tasks page uses it for before.
func (s *Store) TaskExists(ctx context.Context, id string) (bool, error) {
	row, err := s.one(ctx, "task exists", `SELECT EXISTS (SELECT 1 FROM tasks WHERE id = $1) AS ok`, id)
	if err != nil {
		return false, err
	}
	return row != nil && asBool(row["ok"]), nil
}

// Tasks is one page of GET /api/tasks, newest first, plus one extra row for next_before.
func (s *Store) Tasks(ctx context.Context, q TaskQuery) (map[string]any, error) {
	if q.Limit <= 0 {
		q.Limit = 50
	}
	rows, err := s.query(ctx, "list tasks", taskFrom+`
		WHERE `+taskFilters+`
		  AND ($6::text IS NULL OR (t.created_at, t.id) < (SELECT b.created_at, b.id FROM tasks b WHERE b.id = $6))
		ORDER BY t.created_at DESC, t.id DESC LIMIT $7 OFFSET $8`,
		nullString(q.ProjectID), nullString(q.Status), textArray(q.Statuses), q.Inside,
		nullString(q.ScheduleID), nullString(q.Before), q.Limit+1, 0)
	if err != nil {
		return nil, err
	}
	shown := rows
	var next any
	if len(rows) > q.Limit {
		shown = rows[:q.Limit]
		next = asString(shown[len(shown)-1]["id"])
	}
	ids := make([]string, len(shown))
	for i, row := range shown {
		ids[i] = asString(row["id"])
	}
	costs, err := s.taskCosts(ctx, ids)
	if err != nil {
		return nil, err
	}
	items := make([]any, 0, len(shown))
	for _, row := range shown {
		id := asString(row["id"])
		items = append(items, taskListItem(row, costs[id]))
	}
	counts, err := s.countByStatus(ctx, q)
	if err != nil {
		return nil, err
	}
	return map[string]any{"tasks": items, "next_before": next, "counts": counts}, nil
}

// Task is GET /api/tasks/<id>. ErrNotFound when the id is unknown.
// A room read that fails is omitted (room null). Other read failures are returned.
func (s *Store) Task(ctx context.Context, id string) (map[string]any, error) {
	row, err := s.one(ctx, "task", taskFrom+` WHERE t.id = $1`, id)
	if err != nil {
		return nil, err
	}
	if row == nil {
		return nil, ErrNotFound
	}
	stepRows, err := s.query(ctx, "steps", `SELECT * FROM task_steps WHERE task_id = $1 ORDER BY step_index`, id)
	if err != nil {
		return nil, err
	}
	steps := make([]map[string]any, len(stepRows))
	stepList := make([]any, len(stepRows))
	for i, step := range stepRows {
		steps[i] = stepRow(step)
		stepList[i] = steps[i]
	}
	journalRows, err := s.query(ctx, "journal", `
		SELECT j.id, j.task_id, j.step_id, COALESCE(s.step_key, '') AS step_key, j.kind, j.summary, j.learnings,
		       j.via, j.delivered_at, j.created_at
		FROM task_journal j LEFT JOIN task_steps s ON s.id = j.step_id
		WHERE j.task_id = $1 ORDER BY j.id`, id)
	if err != nil {
		return nil, err
	}
	journal := make([]map[string]any, len(journalRows))
	journalList := make([]any, len(journalRows))
	for i, entry := range journalRows {
		journal[i] = journalRow(entry)
		journalList[i] = journal[i]
	}
	versions, err := s.query(ctx, "requirement versions", `
		SELECT version, text, via, created_at, delivered_at FROM task_requirement_versions
		WHERE task_id = $1 ORDER BY version`, id)
	if err != nil {
		return nil, err
	}
	actions, err := s.query(ctx, "manager actions", `
		SELECT created_at, channel, tool, arguments, risk, status, confirmation_id FROM manager_actions
		WHERE task_id = $1 AND status <> 'error' ORDER BY id DESC LIMIT 20`, id)
	if err != nil {
		return nil, err
	}
	worktree, err := s.one(ctx, "worktree", `SELECT * FROM task_worktrees WHERE task_id = $1`, id)
	if err != nil {
		return nil, err
	}
	costs, err := s.taskCosts(ctx, []string{id})
	if err != nil {
		return nil, err
	}
	runRows, err := s.query(ctx, "task runs", listRunsSQL, []string{id})
	if err != nil {
		return nil, err
	}
	roleRows, err := s.query(ctx, "role runs", roleRunsSQL, id)
	if err != nil {
		return nil, err
	}
	overrides, err := s.profileOverrides(ctx, id)
	if err != nil {
		return nil, err
	}
	status := strOr(row["status"], "pending")
	agents, err := s.taskAgents(ctx, asString(row["project_id"]), id, status)
	if err != nil {
		return nil, err
	}
	roles, err := s.taskRoles(ctx, id, steps, roleRows)
	if err != nil {
		return nil, err
	}
	gates, err := s.taskGates(ctx, row)
	if err != nil {
		return nil, err
	}
	workspaces, err := s.stepWorkspaces(ctx, id, steps, worktree != nil)
	if err != nil {
		return nil, err
	}
	room, err := s.room(ctx, id, steps)
	if err != nil {
		slog.Warn("task room", "task_id", id, "err", LogText(err))
		room = nil
	}
	return taskDetail(row, stepList, journal, journalList, versions, actions, worktree, costs[id], runRows, roleRows, steps, room, overrides, agents, roles, gates, workspaces), nil
}

func (s *Store) countByStatus(ctx context.Context, q TaskQuery) (map[string]any, error) {
	rows, err := s.query(ctx, "count tasks", `
		SELECT t.status, count(*) AS count FROM tasks t WHERE `+taskFilters+` GROUP BY t.status`,
		nullString(q.ProjectID), nil, textArray(q.Statuses), q.Inside, nullString(q.ScheduleID))
	if err != nil {
		return nil, err
	}
	counts := map[string]any{}
	for _, row := range rows {
		if row["status"] == nil {
			continue
		}
		counts[asString(row["status"])] = asInt(row["count"])
	}
	return counts, nil
}

func (s *Store) taskCosts(ctx context.Context, ids []string) (map[string]map[string]any, error) {
	if len(ids) == 0 {
		return map[string]map[string]any{}, nil
	}
	rows, err := s.query(ctx, "task costs", costSQL, ids, "interrupted")
	if err != nil {
		return nil, err
	}
	return assembleCosts(ids, rows), nil
}

func (s *Store) scheduleOverview(ctx context.Context, since time.Time) (map[string]any, error) {
	counts, err := s.one(ctx, "schedules", `SELECT count(*) AS total, count(*) FILTER (WHERE enabled) AS enabled FROM schedules`)
	if err != nil {
		return nil, err
	}
	upcoming, err := s.query(ctx, "schedule upcoming", `
		SELECT s.id, s.name, s.project_id, p.name AS project_name, s.next_fire_at, s.timezone
		FROM schedules s JOIN projects p ON p.id = s.project_id
		WHERE s.enabled AND s.next_fire_at IS NOT NULL
		ORDER BY s.next_fire_at, s.id LIMIT 5`)
	if err != nil {
		return nil, err
	}
	failing, err := s.query(ctx, "schedule failing", `
		SELECT s.id, s.name, s.project_id, p.name AS project_name, s.timezone, s.consecutive_failures,
		       s.failures_alert_at, s.stale_alert_at, s.last_success_at,
		       (SELECT r.status FROM schedule_runs r WHERE r.schedule_id = s.id AND NOT (r.status = ANY($1::text[]))
		        ORDER BY r.created_at DESC, r.id DESC LIMIT 1) AS last_status
		FROM schedules s JOIN projects p ON p.id = s.project_id
		WHERE s.consecutive_failures > 0 OR s.failures_alert_at IS NOT NULL OR s.stale_alert_at IS NOT NULL
		ORDER BY (s.failures_alert_at IS NULL AND s.stale_alert_at IS NULL), s.consecutive_failures DESC, lower(s.name)`,
		openScheduleStatuses)
	if err != nil {
		return nil, err
	}
	skipped, err := s.query(ctx, "schedule skipped", `
		SELECT status, sum(firings) AS n FROM schedule_runs
		WHERE created_at >= $1 AND status = ANY($2::text[]) GROUP BY status`, since, skippedScheduleRuns)
	if err != nil {
		return nil, err
	}
	return scheduleView(counts, upcoming, failing, skipped), nil
}

func (s *Store) room(ctx context.Context, taskID string, steps []map[string]any) (map[string]any, error) {
	project, err := s.one(ctx, "room project", `
		SELECT p.agent_room FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.id = $1`, taskID)
	if err != nil {
		return nil, err
	}
	kinds, err := s.query(ctx, "room messages", `
		SELECT kind, count(*) AS n FROM room_messages WHERE task_id = $1 GROUP BY kind`, taskID)
	if err != nil {
		return nil, err
	}
	messages, err := s.query(ctx, "room by step", `
		SELECT id, sender_seat, audience, kind, reply_to, answers_ids
		FROM room_messages WHERE task_id = $1 ORDER BY id`, taskID)
	if err != nil {
		return nil, err
	}
	unanswered, err := s.one(ctx, "room unanswered", `
		SELECT count(*) AS n FROM room_messages q
		WHERE q.task_id = $1 AND q.kind = 'question'
		  AND NOT EXISTS (
		    SELECT 1 FROM room_messages a
		    WHERE a.task_id = q.task_id AND a.kind = 'answer'
		      AND (a.reply_to = q.id OR q.id = ANY(a.answers_ids)))`, taskID)
	if err != nil {
		return nil, err
	}
	consults, err := s.query(ctx, "room consults", `
		SELECT state, fallback_reason, cost_usd FROM room_consults WHERE task_id = $1 ORDER BY id`, taskID)
	if err != nil {
		return nil, err
	}
	enabled := true
	if project != nil && asString(project["agent_room"]) == "off" {
		enabled = false
	}
	pending := 0
	if unanswered != nil {
		pending = asInt(unanswered["n"])
	}
	return roomView(enabled, kinds, pending, consults, roomByStep(steps, messages)), nil
}

func (s *Store) one(ctx context.Context, label, query string, args ...any) (map[string]any, error) {
	rows, err := s.query(ctx, label, query, args...)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return rows[0], nil
}

func nullString(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func textArray(v []string) any {
	if v == nil {
		return nil
	}
	return v
}

func emptyAgents() map[string]any {
	return map[string]any{
		"manager":   nil,
		"planners":  []any{},
		"workers":   []any{},
		"reviewers": []any{},
	}
}

func spendView(since time.Time, spend Spend) map[string]any {
	return map[string]any{
		"since":               localISO(since),
		"runs":                spend.Runs,
		"running":             spend.Running,
		"unpriced_runs":       spend.Unpriced,
		"provider_priced_usd": spend.Provider,
		"equivalent_usd":      spend.Equivalent,
	}
}

func overviewProjects(rows []map[string]any) []any {
	names := map[string]string{}
	groups := map[string]map[string]int{}
	for _, row := range rows {
		if row["project_id"] == nil {
			continue
		}
		id := asString(row["project_id"])
		names[id] = asString(row["project_name"])
		if row["status"] == nil {
			continue
		}
		g := groups[id]
		if g == nil {
			g = map[string]int{}
			groups[id] = g
		}
		g[taskGroup(asString(row["status"]))] += asInt(row["count"])
	}
	var active []string
	for id, g := range groups {
		if g[groupNeedsYou] > 0 || g[groupInProgress] > 0 || g[groupReady] > 0 {
			active = append(active, id)
		}
	}
	sort.Slice(active, func(i, j int) bool {
		left := strings.ToLower(overviewLabel(names[active[i]], active[i]))
		right := strings.ToLower(overviewLabel(names[active[j]], active[j]))
		return left < right
	})
	out := make([]any, 0, len(active))
	for _, id := range active {
		ready := 0
		if groups[id] != nil {
			ready = groups[id][groupReady]
		}
		name := overviewLabel(names[id], id)
		out = append(out, map[string]any{
			"project_id": id,
			"name":       name,
			"tasks":      map[string]any{"running": 0, "ready": ready},
			"workers":    map[string]any{"running": 0, "waiting": 0},
			"reviewers":  map[string]any{"running": 0, "waiting": 0},
		})
	}
	return out
}

// overviewLabel is Python's `name or project_id`. An empty name falls back to
// the id. A whitespace-only name does not: it is truthy there.
func overviewLabel(name, id string) string {
	if name == "" {
		return id
	}
	return name
}

func idleProjects(rows []map[string]any) int {
	names := map[string]struct{}{}
	active := map[string]struct{}{}
	groups := map[string]map[string]int{}
	for _, row := range rows {
		if row["project_id"] == nil {
			continue
		}
		id := asString(row["project_id"])
		names[id] = struct{}{}
		if row["status"] == nil {
			continue
		}
		g := groups[id]
		if g == nil {
			g = map[string]int{}
			groups[id] = g
		}
		g[taskGroup(asString(row["status"]))] += asInt(row["count"])
	}
	for id, g := range groups {
		if g[groupNeedsYou] > 0 || g[groupInProgress] > 0 || g[groupReady] > 0 {
			active[id] = struct{}{}
		}
	}
	return len(names) - len(active)
}

func scheduleView(counts map[string]any, upcoming, failing, skipped []map[string]any) map[string]any {
	if counts == nil {
		counts = map[string]any{}
	}
	skippedToday := map[string]any{
		"skipped_gate":    0,
		"skipped_overlap": 0,
		"skipped_missed":  0,
		"skipped_budget":  0,
	}
	for _, row := range skipped {
		status := asString(row["status"])
		if _, ok := skippedToday[status]; ok {
			skippedToday[status] = asInt(row["n"])
		}
	}
	next := make([]any, 0, len(upcoming))
	for _, row := range upcoming {
		next = append(next, map[string]any{
			"schedule_id":  row["id"],
			"name":         row["name"],
			"project_id":   row["project_id"],
			"project_name": row["project_name"],
			"next_fire_at": row["next_fire_at"],
			"timezone":     row["timezone"],
		})
	}
	bad := make([]any, 0, len(failing))
	for _, row := range failing {
		bad = append(bad, map[string]any{
			"schedule_id":          row["id"],
			"name":                 row["name"],
			"project_id":           row["project_id"],
			"project_name":         row["project_name"],
			"timezone":             row["timezone"],
			"consecutive_failures": asInt(row["consecutive_failures"]),
			"failures_since":       row["failures_alert_at"],
			"stale_since":          row["stale_alert_at"],
			"last_success_at":      row["last_success_at"],
			"last_status":          row["last_status"],
		})
	}
	return map[string]any{
		"total":         asInt(counts["total"]),
		"enabled":       asInt(counts["enabled"]),
		"next":          next,
		"failing":       bad,
		"skipped_today": skippedToday,
	}
}

func roomView(enabled bool, kinds []map[string]any, unanswered int, consults []map[string]any, byStep map[string]any) map[string]any {
	byKind := map[string]int{}
	messages, questions := 0, 0
	for _, row := range kinds {
		n := asInt(row["n"])
		kind := asString(row["kind"])
		byKind[kind] = n
		messages += n
		if kind == "question" {
			questions = n
		}
	}
	answered := 0
	reasons := map[string]int{}
	var cost float64
	for _, row := range consults {
		if asString(row["state"]) == "answered" {
			answered++
		}
		if reason := asString(row["fallback_reason"]); row["fallback_reason"] != nil && reason != "" {
			reasons[reason]++
		}
		if v, ok := asFloat(row["cost_usd"]); ok {
			cost += v
		}
	}
	var costOut any = 0
	if len(consults) > 0 {
		costOut = roundN(cost, 6)
	}
	byReason := map[string]any{}
	reasonNames := make([]string, 0, len(reasons))
	for name := range reasons {
		reasonNames = append(reasonNames, name)
	}
	sort.Strings(reasonNames)
	for _, name := range reasonNames {
		byReason[name] = reasons[name]
	}
	if byStep == nil {
		byStep = map[string]any{}
	}
	return map[string]any{
		"enabled": enabled,
		"counts": map[string]any{
			"messages":   messages,
			"by_kind":    byKind,
			"questions":  questions,
			"unanswered": unanswered,
		},
		"by_step": byStep,
		"consults": map[string]any{
			"total":     len(consults),
			"answered":  answered,
			"by_reason": byReason,
			"cost_usd":  costOut,
		},
	}
}

func taskDetail(row map[string]any, steps []any, journal []map[string]any, journalList []any, versions, actions []map[string]any, worktree map[string]any, cost map[string]any, runs, roleRows, stepMaps []map[string]any, room, overrides, agents map[string]any, roles, gates []any, workspaces any) map[string]any {
	id := asString(row["id"])
	status := strOr(row["status"], "pending")
	versionList := make([]any, len(versions))
	for i, version := range versions {
		versionList[i] = version
	}
	actionList := make([]any, len(actions))
	for i, action := range actions {
		actionList[i] = map[string]any{
			"created_at":      action["created_at"],
			"channel":         action["channel"],
			"tool":            action["tool"],
			"arguments":       jsonValue(action["arguments"]),
			"risk":            action["risk"],
			"status":          action["status"],
			"confirmation_id": action["confirmation_id"],
		}
	}
	if steps == nil {
		steps = []any{}
	}
	if journalList == nil {
		journalList = []any{}
	}
	if overrides == nil {
		overrides = map[string]any{}
	}
	if roles == nil {
		roles = []any{}
	}
	if gates == nil {
		gates = []any{}
	}
	out := map[string]any{
		"task_id":              id,
		"requirement":          asString(row["requirement"]),
		"name":                 taskName(asString(row["title"]), asString(row["goal_id"]), asString(row["requirement"]), id),
		"requirement_versions": versionList,
		"supplements":          supplementsOf(journal),
		"manager_actions":      actionList,
		"goal_id":              asString(row["goal_id"]),
		"project_id":           asString(row["project_id"]),
		"project_name":         asString(row["project_name"]),
		"status":               status,
		"group":                taskGroup(status),
		"phase":                strOr(row["phase"], "pending"),
		"planner_id":           row["planner_id"],
		"correlation_id":       asString(row["correlation_id"]),
		"work_dir":             asString(row["work_dir"]),
		"config":               readConfig(row["config"]),
		"profile_overrides":    overrides,
		"agents":               agents,
		"roles":                roles,
		"builtin_role_stats":   builtinStats(roleRows, stepMaps),
		"gates":                gates,
		"task_workspace":       taskWorkspace(row, worktree),
		"step_workspaces":      workspaces,
		"room":                 room,
		"origin_channel_id":    asString(row["origin_channel_id"]),
		"result_summary":       row["result_summary"],
		"error":                row["error"],
		"fail_kind":            row["fail_kind"],
		"task_budget_usd":      taskBudgetUSD(asString(row["project_id"]), row["task_budget_usd"]),
		"own_budget_usd":       row["budget_usd"],
		"schedule_id":          row["schedule_id"],
		"schedule_name":        row["schedule_name"],
		"created_at":           row["created_at"],
		"updated_at":           row["updated_at"],
		"completed_at":         row["completed_at"],
		"steps":                steps,
		"journal":              journalList,
		"progress":             progressNotes(journal),
		"cost":                 cost,
	}
	if roles := runRoles(runs, stepMaps); len(roles) > 0 {
		out["run_roles"] = roles
	}
	return out
}

func jsonValue(v any) any {
	s, ok := v.(string)
	if !ok {
		return v
	}
	var out any
	if json.Unmarshal([]byte(s), &out) != nil {
		return v
	}
	return out
}
