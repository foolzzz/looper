package db

import (
	"context"
	"strings"
	"time"
)

// RoomViewLimit is room.TASK_VIEW_LIMIT. A request limit is clamped to 1..this.
const RoomViewLimit = 500

// RoomQuery is GET /api/tasks/<id>/room. Kind and Sender empty mean no filter.
// AfterTooBig is a whole number past int64: every message id fails id > after.
// Limit is clamped again in TaskRoom.
type RoomQuery struct {
	Kind        string
	Sender      string
	Unanswered  bool
	After       int64
	AfterTooBig bool
	Limit       int
}

// roomRun is one WorkerPool room run: its status ("pending" or "running") and
// since when. The HTTP handler has no pool, so it passes none.
type roomRun struct {
	State string
	Since time.Time
}

type roomMember struct {
	Seat       string
	Role       string
	Kind       string
	StepKey    string
	StepStatus string
	Presence   string
	Since      *time.Time
}

func (m roomMember) view() map[string]any {
	out := map[string]any{
		"seat":        m.Seat,
		"role":        m.Role,
		"kind":        m.Kind,
		"step":        m.StepKey,
		"step_status": m.StepStatus,
		"presence":    m.Presence,
	}
	if m.Since != nil {
		out["since"] = pyTime(*m.Since)
	}
	return out
}

const roomMessageColumns = `id, task_id, sender_seat, sender_kind, sender_step, sender_run, via, audience_all, audience, cc_planner, ` +
	`kind, reason, reply_to, answers_ids, refs, body, planner_pending, created_at`

// TaskRoom is room.task_view with no live runs. ErrNotFound when the task is
// missing. The project being missing leaves the room enabled.
func (s *Store) TaskRoom(ctx context.Context, taskID string, q RoomQuery) (map[string]any, error) {
	task, err := s.one(ctx, "room task", `SELECT id, project_id FROM tasks WHERE id = $1`, taskID)
	if err != nil {
		return nil, err
	}
	if task == nil {
		return nil, ErrNotFound
	}
	var project map[string]any
	if task["project_id"] != nil && asString(task["project_id"]) != "" {
		project, err = s.one(ctx, "room project settings", `SELECT agent_room FROM projects WHERE id = $1`, task["project_id"])
		if err != nil {
			return nil, err
		}
	}
	steps, err := s.query(ctx, "room steps", `SELECT * FROM task_steps WHERE task_id = $1 ORDER BY step_index`, taskID)
	if err != nil {
		return nil, err
	}
	for i := range steps {
		steps[i] = stepRow(steps[i])
	}
	messages, err := s.query(ctx, "room task messages", `
		SELECT `+roomMessageColumns+` FROM room_messages WHERE task_id = $1 AND id > 0 ORDER BY id ASC`, taskID)
	if err != nil {
		return nil, err
	}
	consults, err := s.query(ctx, "room task consults", `SELECT * FROM room_consults WHERE task_id = $1 ORDER BY id`, taskID)
	if err != nil {
		return nil, err
	}
	return roomTaskView(project, steps, messages, consults, nil, q), nil
}

func roomTaskView(project map[string]any, steps, messages, consults []map[string]any, runs map[string]roomRun, q RoomQuery) map[string]any {
	enabled := true
	if project != nil && asString(project["agent_room"]) == "off" {
		enabled = false
	}
	asked := map[string]bool{}
	for _, row := range consults {
		if asString(row["state"]) == "running" {
			asked[asString(row["seat"])] = true
		}
	}
	seats := roomMembers(steps, runs, asked)
	answered := roomAnswered(messages)
	byStep := roomByStepStats(seats, messages, answered)
	shown := messages
	if q.Unanswered {
		shown = filterRoom(shown, func(row map[string]any) bool {
			id, _ := messageID(row["id"])
			return asString(row["kind"]) == "question" && !answered[id]
		})
	}
	if q.Kind != "" {
		kind := q.Kind
		shown = filterRoom(shown, func(row map[string]any) bool {
			return asString(row["kind"]) == kind
		})
	}
	if q.Sender != "" {
		sender := q.Sender
		shown = filterRoom(shown, func(row map[string]any) bool {
			return roomSender(asString(row["sender_seat"]), sender)
		})
	}
	if q.AfterTooBig {
		shown = []map[string]any{}
	} else {
		after := q.After
		shown = filterRoom(shown, func(row map[string]any) bool {
			id, _ := messageID(row["id"])
			return id > after
		})
	}
	limit := roomPageLimit(q.Limit)
	more := len(shown) > limit
	if len(shown) > limit {
		shown = shown[len(shown)-limit:]
	}
	views := make([]any, 0, len(shown))
	for _, row := range shown {
		id, _ := messageID(row["id"])
		view := roomMessageView(row)
		view["cc_planner"] = asBool(row["cc_planner"])
		view["answered"] = asString(row["kind"]) == "question" && answered[id]
		views = append(views, view)
	}
	byKind := map[string]int{}
	questions, unansweredN := 0, 0
	for _, row := range messages {
		kind := asString(row["kind"])
		byKind[kind]++
		if kind != "question" {
			continue
		}
		questions++
		id, _ := messageID(row["id"])
		if !answered[id] {
			unansweredN++
		}
	}
	seatViews := make([]any, 0, len(seats))
	for _, seat := range seats {
		seatViews = append(seatViews, seat.view())
	}
	return map[string]any{
		"enabled":  enabled,
		"seats":    seatViews,
		"messages": views,
		"more":     more,
		"counts": map[string]any{
			"messages":   len(messages),
			"by_kind":    byKind,
			"questions":  questions,
			"unanswered": unansweredN,
		},
		"by_step":  byStep,
		"consults": roomConsults(consults),
	}
}

func roomPageLimit(n int) int {
	if n < 1 {
		return 1
	}
	if n > RoomViewLimit {
		return RoomViewLimit
	}
	return n
}

func filterRoom(rows []map[string]any, keep func(map[string]any) bool) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		if keep(row) {
			out = append(out, row)
		}
	}
	return out
}

func roomSender(seat, sender string) bool {
	if seat == sender {
		return true
	}
	head, _, _ := strings.Cut(seat, "@")
	return head == sender
}

// roomMembers is room.members. A gate has no seat. review_role_key is read
// when the row still has it; the current schema stores review_roles instead.
func roomMembers(steps []map[string]any, runs map[string]roomRun, consulting map[string]bool) []roomMember {
	var found []roomMember
	for _, step := range steps {
		if asString(step["kind"]) == "gate" {
			continue
		}
		key := asString(step["step_key"])
		if key == "" {
			key = asString(step["id"])
		}
		status := asString(step["status"])
		if status == "" {
			status = "pending"
		}
		role := asString(step["role_key"])
		if role == "" {
			role = "worker"
		}
		worker := asString(step["worker_id"])
		found = append(found, roomSeat(role, "work", key, status, worker, worker != "", runs))
		reviewer := asString(step["reviewer_id"])
		stage := asString(step["review_stage"])
		reviewRole := asString(step["review_role_key"])
		if asBool(step["review"]) || reviewer != "" || reviewRole != "" {
			run := ""
			if stage != "role" {
				run = reviewer
			}
			found = append(found, roomSeat("reviewer", "review", key, status, run, reviewer != "" || stage == "role", runs))
		}
		if reviewRole != "" {
			run := ""
			if stage == "role" {
				run = reviewer
			}
			result, _ := step["result"].(map[string]any)
			_, roleReview := result["role_review"]
			found = append(found, roomSeat(reviewRole, "review", key, status, run, stage == "role" || roleReview, runs))
		}
	}
	for i, member := range found {
		if member.Presence == "offline" && consulting[member.Seat] {
			found[i].Presence = "consulting"
		}
	}
	return found
}

func roomSeat(role, kind, key, status, run string, ran bool, runs map[string]roomRun) roomMember {
	member := roomMember{
		Seat:       role + "@" + key,
		Role:       role,
		Kind:       kind,
		StepKey:    key,
		StepStatus: status,
	}
	if run != "" {
		if state, ok := runs[run]; ok {
			member.Presence = "online"
			if state.State == "pending" {
				member.Presence = "waiting"
			}
			since := state.Since
			member.Since = &since
			return member
		}
	}
	switch {
	case ran:
		member.Presence = "offline"
	case status == "skipped":
		member.Presence = "dropped"
	default:
		member.Presence = "planned"
	}
	return member
}

func roomAnswered(rows []map[string]any) map[int64]bool {
	out := map[int64]bool{}
	for _, row := range rows {
		if asString(row["kind"]) != "answer" {
			continue
		}
		if row["reply_to"] != nil {
			if id, ok := messageID(row["reply_to"]); ok {
				out[id] = true
			}
		}
		for _, id := range intList(row["answers_ids"]) {
			out[int64(id)] = true
		}
	}
	return out
}

func roomByStepStats(seats []roomMember, messages []map[string]any, answered map[int64]bool) map[string]any {
	seen := map[string]bool{}
	var order []string
	for _, seat := range seats {
		if !seen[seat.StepKey] {
			seen[seat.StepKey] = true
			order = append(order, seat.StepKey)
		}
	}
	out := map[string]any{}
	for _, label := range order {
		keys := map[string]bool{}
		roles := map[string]bool{}
		for _, seat := range seats {
			if seat.StepKey == label {
				keys[seat.Seat] = true
				roles[seat.Role] = true
			}
		}
		sent, received, unanswered := 0, 0, 0
		for _, row := range messages {
			audience := stringSet(row["audience"])
			isSent := keys[asString(row["sender_seat"])]
			isReceived := !isSent && setIntersects(audience, keys, roles)
			if isSent {
				sent++
			}
			if isReceived {
				received++
			}
			id, _ := messageID(row["id"])
			if asString(row["kind"]) == "question" && !answered[id] && (isSent || isReceived) {
				unanswered++
			}
		}
		if sent != 0 || received != 0 || unanswered != 0 {
			out[label] = map[string]any{"sent": sent, "received": received, "unanswered": unanswered}
		}
	}
	return out
}

func roomMessageView(row map[string]any) map[string]any {
	view := map[string]any{
		"id":   row["id"],
		"from": row["sender_seat"],
		"kind": row["kind"],
		"body": row["body"],
	}
	if asBool(row["audience_all"]) {
		view["to"] = "all"
	} else {
		view["to"] = stringList(row["audience"])
	}
	if id, ok := messageID(row["reply_to"]); ok && row["reply_to"] != nil && id != 0 {
		view["reply_to"] = row["reply_to"]
	}
	if ids := intList(row["answers_ids"]); len(ids) > 0 {
		view["answers_ids"] = ids
	}
	refs := jsonArray(row["refs"])
	if files := refValues(refs, "file"); len(files) > 0 {
		view["files"] = files
	}
	if commits := refValues(refs, "commit"); len(commits) > 0 {
		view["commits"] = commits
	}
	if reason := asString(row["reason"]); row["reason"] != nil && reason != "" {
		view["reason"] = reason
	}
	if via := asString(row["via"]); via != "" && via != "agent" {
		view["via"] = via
	}
	if row["created_at"] != nil {
		view["at"] = row["created_at"]
	}
	return view
}

func refValues(refs []any, key string) []any {
	var out []any
	for _, ref := range refs {
		item, ok := ref.(map[string]any)
		if !ok {
			continue
		}
		text, ok := item[key].(string)
		if ok && text != "" {
			out = append(out, text)
		}
	}
	return out
}

func jsonArray(v any) []any {
	switch t := v.(type) {
	case nil:
		return []any{}
	case []any:
		if t == nil {
			return []any{}
		}
		return t
	case string:
		return jsonList(t)
	case []byte:
		return jsonList(string(t))
	default:
		return []any{}
	}
}

func roomConsults(rows []map[string]any) map[string]any {
	answered := 0
	reasons := map[string]int{}
	var cost float64
	for _, row := range rows {
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
	byReason := map[string]any{}
	for name, n := range reasons {
		byReason[name] = n
	}
	list := rows
	if len(list) > 50 {
		list = list[len(list)-50:]
	}
	items := make([]any, 0, len(list))
	for _, row := range list {
		questions := intList(row["questions"])
		if questions == nil {
			questions = []int{}
		}
		var at any
		if row["created_at"] != nil {
			at = row["created_at"]
		}
		items = append(items, map[string]any{
			"seat":          row["seat"],
			"questions":     questions,
			"state":         row["state"],
			"reason":        row["fallback_reason"],
			"read_only":     row["read_only"],
			"denied_writes": deniedWrites(row["denied_writes"]),
			"cost_usd":      row["cost_usd"],
			"at":            at,
		})
	}
	return map[string]any{
		"total":     len(rows),
		"answered":  answered,
		"by_reason": byReason,
		"cost_usd":  roundN(cost, 6),
		"list":      items,
	}
}

func deniedWrites(v any) int {
	if v == nil {
		return 0
	}
	return asInt(v)
}

func messageID(v any) (int64, bool) {
	switch t := v.(type) {
	case int:
		return int64(t), true
	case int32:
		return int64(t), true
	case int64:
		return t, true
	case float64:
		return int64(t), true
	default:
		n, ok := asIntOK(v)
		if !ok {
			return 0, false
		}
		return int64(n), true
	}
}
