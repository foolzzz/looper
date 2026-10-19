package db

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Task detail follows the Python GET /api/tasks/<id> handler: the same SQL,
// then the same view builders. A stored value that no longer validates is
// ignored, the way LayerValues.from_row ignores it. Nothing here reads a
// live WorkerPool. With no agent running, room seats are not in the payload
// (the handler keeps enabled, counts, by_step and consults only).

const profileJSON = `json_build_object('id', a.id, 'name', a.name, 'model', a.model, 'effort', a.effort, 'builtin', a.builtin, ` +
	`'agent_teams', a.agent_teams, 'system_prompt_mode', a.system_prompt_mode, ` +
	`'runtime_id', r.id, 'runtime_name', r.name, 'runtime_type', r.type, 'runtime_executable', r.executable, ` +
	`'runtime_capabilities', r.capabilities, 'provider_id', p.id, 'provider_name', p.name, 'provider_auth', p.auth, ` +
	`'provider_protocols', p.protocols, 'provider_oauth_runtime_type', p.oauth_runtime_type, ` +
	`'provider_base_url', p.base_url, 'provider_models', p.models, 'provider_env', p.env)`

const profileJoin = `agent_profiles a JOIN agent_runtimes r ON r.id = a.runtime_id JOIN llm_providers p ON p.id = a.provider_id`

// Copied from looper/db/agent_setting_layers.py list_layers.
const layerSQL = `SELECT l.scope, l.role, l.project_id, l.harness_prompt, l.profile_id, l.env, l.run_budget_usd, ` +
	`l.max_concurrency, l.boundaries, ` +
	`(SELECT ` + profileJSON + ` FROM ` + profileJoin + ` WHERE a.id = l.profile_id) AS profile ` +
	`FROM agent_setting_layers l WHERE l.project_id IS NULL OR l.project_id = $1`

const taskLayerSQL = `SELECT COALESCE(t.role, c.role) AS role, t.profile_id, ` +
	`(SELECT ` + profileJSON + ` FROM ` + profileJoin + ` WHERE a.id = t.profile_id) AS profile, c.max_concurrency ` +
	`FROM (SELECT * FROM task_profile_overrides WHERE task_id = $1) t ` +
	`FULL JOIN (SELECT * FROM task_concurrency_limits WHERE task_id = $1) c ON c.role = t.role`

const plannerLayerSQL = `SELECT CASE WHEN c.step_id IS NULL THEN 'planner_task' ELSE 'planner_step' END AS scope, c.role, ` +
	`c.profile_id, (SELECT ` + profileJSON + ` FROM ` + profileJoin + ` WHERE a.id = c.profile_id) AS profile ` +
	`FROM task_planner_profiles c JOIN tasks k ON k.id = c.task_id ` +
	`JOIN project_allowed_profiles l ON l.project_id = k.project_id AND l.profile_id = c.profile_id ` +
	`WHERE c.task_id = $1 AND (c.step_id IS NULL OR c.step_id = $2)`

const profileOverridesSQL = `SELECT t.role, (SELECT ` + profileJSON + ` FROM ` + profileJoin + ` WHERE a.id = t.profile_id) AS profile ` +
	`FROM task_profile_overrides t WHERE t.task_id = $1 ORDER BY t.role`

// Copied from looper/db/task_roles.py versions.
const roleVersionsSQL = `SELECT r.task_id, r.role_key, r.version, r.kind, r.profile_id, r.role_prompt, r.harness, r.reason, ` +
	`r.via, r.template_key, r.items, r.created_at, a.name AS profile_name, ` +
	`(l.profile_id IS NOT NULL) AS profile_allowed ` +
	`FROM task_roles r JOIN tasks k ON k.id = r.task_id LEFT JOIN agent_profiles a ON a.id = r.profile_id ` +
	`LEFT JOIN project_allowed_profiles l ON l.project_id = k.project_id AND l.profile_id = r.profile_id ` +
	`WHERE r.task_id = $1 ORDER BY ` +
	`(SELECT min(f.created_at) FROM task_roles f WHERE f.task_id = r.task_id AND f.role_key = r.role_key), ` +
	`r.role_key, r.version`

// Copied from looper/db/user_gates.py _GATES plus list_for_task's order.
const gatesSQL = `SELECT g.*, s.step_key, s.step_index, s.depends_on, ` +
	`EXISTS (SELECT 1 FROM task_user_gate_messages m WHERE m.step_id = g.step_id AND m.role = 'user' ` +
	`AND m.id > GREATEST(g.delivered_through, g.failed_through)) AS waiting_for_planner, ` +
	`(SELECT count(*) FROM task_user_gate_messages m WHERE m.step_id = g.step_id AND m.role = 'user' ` +
	`AND m.id > g.delivered_through AND m.id <= g.failed_through) AS not_delivered, ` +
	`(SELECT count(*) FROM task_user_gate_messages m WHERE m.step_id = g.step_id AND m.post_state = 'sent') AS posts_sent, ` +
	`(SELECT count(*) FROM task_user_gate_messages m WHERE m.step_id = g.step_id AND m.post_state = 'failed') AS posts_failed, ` +
	`(SELECT m.post_error FROM task_user_gate_messages m WHERE m.step_id = g.step_id AND m.post_state = 'failed' ` +
	`ORDER BY m.id DESC LIMIT 1) AS post_last_error ` +
	`FROM task_user_gates g JOIN task_steps s ON s.id = g.step_id ` +
	`WHERE g.task_id = $1 ORDER BY s.step_index, s.created_at`

const (
	concurrencyMax       = 64
	builtinWorkerLimit   = 16
	builtinReviewerLimit = 8
	dashboardTargetKey   = "user_gates.dashboard_target"
	maxSenders           = 100
)

var (
	windowSuffix = regexp.MustCompile(`\[[^\]]*\]$`)
	dateSuffix   = regexp.MustCompile(`-\d{8}$`)
	senderFormat = map[string]struct {
		field string
		re    *regexp.Regexp
	}{
		"telegram": {"allowed_user_ids", regexp.MustCompile(`^[1-9][0-9]{0,19}$`)},
		"lark":     {"allowed_open_ids", regexp.MustCompile(`^ou_[0-9A-Za-z]{8,64}$`)},
	}
	roleNeeds = []struct {
		role  string
		needs []string
	}{
		{"manager", []string{"resident_session", "resume"}},
		{"planner", []string{"resident_session", "resume"}},
		{"worker", []string{"run"}},
		{"reviewer", []string{"run"}},
	}
	claudeAliases = map[string]string{
		"opus":   "claude-opus-5-5",
		"sonnet": "claude-sonnet-5-5",
		"haiku":  "claude-haiku-4-5",
	}
	builtinPrices = map[string][4]float64{
		"claude-fable-5-1":         {10, 50, 0.25, 12.5},
		"claude-opus-5-5":          {4, 20, 0.20, 5},
		"claude-sonnet-5-5":        {2, 10, 0.20, 2.5},
		"claude-haiku-4-5":         {1, 5, 0.10, 1.25},
		"gpt-6.1-sol":              {2, 10, 0.10, 2.5},
		"gpt-6-sol":                {2, 10, 0.20, 2.5},
		"gpt-6-luna":               {0.10, 0.50, 0.01, 0.125},
		"gpt-6-astra":              {10, 50, 1, 12.5},
		"gpt-5.6-sol":              {4, 20, 0.40, 5},
		"gpt-5.6-terra":            {2, 12, 0.20, 2.5},
		"gpt-5.6-luna":             {0.20, 1.20, 0.02, 0.25},
		"gpt-5.5":                  {5, 30, 0.50, 5},
		"gpt-5.4-mini":             {0.75, 4.50, 0.075, 0.75},
		"glm-5.3":                  {1.40, 4.40, 0.26, 1.40},
		"glm-5.3-flash":            {0.15, 0.50, 0.03, 0.15},
		"glm-5.3-flashx":           {0.37, 1.25, 0.075, 0.37},
		"glm-5.2":                  {1.40, 4.40, 0.26, 1.40},
		"glm-5.1":                  {1.40, 4.40, 0.26, 1.40},
		"glm-5":                    {1, 3.20, 0.20, 1},
		"glm-4.7":                  {0.60, 2.20, 0.11, 0.60},
		"glm-4.6":                  {0.60, 2.20, 0.11, 0.60},
		"glm-4.5-air":              {0.20, 1.10, 0.03, 0.20},
		"deepseek-flash":           {0.30, 1.20, 0.006, 0.30},
		"deepseek-v4-pro":          {1.32, 3.96, 0.044, 1.32},
		"kimi-k3":                  {3, 15, 0.30, 3},
		"kimi-k2.7-code":           {0.95, 4, 0.19, 0.95},
		"kimi-k2.7-code-highspeed": {1.90, 8, 0.38, 1.90},
		"kimi-k2.6":                {0.95, 4, 0.16, 0.95},
	}
	retryWait = []time.Duration{60 * time.Second, 300 * time.Second}
)

type layerKey struct {
	scope string
	role  string
}

type layerValues struct {
	profile map[string]any
	limit   *int
}

type gateChat struct {
	channel string
	kind    string
	id      string
}

func (c gateChat) label() string {
	word := "Telegram"
	if c.channel == "lark" {
		word = "Lark"
	}
	group := ""
	if c.channel == "lark" && c.kind == "chat" {
		group = " group"
	}
	return word + group + " " + c.id
}

func (s *Store) profileOverrides(ctx context.Context, taskID string) (map[string]any, error) {
	rows, err := s.query(ctx, "profile overrides", profileOverridesSQL, taskID)
	if err != nil {
		return nil, err
	}
	out := map[string]any{}
	for _, row := range rows {
		profile, ok := asMap(row["profile"])
		if !ok {
			slog.Warn("ignoring task profile override", "task_id", taskID, "role", asString(row["role"]))
			continue
		}
		out[asString(row["role"])] = profileRecord(profile)
	}
	return out, nil
}

func (s *Store) taskAgents(ctx context.Context, projectID, taskID, status string) (map[string]any, error) {
	rows, err := s.query(ctx, "agent setting layers", layerSQL, nullString(projectID))
	if err != nil {
		return nil, err
	}
	taskRows, err := s.query(ctx, "task agent layer", taskLayerSQL, taskID)
	if err != nil {
		return nil, err
	}
	for _, row := range taskRows {
		row["scope"] = "task"
		row["task_id"] = taskID
		rows = append(rows, row)
	}
	// step_id is NULL: list_layers keeps planner choices for the rest of the task only.
	chosen, err := s.query(ctx, "planner profiles", plannerLayerSQL, taskID, nil)
	if err != nil {
		return nil, err
	}
	for _, row := range chosen {
		row["task_id"] = taskID
		rows = append(rows, row)
	}
	return agentsViewFrom(rows, projectID, status), nil
}

func (s *Store) taskRoles(ctx context.Context, taskID string, steps, runs []map[string]any) ([]any, error) {
	versions, err := s.query(ctx, "task roles", roleVersionsSQL, taskID)
	if err != nil {
		return nil, err
	}
	type group struct {
		rows []map[string]any
	}
	var groups []group
	at := map[string]int{}
	for _, row := range versions {
		key := asString(row["role_key"])
		i, ok := at[key]
		if !ok {
			at[key] = len(groups)
			groups = append(groups, group{})
			i = at[key]
		}
		groups[i].rows = append(groups[i].rows, row)
	}
	roles := make([]any, 0, len(groups))
	for _, g := range groups {
		if len(g.rows) == 0 {
			continue
		}
		current := g.rows[len(g.rows)-1]
		kind := asString(current["kind"])
		if kind != "work" && kind != "review" {
			slog.Warn("ignoring task role", "task_id", taskID, "role", asString(current["role_key"]), "kind", kind)
			continue
		}
		key := asString(current["role_key"])
		var matched []map[string]any
		if kind == "work" {
			matched = stepsWithRole(steps, key)
		} else {
			matched = stepsReviewedBy(steps, key)
		}
		items, err := s.namedItems(ctx, textList(current["items"]))
		if err != nil {
			return nil, err
		}
		var profile any
		if current["profile_id"] != nil && asString(current["profile_id"]) != "" {
			profile = map[string]any{
				"profile_id": current["profile_id"],
				"name":       current["profile_name"],
				"allowed":    asBool(current["profile_allowed"]),
			}
		}
		versionList := make([]any, 0, len(g.rows))
		for _, row := range g.rows {
			via := asString(row["via"])
			if via == "" {
				via = "planner"
			}
			versionList = append(versionList, map[string]any{
				"version":    asInt(row["version"]),
				"created_at": row["created_at"],
				"reason":     asString(row["reason"]),
				"via":        via,
				"template":   row["template_key"],
				"sha256":     sha256Text(asString(row["role_prompt"])),
			})
		}
		via := asString(current["via"])
		if via == "" {
			via = "planner"
		}
		var shown []any
		for _, step := range matched {
			if asString(step["status"]) == "skipped" {
				continue
			}
			label := asString(step["step_key"])
			if label == "" {
				label = asString(step["id"])
			}
			shown = append(shown, label)
		}
		if shown == nil {
			shown = []any{}
		}
		roles = append(roles, map[string]any{
			"id":          key,
			"kind":        kind,
			"version":     asInt(current["version"]),
			"profile":     profile,
			"role_prompt": asString(current["role_prompt"]),
			"harness":     current["harness"],
			"reason":      asString(current["reason"]),
			"created_at":  current["created_at"],
			"via":         via,
			"template":    current["template_key"],
			"items":       items,
			"versions":    versionList,
			"steps":       shown,
			"stats":       roleStats(runsOf(runs, workerOrReviewer(kind), key), matched, reviewsOf(runs, steps, kind, key)),
		})
	}
	return roles, nil
}

func workerOrReviewer(kind string) string {
	if kind == "review" {
		return "reviewer"
	}
	return "worker"
}

func stepsWithRole(steps []map[string]any, key string) []map[string]any {
	var out []map[string]any
	for _, step := range steps {
		if asString(step["role_key"]) == key && step["role_key"] != nil {
			out = append(out, step)
		}
	}
	return out
}

func stepsReviewedBy(steps []map[string]any, key string) []map[string]any {
	var out []map[string]any
	for _, step := range steps {
		for _, role := range reviewRoleKeys(step) {
			if role == key {
				out = append(out, step)
				break
			}
		}
	}
	return out
}

func reviewsOf(runs, steps []map[string]any, kind, key string) []map[string]any {
	if kind == "review" {
		return runsOf(runs, "reviewer", key)
	}
	roleOf := map[string]any{}
	for _, step := range steps {
		roleOf[asString(step["id"])] = step["role_key"]
	}
	var out []map[string]any
	for _, row := range runsOf(runs, "reviewer", "") {
		if roleOf[asString(row["step_id"])] == key {
			out = append(out, row)
		}
	}
	return out
}

func runsOf(runs []map[string]any, role, key string) []map[string]any {
	var out []map[string]any
	for _, row := range runs {
		if asString(row["role"]) == role && asString(row["task_role"]) == key {
			out = append(out, row)
		}
	}
	return out
}

func reviewRoleKeys(step map[string]any) []string {
	items, ok := step["review_roles"].([]any)
	if !ok {
		return nil
	}
	var keys []string
	for _, item := range items {
		m, ok := item.(map[string]any)
		if !ok || m["role"] == nil {
			continue
		}
		keys = append(keys, fmt.Sprint(m["role"]))
	}
	return keys
}

func (s *Store) namedItems(ctx context.Context, ids []string) ([]any, error) {
	if len(ids) == 0 {
		return []any{}, nil
	}
	rows, err := s.query(ctx, "library items", `SELECT id, kind, name FROM library_items WHERE id = ANY($1::text[])`, ids)
	if err != nil {
		return nil, err
	}
	known := map[string]map[string]any{}
	for _, row := range rows {
		known[asString(row["id"])] = row
	}
	out := make([]any, 0, len(ids))
	for _, id := range ids {
		var kind, name any
		if row := known[id]; row != nil {
			kind, name = row["kind"], row["name"]
		}
		out = append(out, map[string]any{"id": id, "kind": kind, "name": name})
	}
	return out, nil
}

func (s *Store) taskGates(ctx context.Context, task map[string]any) ([]any, error) {
	id := asString(task["id"])
	rows, err := s.query(ctx, "gates", gatesSQL, id)
	if err != nil {
		return nil, err
	}
	var chat *gateChat
	if len(rows) > 0 {
		found, ok, err := s.gateChat(ctx, task)
		if err != nil {
			return nil, err
		}
		if ok {
			chat = &found
		}
	}
	out := make([]any, 0, len(rows))
	now := time.Now().UTC()
	for _, row := range rows {
		out = append(out, gateRecord(row, chat, now))
	}
	return out, nil
}

func (s *Store) gateChat(ctx context.Context, task map[string]any) (gateChat, bool, error) {
	if asString(task["schedule_id"]) != "" {
		return gateChat{}, false, nil
	}
	if channel, ok := originChannel(asString(task["origin_channel_id"])); ok {
		kind, id, ok := notifyTarget(task["chat_kind"], task["chat_id"])
		if !ok {
			return gateChat{}, false, nil
		}
		return gateChat{channel: channel, kind: kind, id: id}, true, nil
	}
	row, err := s.one(ctx, "dashboard gate target", `SELECT value FROM system_settings WHERE key = $1`, dashboardTargetKey)
	if err != nil {
		return gateChat{}, false, err
	}
	if row == nil {
		return gateChat{}, false, nil
	}
	chat, ok := dashboardChat(asString(row["value"]))
	if !ok {
		return gateChat{}, false, nil
	}
	configs, err := s.query(ctx, "channel configs", `SELECT channel_type, enabled, config FROM channel_configs`)
	if err != nil {
		return gateChat{}, false, err
	}
	if !offered(chat, notifyTargets(configs)) {
		return gateChat{}, false, nil
	}
	return chat, true, nil
}

func originChannel(id string) (string, bool) {
	switch id {
	case "channel-lark":
		return "lark", true
	case "channel-telegram":
		return "telegram", true
	default:
		return "", false
	}
}

func notifyTarget(kind, chat any) (string, string, bool) {
	k, ok := kind.(string)
	if !ok || (k != "user" && k != "chat") {
		return "", "", false
	}
	id, ok := chat.(string)
	if !ok || id == "" {
		return "", "", false
	}
	return k, id, true
}

func dashboardChat(raw string) (gateChat, bool) {
	if raw == "" {
		return gateChat{}, false
	}
	var value map[string]any
	if json.Unmarshal([]byte(raw), &value) != nil || value == nil {
		return gateChat{}, false
	}
	channel := asString(value["channel"])
	chat, ok := value["chat"].(string)
	if !ok || chat == "" || (channel != "lark" && channel != "telegram") {
		return gateChat{}, false
	}
	kind := "chat"
	if channel == "lark" {
		kind = "user"
	}
	return gateChat{channel: channel, kind: kind, id: chat}, true
}

type chatTarget struct {
	channel string
	chat    string
}

func notifyTargets(rows []map[string]any) []chatTarget {
	var out []chatTarget
	for _, row := range rows {
		channel := asString(row["channel_type"])
		fmtSpec, known := senderFormat[channel]
		if !known {
			continue
		}
		raw := any(nil)
		if cfg, ok := asMap(row["config"]); ok {
			raw = cfg[fmtSpec.field]
		}
		ids, err := senderIDs(channel, raw)
		if err != nil {
			continue
		}
		for _, id := range ids {
			out = append(out, chatTarget{channel: channel, chat: id})
		}
	}
	return out
}

func offered(chat gateChat, targets []chatTarget) bool {
	for _, target := range targets {
		if target.channel == chat.channel && target.chat == chat.id {
			return true
		}
	}
	return false
}

func senderIDs(channel string, raw any) ([]string, error) {
	spec, ok := senderFormat[channel]
	if !ok {
		return nil, fmt.Errorf("unknown channel")
	}
	var entries []any
	switch t := raw.(type) {
	case nil:
	case string:
		for _, part := range strings.Split(t, ",") {
			entries = append(entries, part)
		}
	case []any:
		entries = t
	case []string:
		for _, part := range t {
			entries = append(entries, part)
		}
	default:
		return nil, fmt.Errorf("%s must be a list", spec.field)
	}
	seen := map[string]struct{}{}
	var ids []string
	for _, entry := range entries {
		text, ok := senderText(channel, entry)
		if !ok {
			return nil, fmt.Errorf("%s: bad entry", spec.field)
		}
		text = strings.TrimSpace(text)
		if text == "" {
			continue
		}
		if !spec.re.MatchString(text) {
			return nil, fmt.Errorf("%s: %q does not match", spec.field, text)
		}
		if _, ok := seen[text]; ok {
			continue
		}
		seen[text] = struct{}{}
		ids = append(ids, text)
	}
	if len(ids) > maxSenders {
		return nil, fmt.Errorf("%s lists at most %d senders", spec.field, maxSenders)
	}
	return ids, nil
}

func senderText(channel string, entry any) (string, bool) {
	switch t := entry.(type) {
	case string:
		return t, true
	case int, int32, int64:
		if channel != "telegram" {
			return "", false
		}
		n, ok := wholeInt(entry)
		if !ok {
			return "", false
		}
		return strconv.Itoa(n), true
	case float64:
		if channel != "telegram" || math.IsNaN(t) || math.IsInf(t, 0) || t != math.Trunc(t) {
			return "", false
		}
		n := int64(t)
		if float64(n) != t {
			return "", false
		}
		return strconv.FormatInt(n, 10), true
	default:
		return "", false
	}
}

func (s *Store) stepWorkspaces(ctx context.Context, taskID string, steps []map[string]any, inWorktree bool) (any, error) {
	rows, err := s.query(ctx, "step workspaces", `SELECT * FROM step_workspaces WHERE task_id = $1 ORDER BY id`, taskID)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	start, err := s.one(ctx, "task start", `SELECT start_branch, start_commit FROM task_plans WHERE task_id = $1`, taskID)
	if err != nil {
		return nil, err
	}
	startBranch, startCommit := "", ""
	if start != nil {
		startBranch = asString(start["start_branch"])
		startCommit = asString(start["start_commit"])
	}
	keys := map[string]string{}
	for _, step := range steps {
		keys[asString(step["id"])] = asString(step["step_key"])
	}
	found := make([]any, 0, len(rows))
	var merged [][2]string
	for _, row := range rows {
		view := stepWorkspaceRecord(row)
		label := keys[asString(row["step_id"])]
		view["step_key"] = label
		found = append(found, view)
		if asString(row["state"]) == "merged" && row["merge_commit"] != nil && asString(row["merge_commit"]) != "" {
			if label == "" {
				label = asString(row["step_id"])
			}
			merged = append(merged, [2]string{label, asString(row["merge_commit"])})
		}
	}
	undoBranch := startBranch
	if undoBranch == "" {
		undoBranch = asString(rows[0]["target_branch"])
	}
	undo := undoCommands(taskID, undoBranch, startCommit, merged)
	if inWorktree {
		undo = []any{}
	}
	var started any
	if startCommit != "" {
		started = map[string]any{"branch": shortBranch(startBranch), "commit": startCommit}
	}
	return map[string]any{"start": started, "workspaces": found, "undo": undo}, nil
}

func stepWorkspaceRecord(row map[string]any) map[string]any {
	state := asString(row["state"])
	return map[string]any{
		"id":            asInt(row["id"]),
		"step_id":       row["step_id"],
		"branch":        row["branch"],
		"target_branch": shortBranch(asString(row["target_branch"])),
		"base_commit":   row["base_commit"],
		"checkpoint":    asBool(row["checkpoint"]),
		"result_commit": row["result_commit"],
		"merge_commit":  row["merge_commit"],
		"state":         state,
		"merge_error":   row["merge_error"],
		"merge_files":   stringList(row["merge_files"]),
		"detail":        asString(row["detail"]),
		"links":         stringList(row["links"]),
		"path":          row["path"],
		"removed_at":    row["removed_at"],
		"discardable":   row["removed_at"] == nil && state != "merging" && state != "merged",
	}
}

func shortBranch(ref string) string {
	return strings.TrimPrefix(ref, "refs/heads/")
}

func undoCommands(taskID, startBranch, startCommit string, merged [][2]string) []any {
	branch := shortBranch(startBranch)
	on := ""
	if branch != "" {
		on = "git switch " + shlexQuote(branch) + " && "
	}
	list := "git log --first-parent --grep=" + shlexQuote("^Looper-Task: "+taskID+"$")
	if branch != "" {
		list += " " + shlexQuote(branch)
	}
	where := branch
	if where == "" {
		where = "the branch"
	}
	out := []any{map[string]any{
		"what":    "List the task's commits on " + where,
		"command": list,
	}}
	for _, item := range merged {
		out = append(out, map[string]any{
			"what":    "Undo step " + item[0],
			"command": on + "git revert -m 1 " + item[1],
		})
	}
	if startCommit != "" && branch != "" {
		out = append(out,
			map[string]any{
				"what":    "Undo the whole task, keeping its changes as uncommitted changes",
				"command": on + "git reset " + startCommit,
			},
			map[string]any{
				"what":    "Undo the whole task and drop its changes (and any other uncommitted change)",
				"command": on + "git reset --hard " + startCommit,
			},
			map[string]any{
				"what": "Move the task's commits to a new branch and put " + branch + " back where the task started",
				"command": "git switch -c " + shlexQuote(branch+"-"+taskID) + " " + shlexQuote(branch) +
					" && git branch -f " + shlexQuote(branch) + " " + startCommit,
			},
		)
	}
	return out
}

func gateRecord(row map[string]any, chat *gateChat, now time.Time) map[string]any {
	state := asString(row["state"])
	waiting := state == "open" || state == "discussing"
	var waitingOn any
	var retry any
	if waiting {
		retry = retryAt(asInt(row["failures"]), row["failed_at"])
		retrying := false
		if text, ok := retry.(string); ok {
			if when, ok := parsePyTime(text); ok && !when.After(now) {
				retrying = true
			}
		}
		if asBool(row["waiting_for_planner"]) || retrying {
			waitingOn = "planner"
		} else {
			waitingOn = "user"
		}
	}
	notDelivered := 0
	if waiting {
		notDelivered = asInt(row["not_delivered"])
	}
	var posts any
	if chat != nil {
		posts = map[string]any{
			"channel":    chat.channel,
			"target":     chat.label(),
			"sent":       asInt(row["posts_sent"]),
			"failed":     asInt(row["posts_failed"]),
			"last_error": row["post_last_error"],
		}
	}
	return map[string]any{
		"step":           row["step_key"],
		"step_id":        row["step_id"],
		"state":          state,
		"topic":          row["topic"],
		"agenda":         jsonList(row["agenda"]),
		"findings":       strOr(row["findings"], ""),
		"summary":        jsonObject(row["summary"]),
		"resolution":     strOr(row["resolution"], ""),
		"ended_by_user":  asBool(row["ended_by_user"]),
		"plan_change":    jsonObject(row["plan_change"]),
		"depends_on":     stringList(row["depends_on"]),
		"waiting_on":     waitingOn,
		"turns":          asInt(row["turns"]),
		"not_delivered":  notDelivered,
		"retry_at":       retry,
		"reminders":      asInt(row["reminders"]),
		"opened_at":      row["opened_at"],
		"first_reply_at": row["first_reply_at"],
		"closed_at":      row["closed_at"],
		"channel_posts":  posts,
	}
}

func retryAt(failures int, failedAt any) any {
	if failures <= 0 || failures > len(retryWait) || failedAt == nil {
		return nil
	}
	when, ok := parsePyTime(asString(failedAt))
	if !ok {
		return nil
	}
	return pyTime(when.Add(retryWait[failures-1]))
}

func parsePyTime(text string) (time.Time, bool) {
	if text == "" {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339Nano, text)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

func jsonList(v any) []any {
	switch t := v.(type) {
	case nil:
		return []any{}
	case []any:
		if t == nil {
			return []any{}
		}
		return t
	case string:
		var out []any
		if json.Unmarshal([]byte(t), &out) == nil && out != nil {
			return out
		}
		return []any{}
	default:
		return []any{}
	}
}

func jsonObject(v any) any {
	if s, ok := v.(string); ok {
		var out any
		if json.Unmarshal([]byte(s), &out) == nil {
			return out
		}
	}
	return v
}

func roomByStep(steps, messages []map[string]any) map[string]any {
	type seat struct{ key, role, step string }
	var seats []seat
	for _, step := range steps {
		if asString(step["kind"]) == "gate" {
			continue
		}
		label := asString(step["step_key"])
		if label == "" {
			label = asString(step["id"])
		}
		role := asString(step["role_key"])
		if step["role_key"] == nil || role == "" {
			role = "worker"
		}
		seats = append(seats, seat{role + "@" + label, role, label})
		if asBool(step["review"]) || (step["reviewer_id"] != nil && asString(step["reviewer_id"]) != "") {
			seats = append(seats, seat{"reviewer@" + label, "reviewer", label})
		}
	}
	answered := answeredIDs(messages)
	out := map[string]any{}
	seen := map[string]bool{}
	var order []string
	for _, seat := range seats {
		if !seen[seat.step] {
			seen[seat.step] = true
			order = append(order, seat.step)
		}
	}
	for _, label := range order {
		keys := map[string]bool{}
		roles := map[string]bool{}
		for _, seat := range seats {
			if seat.step == label {
				keys[seat.key] = true
				roles[seat.role] = true
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
			if asString(row["kind"]) == "question" && !answered[asInt(row["id"])] && (isSent || isReceived) {
				unanswered++
			}
		}
		if sent != 0 || received != 0 || unanswered != 0 {
			out[label] = map[string]any{"sent": sent, "received": received, "unanswered": unanswered}
		}
	}
	return out
}

func answeredIDs(messages []map[string]any) map[int]bool {
	out := map[int]bool{}
	for _, row := range messages {
		if asString(row["kind"]) != "answer" {
			continue
		}
		if row["reply_to"] != nil {
			out[asInt(row["reply_to"])] = true
		}
		for _, id := range intList(row["answers_ids"]) {
			out[id] = true
		}
	}
	return out
}

func stringSet(v any) map[string]bool {
	out := map[string]bool{}
	switch t := v.(type) {
	case []string:
		for _, s := range t {
			out[s] = true
		}
	case []any:
		for _, item := range t {
			if s, ok := item.(string); ok {
				out[s] = true
			}
		}
	}
	return out
}

func setIntersects(audience map[string]bool, keys, roles map[string]bool) bool {
	for name := range audience {
		if keys[name] || roles[name] {
			return true
		}
	}
	return false
}

func intList(v any) []int {
	switch t := v.(type) {
	case []int:
		return t
	case []int32:
		out := make([]int, len(t))
		for i, n := range t {
			out[i] = int(n)
		}
		return out
	case []int64:
		out := make([]int, len(t))
		for i, n := range t {
			out[i] = int(n)
		}
		return out
	case []any:
		out := make([]int, 0, len(t))
		for _, item := range t {
			if item != nil {
				out = append(out, asInt(item))
			}
		}
		return out
	default:
		return nil
	}
}

func textList(v any) []string {
	switch t := v.(type) {
	case []string:
		return t
	case []any:
		out := make([]string, 0, len(t))
		for _, item := range t {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	default:
		return nil
	}
}

func agentsViewFrom(rows []map[string]any, projectID, status string) map[string]any {
	layers := indexLayers(rows, projectID)
	roles := map[string]any{}
	for _, role := range []string{"planner", "worker", "reviewer"} {
		value, source := resolveProfile(role, layers, projectID)
		entry := map[string]any{"profile": map[string]any{"value": value, "source": source}}
		if role == "worker" || role == "reviewer" {
			entry["concurrency"] = concurrencyView(role, layers)
		}
		roles[role] = entry
	}
	return map[string]any{
		"roles":           roles,
		"concurrency_max": concurrencyMax,
		"editable":        agentsEditable(status),
	}
}

func indexLayers(rows []map[string]any, projectID string) map[layerKey]layerValues {
	index := map[layerKey]layerValues{}
	for _, row := range rows {
		if row["project_id"] != nil && asString(row["project_id"]) != projectID {
			continue
		}
		scope := asString(row["scope"])
		if !knownLayer(scope) {
			continue
		}
		role := ""
		if row["role"] != nil {
			role = asString(row["role"])
			if !knownAgent(role) {
				continue
			}
		}
		index[layerKey{scope, role}] = layerFromRow(row)
	}
	return index
}

func knownLayer(scope string) bool {
	switch scope {
	case "builtin", "profile", "system", "role", "project", "role_project",
		"planner_task", "planner_step", "task_role", "task":
		return true
	default:
		return false
	}
}

func knownAgent(role string) bool {
	switch role {
	case "manager", "planner", "worker", "reviewer":
		return true
	default:
		return false
	}
}

func layerFromRow(row map[string]any) layerValues {
	var values layerValues
	place := layerPlace(row)
	if row["profile"] != nil {
		if profile, ok := asMap(row["profile"]); ok {
			if choice, ok := profileChoice(profile); ok {
				values.profile = choice
			} else {
				slog.Warn("ignoring stored agent profile", "place", place)
			}
		} else {
			slog.Warn("ignoring stored agent profile", "place", place)
		}
	}
	if row["max_concurrency"] != nil {
		n, ok := wholeInt(row["max_concurrency"])
		if ok && n >= 1 && n <= concurrencyMax {
			values.limit = &n
		} else {
			slog.Warn("ignoring stored max_concurrency", "place", place)
		}
	}
	return values
}

func layerPlace(row map[string]any) string {
	var parts []string
	for _, key := range []string{"scope", "role", "project_id", "task_id"} {
		if row[key] != nil && asString(row[key]) != "" {
			parts = append(parts, asString(row[key]))
		}
	}
	return strings.Join(parts, "/")
}

func profileChoice(raw map[string]any) (map[string]any, bool) {
	for _, key := range []string{"id", "name", "model", "runtime_id", "runtime_name", "runtime_type", "provider_id", "provider_name", "provider_auth"} {
		text, ok := raw[key].(string)
		if !ok || text == "" {
			return nil, false
		}
	}
	switch asString(raw["provider_auth"]) {
	case "oauth", "api_key":
	default:
		return nil, false
	}
	if mode, ok := raw["system_prompt_mode"]; ok && mode != nil {
		text, ok := mode.(string)
		if !ok || (text != "append" && text != "replace") {
			return nil, false
		}
	}
	if _, ok := raw["agent_teams"].(bool); raw["agent_teams"] != nil && !ok {
		return nil, false
	}
	var effort any
	if raw["effort"] != nil {
		if _, ok := raw["effort"].(string); !ok {
			return nil, false
		}
		effort = raw["effort"]
	}
	return map[string]any{
		"profile_id": raw["id"],
		"name":       raw["name"],
		"model":      raw["model"],
		"effort":     effort,
		"runtime":    raw["runtime_name"],
		"provider":   raw["provider_name"],
	}, true
}

func builtinProfile() map[string]any {
	return map[string]any{
		"profile_id": "agent-claude-code-opus",
		"name":       "claude-code/opus",
		"model":      "opus",
		"effort":     nil,
		"runtime":    "claude-code",
		"provider":   "claude-subscription",
	}
}

func resolveProfile(role string, layers map[layerKey]layerValues, projectID string) (map[string]any, string) {
	chosen := builtinProfile()
	source := "builtin"
	for _, key := range chain(role, projectID) {
		if values, ok := layers[key]; ok && values.profile != nil {
			chosen = values.profile
			source = key.scope
		}
	}
	return chosen, source
}

func chain(role, projectID string) []layerKey {
	keys := []layerKey{{"system", ""}, {"role", role}}
	if projectID != "" && role != "manager" {
		keys = append(keys, layerKey{"project", ""}, layerKey{"role_project", role})
	}
	if role == "worker" || role == "reviewer" {
		keys = append(keys, layerKey{"planner_task", role}, layerKey{"planner_step", role}, layerKey{"task_role", role})
	}
	if role != "manager" {
		keys = append(keys, layerKey{"task", role})
	}
	return keys
}

func concurrencyView(role string, layers map[layerKey]layerValues) map[string]any {
	value, source := defaultConcurrency(role, layers)
	var own any
	if row, ok := layers[layerKey{"task", role}]; ok && row.limit != nil {
		value = *row.limit
		source = "task"
		own = *row.limit
	}
	fallback, fallbackSource := defaultConcurrency(role, layers)
	return map[string]any{
		"value":  value,
		"source": source,
		"task":   own,
		"default": map[string]any{
			"value":  fallback,
			"source": fallbackSource,
		},
	}
}

func defaultConcurrency(role string, layers map[layerKey]layerValues) (int, string) {
	if row, ok := layers[layerKey{"role", role}]; ok && row.limit != nil {
		return *row.limit, "role"
	}
	if role == "reviewer" {
		return builtinReviewerLimit, "builtin"
	}
	return builtinWorkerLimit, "builtin"
}

func profileRecord(row map[string]any) map[string]any {
	mode := "append"
	if text := asString(row["system_prompt_mode"]); text != "" {
		mode = text
	}
	return map[string]any{
		"profile_id":         row["id"],
		"name":               row["name"],
		"model":              row["model"],
		"effort":             row["effort"],
		"runtime_id":         row["runtime_id"],
		"runtime_name":       row["runtime_name"],
		"runtime_type":       row["runtime_type"],
		"provider_id":        row["provider_id"],
		"provider_name":      row["provider_name"],
		"builtin":            asBool(row["builtin"]),
		"agent_teams":        asBool(row["agent_teams"]),
		"system_prompt_mode": mode,
		"roles":              servableRoles(textList(row["runtime_capabilities"])),
		"used_by": map[string]any{
			"layers":         []any{},
			"task_count":     0,
			"schedule_count": 0,
			"allowed_in":     []any{},
		},
		"cost": costBasis(row),
	}
}

func servableRoles(capabilities []string) []any {
	have := map[string]bool{}
	for _, cap := range capabilities {
		have[cap] = true
	}
	out := []any{}
	for _, role := range roleNeeds {
		ok := true
		for _, need := range role.needs {
			if !have[need] {
				ok = false
				break
			}
		}
		if ok {
			out = append(out, role.role)
		}
	}
	return out
}

func costBasis(row map[string]any) map[string]any {
	source, known := costSource(asString(row["runtime_type"]), asString(row["provider_auth"]), asString(row["provider_base_url"]))
	if !known {
		return map[string]any{"source": nil, "equivalent": false, "unpriced": []any{}}
	}
	unpriced := []any{}
	if source == "provider_price" {
		unpriced = unpricedModels(row)
	}
	return map[string]any{
		"source":     source,
		"equivalent": asString(row["provider_auth"]) == "oauth",
		"unpriced":   unpriced,
	}
}

func costSource(runtimeType, auth, baseURL string) (string, bool) {
	switch runtimeType {
	case "claude-code":
		if auth == "oauth" || officialEndpoint(baseURL) {
			return "runtime", true
		}
		return "provider_price", true
	case "grok":
		return "runtime", true
	case "codex", "omp":
		return "provider_price", true
	default:
		return "", false
	}
}

func officialEndpoint(baseURL string) bool {
	if baseURL == "" {
		return true
	}
	parsed, err := url.Parse(baseURL)
	if err != nil {
		return false
	}
	return strings.EqualFold(parsed.Hostname(), "api.anthropic.com")
}

func unpricedModels(row map[string]any) []any {
	models := catalogModels(row["provider_models"])
	names := []string{asString(row["model"])}
	if entry := exactCatalog(models, names[0]); entry != nil {
		if bg, ok := entry["background_model"].(string); ok && bg != "" {
			names = append(names, bg)
		}
	}
	seen := map[string]bool{}
	var out []any
	for _, name := range names {
		if seen[name] {
			continue
		}
		seen[name] = true
		if !priceComplete(row, models, name) {
			out = append(out, name)
		}
	}
	if out == nil {
		out = []any{}
	}
	return out
}

func priceComplete(row map[string]any, models []map[string]any, model string) bool {
	base := baseModel(model)
	if asString(row["provider_auth"]) == "oauth" && asString(row["provider_oauth_runtime_type"]) == "claude-code" {
		if alias, ok := claudeAliases[base]; ok {
			base = alias
		}
	}
	var prices [4]*float64
	if builtin, ok := builtinPrices[base]; ok {
		for i := range builtin {
			value := builtin[i]
			prices[i] = &value
		}
	}
	entry := exactCatalog(models, model)
	if entry == nil {
		for _, item := range models {
			if baseModel(asString(item["name"])) == base {
				entry = item
				break
			}
		}
	}
	if entry != nil {
		for i, field := range []string{"input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok"} {
			if entry[field] == nil {
				continue
			}
			if value, ok := asFloat(entry[field]); ok {
				prices[i] = &value
			}
		}
	}
	for _, price := range prices {
		if price == nil {
			return false
		}
	}
	return true
}

func catalogModels(raw any) []map[string]any {
	items, ok := raw.([]any)
	if !ok {
		return nil
	}
	var out []map[string]any
	for _, item := range items {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func exactCatalog(models []map[string]any, name string) map[string]any {
	for _, item := range models {
		if asString(item["name"]) == name {
			return item
		}
	}
	return nil
}

func baseModel(name string) string {
	name = strings.TrimSpace(name)
	name = windowSuffix.ReplaceAllString(name, "")
	name = dateSuffix.ReplaceAllString(name, "")
	return strings.ToLower(name)
}

func sha256Text(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

func asMap(v any) (map[string]any, bool) {
	if m, ok := v.(map[string]any); ok && m != nil {
		return m, true
	}
	s, ok := v.(string)
	if !ok || s == "" {
		return nil, false
	}
	var m map[string]any
	if json.Unmarshal([]byte(s), &m) != nil || m == nil {
		return nil, false
	}
	return m, true
}

func taskBudgetUSD(projectID string, raw any) any {
	if raw == nil || raw == "" {
		return nil
	}
	value, err := parseBudget("task_budget_usd", raw)
	if err != nil {
		slog.Warn("ignoring task budget", "project_id", projectID, "reason", err.Error())
		return nil
	}
	return value
}
