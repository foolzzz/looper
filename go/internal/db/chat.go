package db

import (
	"context"
	"log/slog"
	"math"
	"sort"
	"strconv"
	"strings"
)

const (
	chatTaskLimit        = 30
	chatSessionLimit     = 50
	chatMessageLimit     = 200
	chatLastMessageChars = 200
	plannerThreadPrefix  = "planner-"
)

// ChatTargets is chat_targets.list_chat_targets with no live planners.
// Python loads tasks, then steps, then runs. A later exception keeps whatever
// was already assigned, so a failed step or run read still leaves Planner
// targets for the tasks. That failure is logged and not returned. A failed
// task read is returned, and the handler then lists only the Manager.
func (s *Store) ChatTargets(ctx context.Context) ([]map[string]any, error) {
	tasks, err := s.query(ctx, "chat tasks", taskFrom+`
		WHERE `+taskFilters+`
		  AND ($6::text IS NULL OR (t.created_at, t.id) < (SELECT b.created_at, b.id FROM tasks b WHERE b.id = $6))
		ORDER BY t.created_at DESC, t.id DESC LIMIT $7 OFFSET $8`,
		nil, nil, nil, false, nil, nil, chatTaskLimit, 0)
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(tasks))
	seen := make(map[string]bool, len(tasks))
	for _, row := range tasks {
		id := asString(row["id"])
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		ids = append(ids, id)
	}
	sort.Strings(ids)
	var steps, runs []map[string]any
	if len(ids) > 0 {
		steps, err = s.query(ctx, "chat steps", `
			SELECT id, task_id, step_index, instruction, worker_id FROM task_steps
			WHERE task_id = ANY($1::text[]) ORDER BY task_id, step_index`, ids)
		if err != nil {
			slog.Warn("chat targets", "err", LogText(err))
			return buildChatTargets(tasks, nil, nil), nil
		}
		runs, err = s.query(ctx, "chat runs", listRunsSQL, ids)
		if err != nil {
			slog.Warn("chat targets", "err", LogText(err))
			return buildChatTargets(tasks, steps, nil), nil
		}
	}
	return buildChatTargets(tasks, steps, runs), nil
}

// ListChatSessions is chat.list_sessions. Planner threads are left out.
// last_content is the first 200 characters of the last message.
func (s *Store) ListChatSessions(ctx context.Context) ([]map[string]any, error) {
	return s.query(ctx, "chat sessions", `
		SELECT s.session_id, s.channel, s.message_count, s.first_message_at, s.last_message_at,
		       last.role AS last_role, left(last.content, $3) AS last_content
		FROM (
		    SELECT session_id, min(channel) AS channel, count(*) AS message_count,
		           min(created_at) AS first_message_at, max(created_at) AS last_message_at, max(id) AS last_id
		    FROM chat_messages
		    WHERE NOT starts_with(session_id, $2)
		    GROUP BY session_id
		    ORDER BY max(created_at) DESC, max(id) DESC
		    LIMIT $1
		) s
		JOIN chat_messages last ON last.id = s.last_id
		ORDER BY s.last_message_at DESC, s.last_id DESC`,
		chatSessionLimit, plannerThreadPrefix, chatLastMessageChars)
}

// ListChatMessages is chat.list_messages: the newest limit, then oldest first.
func (s *Store) ListChatMessages(ctx context.Context, sessionID string) ([]map[string]any, error) {
	return s.query(ctx, "chat messages", `
		SELECT message_id, role, content, channel, created_at, delivery_failure, delivery_error FROM (
		    SELECT id, message_id, role, content, channel, created_at, delivery_failure, delivery_error
		    FROM chat_messages
		    WHERE session_id = $1
		    ORDER BY id DESC
		    LIMIT $2
		) newest
		ORDER BY id ASC`, sessionID, chatMessageLimit)
}

// buildChatTargets is chat_targets.build_chat_targets with no live planners.
// Tasks are newest first. A tie keeps the incoming order, which the query
// already set to id descending. Step agents keep the first-seen run order.
func buildChatTargets(tasks, steps, runs []map[string]any) []map[string]any {
	targets := []map[string]any{{
		"kind": "manager", "id": "manager", "can_send": true,
	}}
	rows := append([]map[string]any{}, tasks...)
	sort.SliceStable(rows, func(i, j int) bool {
		return asString(rows[i]["created_at"]) > asString(rows[j]["created_at"])
	})

	stepByID := make(map[string]map[string]any, len(steps))
	stepByWorker := make(map[string]map[string]any)
	for _, step := range steps {
		stepByID[asString(step["id"])] = step
		if worker := asString(step["worker_id"]); worker != "" {
			stepByWorker[worker] = step
		}
	}

	type taskAgents struct {
		order []string
		byID  map[string]map[string]any
	}
	byTask := map[string]*taskAgents{}
	for _, run := range runs {
		taskID := asString(run["task_id"])
		agents := byTask[taskID]
		if agents == nil {
			agents = &taskAgents{byID: map[string]map[string]any{}}
			byTask[taskID] = agents
		}
		agentID := asString(run["agent_id"])
		if existing := agents.byID[agentID]; existing != nil {
			existing["status"] = run["status"]
			continue
		}
		step := stepByID[asString(run["step_id"])]
		if step == nil {
			step = stepByWorker[agentID]
		}
		kind := "worker"
		if strings.HasPrefix(agentID, "reviewer-") {
			kind = "reviewer"
		}
		var stepIndex any
		instruction := ""
		if step != nil {
			stepIndex = step["step_index"]
			instruction = asString(step["instruction"])
		}
		agent := map[string]any{
			"kind":        kind,
			"id":          agentID,
			"task_id":     taskID,
			"status":      run["status"],
			"step_index":  stepIndex,
			"instruction": instruction,
			"started_at":  run["created_at"],
			"can_send":    false,
		}
		if role := chatRunRole(run["task_role"]); role != nil {
			agent["task_role"] = role
		}
		agents.byID[agentID] = agent
		agents.order = append(agents.order, agentID)
	}

	for _, row := range rows {
		taskID := asString(row["id"])
		targets = append(targets, map[string]any{
			"kind":        "planner",
			"id":          "planner-" + taskID,
			"task_id":     taskID,
			"requirement": row["requirement"],
			"name":        taskName(asString(row["title"]), asString(row["goal_id"]), asString(row["requirement"]), taskID),
			"status":      row["status"],
			"can_send":    false,
		})
		if agents := byTask[taskID]; agents != nil {
			for _, id := range agents.order {
				targets = append(targets, agents.byID[id])
			}
		}
	}
	return targets
}

// chatRunRole is task_roles.run_role for a run's task_role JSON value.
func chatRunRole(raw any) map[string]any {
	m, ok := raw.(map[string]any)
	if !ok {
		return nil
	}
	id, hasID := m["id"]
	kind, hasKind := m["kind"]
	version, hasVersion := m["version"]
	if !hasID || !hasKind || !hasVersion {
		return nil
	}
	kindName, ok := kind.(string)
	if !ok || (kindName != "work" && kindName != "review") {
		return nil
	}
	ver, ok := RoleVersion(version)
	if !ok {
		return nil
	}
	text, ok := chatRoleID(id)
	if !ok {
		return nil
	}
	return map[string]any{"id": text, "kind": kindName, "version": ver}
}

func chatRoleID(v any) (string, bool) {
	switch t := v.(type) {
	case string:
		return t, true
	case bool:
		if t {
			return "True", true
		}
		return "False", true
	case int:
		return strconv.Itoa(t), true
	case int32:
		return strconv.FormatInt(int64(t), 10), true
	case int64:
		return strconv.FormatInt(t, 10), true
	case float64:
		if math.IsNaN(t) || math.IsInf(t, 0) {
			return "", false
		}
		if t == math.Trunc(t) && t >= math.MinInt64 && t <= math.MaxInt64 {
			return strconv.FormatInt(int64(t), 10), true
		}
		return strconv.FormatFloat(t, 'f', -1, 64), true
	default:
		return "", false
	}
}
