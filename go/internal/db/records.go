package db

import (
	"context"
	"fmt"
	"strings"
)

// RecordQuery is GET /api/llm-records. Empty AgentID, TaskID, and Role are
// unset. Limit and Offset are the values Python already parsed, including a
// negative limit or offset, which PostgreSQL rejects.
type RecordQuery struct {
	AgentID string
	TaskID  string
	Role    string
	Limit   int64
	Offset  int64
}

// Fixed fragments from llm_records._ROLE_FILTERS. The role text is never
// interpolated; an unknown role returns no rows and does not query.
var roleFilters = map[string]string{
	"manager":  "agent_id = 'manager'",
	"planner":  "agent_id LIKE 'planner-%'",
	"worker":   "agent_id LIKE 'worker-%'",
	"reviewer": "agent_id LIKE 'reviewer-%'",
}

// ListRecords is llm_records.list_records. Newest first. An unknown role is
// an empty list and does not touch the database.
func (s *Store) ListRecords(ctx context.Context, q RecordQuery) ([]map[string]any, error) {
	conds := make([]string, 0, 3)
	args := make([]any, 0, 4)
	if q.Role != "" {
		clause, ok := roleFilters[q.Role]
		if !ok {
			return []map[string]any{}, nil
		}
		conds = append(conds, clause)
	}
	n := 1
	if q.AgentID != "" {
		conds = append(conds, fmt.Sprintf("agent_id = $%d", n))
		args = append(args, q.AgentID)
		n++
	}
	if q.TaskID != "" {
		conds = append(conds, fmt.Sprintf("task_id = $%d", n))
		args = append(args, q.TaskID)
		n++
	}
	where := ""
	if len(conds) > 0 {
		where = "WHERE " + strings.Join(conds, " AND ")
	}
	args = append(args, q.Limit, q.Offset)
	query := fmt.Sprintf(
		"SELECT * FROM llm_records %s ORDER BY created_at DESC LIMIT $%d OFFSET $%d",
		where, n, n+1,
	)
	return s.query(ctx, "llm records", query, args...)
}

// GetRecord is llm_records.get_record. ErrNotFound when the id is absent.
func (s *Store) GetRecord(ctx context.Context, id int64) (map[string]any, error) {
	row, err := s.one(ctx, "llm record", `SELECT * FROM llm_records WHERE id = $1`, id)
	if err != nil {
		return nil, err
	}
	if row == nil {
		return nil, ErrNotFound
	}
	return row, nil
}

// ListEvents is llm_records.list_events. A record with no events, including
// an id that is not in the table, is an empty list.
func (s *Store) ListEvents(ctx context.Context, id, after, limit int64) ([]map[string]any, error) {
	return s.query(ctx, "llm record events", `
		SELECT seq, event, created_at FROM llm_record_events
		WHERE record_id = $1 AND seq > $2
		ORDER BY seq
		LIMIT $3`, id, after, limit)
}

// AgentUsage is llm_records.agent_usage. turns is the already clamped count.
// by_day.day is a date. Costs here are float8 sums from PostgreSQL, not the
// Decimal totals task costs use.
func (s *Store) AgentUsage(ctx context.Context, agentID string, turns int) (map[string]any, error) {
	total, err := s.one(ctx, "agent usage total", `
		SELECT count(*) AS runs,
		       count(*) FILTER (WHERE status = 'running') AS running,
		       count(*) FILTER (WHERE status <> 'running' AND cost_usd IS NULL) AS unpriced_runs,
		       COALESCE(sum(cost_usd), 0) AS cost_usd,
		       COALESCE(sum(jsonb_array_length(compactions)), 0) AS compactions
		FROM llm_records WHERE agent_id = $1`, agentID)
	if err != nil {
		return nil, err
	}
	if total == nil {
		return nil, fmt.Errorf("agent usage: no total")
	}
	sessions, err := s.query(ctx, "agent usage sessions", `
		SELECT session_id, count(*) AS runs, COALESCE(sum(cost_usd), 0) AS cost_usd,
		       min(created_at) AS first_at, max(created_at) AS last_at
		FROM llm_records WHERE agent_id = $1
		GROUP BY session_id ORDER BY max(created_at) DESC LIMIT 50`, agentID)
	if err != nil {
		return nil, err
	}
	days, err := s.query(ctx, "agent usage days", `
		SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*) AS runs,
		       COALESCE(sum(cost_usd), 0) AS cost_usd
		FROM llm_records WHERE agent_id = $1
		GROUP BY day ORDER BY day DESC LIMIT 60`, agentID)
	if err != nil {
		return nil, err
	}
	latest, err := s.query(ctx, "agent usage turns", `
		SELECT id, created_at, status, session_id, cost_usd, context_tokens, compactions
		FROM llm_records WHERE agent_id = $1
		ORDER BY created_at DESC, id DESC LIMIT $2`, agentID, turns)
	if err != nil {
		return nil, err
	}
	for i, j := 0, len(latest)-1; i < j; i, j = i+1, j-1 {
		latest[i], latest[j] = latest[j], latest[i]
	}
	var latestContext any
	for i := len(latest) - 1; i >= 0; i-- {
		if latest[i]["context_tokens"] != nil {
			latestContext = latest[i]["context_tokens"]
			break
		}
	}
	return map[string]any{
		"agent_id":              agentID,
		"total":                 total,
		"by_session":            mapsAny(sessions),
		"by_day":                mapsAny(days),
		"turns":                 mapsAny(latest),
		"latest_context_tokens": latestContext,
	}, nil
}

func mapsAny(rows []map[string]any) []any {
	out := make([]any, len(rows))
	for i, row := range rows {
		out[i] = row
	}
	return out
}
