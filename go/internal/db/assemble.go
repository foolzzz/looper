package db

import (
	"encoding/json"
	"math"
	"sort"
	"strings"
	"unicode"
)

// Task groups, in the order Python's TaskGroup enum and the 400 message use.
const (
	groupNeedsYou   = "needs_you"
	groupInProgress = "in_progress"
	groupReady      = "ready"
	groupAttention  = "attention"
	groupCompleted  = "completed"
	groupCancelled  = "cancelled"
	groupOther      = "other"
)

var taskGroupOrder = []string{
	groupNeedsYou, groupInProgress, groupReady, groupAttention,
	groupCompleted, groupCancelled, groupOther,
}

// statusGroup is Python's STATUS_GROUPS insertion order.
var statusGroup = []struct{ status, group string }{
	{"assigned", groupInProgress},
	{"running", groupInProgress},
	{"planning", groupInProgress},
	{"executing", groupInProgress},
	{"verifying", groupInProgress},
	{"stopping", groupInProgress},
	{"waiting_user", groupNeedsYou},
	{"pending", groupInProgress},
	{"init", groupReady},
	{"failed", groupAttention},
	{"stopped", groupAttention},
	{"completed", groupCompleted},
	{"cancelled", groupCancelled},
}

// activeStatuses is needs_you and in_progress, in dict order.
var activeStatuses = []string{
	"assigned", "running", "planning", "executing", "verifying", "stopping", "waiting_user", "pending",
}

var knownStatus = func() map[string]string {
	out := make(map[string]string, len(statusGroup))
	for _, row := range statusGroup {
		out[row.status] = row.group
	}
	return out
}()

func taskGroup(status string) string {
	if g, ok := knownStatus[status]; ok {
		return g
	}
	return groupOther
}

// StatusesOf is the Tasks page group filter: the statuses a row must be in,
// or, for other, the known statuses it must not be in.
func StatusesOf(group string) ([]string, bool) {
	return statusesOf(group)
}

func statusesOf(group string) (statuses []string, inside bool) {
	if group == "" {
		return nil, true
	}
	if group == groupOther {
		all := make([]string, len(statusGroup))
		for i, row := range statusGroup {
			all[i] = row.status
		}
		return all, false
	}
	var match []string
	for _, row := range statusGroup {
		if row.group == group {
			match = append(match, row.status)
		}
	}
	return match, true
}

func groupCounts(rows []map[string]any) map[string]any {
	counts := map[string]int{}
	for _, g := range taskGroupOrder {
		counts[g] = 0
	}
	other := map[string]int{}
	for _, row := range rows {
		if row["status"] == nil {
			continue
		}
		status := asString(row["status"])
		g := taskGroup(status)
		n := asInt(row["count"])
		counts[g] += n
		if g == groupOther {
			other[status] += n
		}
	}
	total := 0
	out := map[string]any{}
	for _, g := range taskGroupOrder {
		out[g] = counts[g]
		total += counts[g]
	}
	out["total"] = total
	out["other_statuses"] = other
	return out
}

func projectView(row map[string]any) map[string]any {
	out := map[string]any{
		"project_id":     row["id"],
		"name":           row["name"],
		"description":    row["description"],
		"work_dir":       row["work_dir"],
		"background":     row["background"],
		"created_at":     row["created_at"],
		"updated_at":     row["updated_at"],
		"task_count":     asInt(row["task_count"]),
		"active_count":   asInt(row["active_count"]),
		"schedule_count": asInt(row["schedule_count"]),
	}
	for key, val := range executionView(row) {
		out[key] = val
	}
	return out
}

func taskName(title, goal, requirement, id string) string {
	for _, value := range []string{title, goal} {
		if pythonStrip(value) != "" {
			return strings.Join(pythonFields(value), " ")
		}
	}
	first := pythonStrip(requirement)
	if i := strings.IndexByte(first, '\n'); i >= 0 {
		first = pythonStrip(first[:i])
	}
	if first != "" {
		return first
	}
	return id
}

// pythonSpace is str.isspace. unicode.IsSpace omits U+001C–U+001F. Python
// treats those controls as whitespace, so a title of only them is empty and
// falls through, and one with them between words collapses to single spaces.
func pythonSpace(r rune) bool {
	return unicode.IsSpace(r) || r >= 0x1C && r <= 0x1F
}

func pythonStrip(s string) string {
	return strings.TrimFunc(s, pythonSpace)
}

func pythonFields(s string) []string {
	return strings.FieldsFunc(s, pythonSpace)
}

func taskListItem(row map[string]any, cost map[string]any) map[string]any {
	id := asString(row["id"])
	status := strOr(row["status"], "pending")
	return map[string]any{
		"task_id":        id,
		"requirement":    asString(row["requirement"]),
		"name":           taskName(asString(row["title"]), asString(row["goal_id"]), asString(row["requirement"]), id),
		"goal_id":        asString(row["goal_id"]),
		"project_id":     asString(row["project_id"]),
		"project_name":   asString(row["project_name"]),
		"work_dir":       asString(row["work_dir"]),
		"status":         status,
		"group":          taskGroup(status),
		"phase":          strOr(row["phase"], "pending"),
		"planner_id":     row["planner_id"],
		"correlation_id": asString(row["correlation_id"]),
		"created_at":     row["created_at"],
		"completed_at":   row["completed_at"],
		"schedule_id":    row["schedule_id"],
		"schedule_name":  row["schedule_name"],
		"cost":           cost,
	}
}

func stepRow(row map[string]any) map[string]any {
	out := make(map[string]any, len(row))
	for k, v := range row {
		out[k] = v
	}
	for _, name := range []string{"result", "review_roles"} {
		if s, ok := out[name].(string); ok {
			var decoded any
			if json.Unmarshal([]byte(s), &decoded) == nil {
				out[name] = decoded
			}
		}
	}
	for _, name := range []string{"target_files", "validation_commands", "acceptance_criteria", "depends_on", "feedback", "missed_messages"} {
		out[name] = stringList(out[name])
	}
	return out
}

func journalRow(row map[string]any) map[string]any {
	out := make(map[string]any, len(row))
	for k, v := range row {
		out[k] = v
	}
	out["learnings"] = stringList(out["learnings"])
	return out
}

func stringList(v any) []any {
	switch t := v.(type) {
	case nil:
		return []any{}
	case []any:
		if t == nil {
			return []any{}
		}
		return t
	case []string:
		out := make([]any, len(t))
		for i, s := range t {
			out[i] = s
		}
		return out
	default:
		return []any{}
	}
}

func readConfig(v any) map[string]any {
	for range 8 {
		if m, ok := v.(map[string]any); ok {
			if m == nil {
				return map[string]any{}
			}
			return m
		}
		s, ok := v.(string)
		if !ok {
			break
		}
		if err := json.Unmarshal([]byte(s), &v); err != nil {
			break
		}
	}
	return map[string]any{}
}

func supplementsOf(journal []map[string]any) []any {
	out := []any{}
	for _, row := range journal {
		if asString(row["kind"]) != "supplement" {
			continue
		}
		out = append(out, map[string]any{
			"text":         row["summary"],
			"via":          asString(row["via"]),
			"created_at":   row["created_at"],
			"delivered_at": row["delivered_at"],
		})
	}
	return out
}

func progressNotes(journal []map[string]any) []any {
	out := []any{}
	for i := len(journal) - 1; i >= 0; i-- {
		row := journal[i]
		if asString(row["kind"]) != "progress" {
			continue
		}
		note := progressNote(asString(row["summary"]))
		if note == nil {
			continue
		}
		note["at"] = row["created_at"]
		out = append(out, note)
	}
	return out
}

func progressNote(text string) map[string]any {
	var raw map[string]any
	if json.Unmarshal([]byte(text), &raw) != nil {
		return nil
	}
	out := map[string]any{}
	for _, name := range []string{"done", "now", "next", "risks", "estimate"} {
		s, ok := raw[name].(string)
		if !ok {
			return nil
		}
		out[name] = s
	}
	return out
}

// agentsView is the empty-layers path of agent_settings.task_agents_view:
// the built-in Claude Code profile, and the built-in worker and reviewer limits.
// Setting layers and task profile overrides are not applied.
func agentsView(status string) map[string]any {
	profile := func() map[string]any {
		return map[string]any{
			"value": map[string]any{
				"profile_id": "agent-claude-code-opus",
				"name":       "claude-code/opus",
				"model":      "opus",
				"effort":     nil,
				"runtime":    "claude-code",
				"provider":   "claude-subscription",
			},
			"source": "builtin",
		}
	}
	conc := func(value int) map[string]any {
		one := map[string]any{"value": value, "source": "builtin"}
		return map[string]any{
			"value":   value,
			"source":  "builtin",
			"task":    nil,
			"default": one,
		}
	}
	return map[string]any{
		"roles": map[string]any{
			"planner":  map[string]any{"profile": profile()},
			"worker":   map[string]any{"profile": profile(), "concurrency": conc(16)},
			"reviewer": map[string]any{"profile": profile(), "concurrency": conc(8)},
		},
		"concurrency_max": 64,
		"editable":        agentsEditable(status),
	}
}

func agentsEditable(status string) bool {
	switch taskGroup(status) {
	case groupNeedsYou, groupInProgress, groupReady, groupAttention, groupCancelled:
		return true
	default:
		return false
	}
}

func builtinStats(runs []map[string]any, steps []map[string]any) map[string]any {
	roleOf := map[string]any{}
	for _, step := range steps {
		roleOf[asString(step["id"])] = step["role_key"]
	}
	runsOf := func(role, key string) []map[string]any {
		var out []map[string]any
		for _, row := range runs {
			if asString(row["role"]) == role && asString(row["task_role"]) == key {
				out = append(out, row)
			}
		}
		return out
	}
	builtinReviews := func(workKey any) []map[string]any {
		var out []map[string]any
		for _, row := range runsOf("reviewer", "") {
			if sameRole(roleOf[asString(row["step_id"])], workKey) {
				out = append(out, row)
			}
		}
		return out
	}
	var workerSteps, reviewerSteps []map[string]any
	for _, step := range steps {
		// NULL and "" are the built-in worker (Python: not step.get("role_key")).
		if step["role_key"] == nil || asString(step["role_key"]) == "" {
			workerSteps = append(workerSteps, step)
		}
		if asBool(step["review"]) {
			reviewerSteps = append(reviewerSteps, step)
		}
	}
	return map[string]any{
		"worker":   roleStats(runsOf("worker", ""), workerSteps, builtinReviews(nil)),
		"reviewer": roleStats(runsOf("reviewer", ""), reviewerSteps, runsOf("reviewer", "")),
	}
}

func sameRole(stepRole, workKey any) bool {
	// Python compares with ==. A built-in review matches only a NULL role_key.
	return stepRole == workKey
}

func roleStats(runs, steps, reviews []map[string]any) map[string]any {
	count := func(rows []map[string]any, outcome string) int {
		n := 0
		for _, row := range rows {
			if asString(row["outcome"]) == outcome && row["outcome"] != nil {
				n += asInt(row["runs"])
			}
		}
		return n
	}
	finished := 0
	runN, running := 0, 0
	var cost float64
	for _, row := range runs {
		runsN := asInt(row["runs"])
		runN += runsN
		running += asInt(row["running"])
		finished += runsN - asInt(row["running"])
		if v, ok := asFloat(row["cost_usd"]); ok {
			cost += v
		}
	}
	violations := count(runs, "contract")
	passed, notPassed := count(reviews, "passed"), count(reviews, "not_passed")
	var live []map[string]any
	for _, step := range steps {
		if asString(step["status"]) != "skipped" {
			live = append(live, step)
		}
	}
	accepted, failed, retries := 0, 0, 0
	for _, step := range live {
		switch asString(step["status"]) {
		case "verified":
			accepted++
		case "failed":
			failed++
		}
		retries += asInt(step["retries"])
	}
	var violationRate, passRate any
	if finished > 0 {
		violationRate = roundN(float64(violations)/float64(finished), 4)
	}
	if passed+notPassed > 0 {
		passRate = roundN(float64(passed)/float64(passed+notPassed), 4)
	}
	return map[string]any{
		"runs":                    runN,
		"running":                 running,
		"cost_usd":                roundN(cost, 6),
		"contract_violations":     violations,
		"contract_violation_rate": violationRate,
		"steps":                   len(live),
		"accepted":                accepted,
		"failed":                  failed,
		"retries":                 retries,
		"reviews":                 map[string]any{"passed": passed, "not_passed": notPassed},
		"review_pass_rate":        passRate,
	}
}

func runRoles(runs, steps []map[string]any) map[string]any {
	roles := map[string]any{}
	for _, step := range steps {
		if id := asString(step["worker_id"]); id != "" && step["role_key"] != nil && asString(step["role_key"]) != "" {
			if role := runRole(map[string]any{"id": step["role_key"], "kind": "work", "version": step["role_version"]}); role != nil {
				roles[id] = role
			}
		}
		reviewing := reviewingRole(step)
		if id := asString(step["reviewer_id"]); reviewing != nil && id != "" {
			if role := runRole(map[string]any{"id": reviewing["role"], "kind": "review", "version": reviewing["version"]}); role != nil {
				roles[id] = role
			}
		}
	}
	for _, run := range runs {
		if role := runRole(run["task_role"]); role != nil {
			roles[asString(run["agent_id"])] = role
		}
	}
	return roles
}

func reviewingRole(step map[string]any) map[string]any {
	if asString(step["review_stage"]) != "role" {
		return nil
	}
	items, _ := step["review_roles"].([]any)
	pos, ok := asIntOK(step["review_position"])
	if !ok || pos < 0 || pos >= len(items) {
		return nil
	}
	item, _ := items[pos].(map[string]any)
	return item
}

func runRole(raw any) map[string]any {
	m, ok := raw.(map[string]any)
	if !ok || m == nil {
		return nil
	}
	id, ok := m["id"].(string)
	if !ok || id == "" {
		return nil
	}
	kind, ok := m["kind"].(string)
	if !ok || (kind != "work" && kind != "review") {
		return nil
	}
	ver, ok := asIntOK(m["version"])
	if !ok {
		return nil
	}
	return map[string]any{"id": id, "kind": kind, "version": ver}
}

func worktreeView(row map[string]any) map[string]any {
	if row == nil {
		return nil
	}
	branch := shlexQuote(asString(row["branch"]))
	base := asString(row["base_commit"])
	commands := []any{
		map[string]any{"what": "See the task's commits", "command": "git log --oneline " + base + ".." + branch},
		map[string]any{"what": "See its changes", "command": "git diff " + base + "..." + branch},
		map[string]any{"what": "Merge it into the branch you have checked out", "command": "git merge " + branch},
	}
	if asString(row["state"]) == "removed" {
		commands = append(commands, map[string]any{
			"what":    "Delete the branch, once merged or to drop the work",
			"command": "git branch -D " + branch,
		})
	}
	return map[string]any{
		"branch":        row["branch"],
		"base_commit":   row["base_commit"],
		"path":          row["path"],
		"repository":    row["repo_dir"],
		"links":         stringList(row["links"]),
		"state":         row["state"],
		"result_commit": row["result_commit"],
		"removed_at":    row["removed_at"],
		"commands":      commands,
	}
}

func taskWorkspace(task, worktree map[string]any) map[string]any {
	dir := asString(task["workspace_dir"])
	if dir == "" {
		dir = asString(task["work_dir"])
	}
	mode := asString(task["workspace_mode"])
	if mode == "" {
		mode = "in_place"
	}
	return map[string]any{
		"workspace_dir": dir,
		"mode":          mode,
		"base":          asString(task["workspace_base"]),
		"worktree":      worktreeView(worktree),
	}
}

// shlexQuote matches CPython shlex.quote for ASCII. An empty string is "”".
func shlexQuote(s string) string {
	if s == "" {
		return "''"
	}
	for _, r := range s {
		if r > 127 || !isQuoteSafe(r) {
			return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
		}
	}
	return s
}

func isQuoteSafe(r rune) bool {
	switch {
	case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		return true
	default:
		return strings.ContainsRune("_@%+=:,./-", r)
	}
}

func assembleCosts(ids []string, rows []map[string]any) map[string]map[string]any {
	out := make(map[string]map[string]any, len(ids))
	for _, id := range ids {
		out[id] = emptyCost()
	}
	models := map[string]map[string]*modelAcc{}
	mgrModels := map[string]map[string]*modelAcc{}
	for _, id := range ids {
		models[id] = map[string]*modelAcc{}
		mgrModels[id] = map[string]*modelAcc{}
	}
	for _, row := range rows {
		id := asString(row["task_id"])
		cost, ok := out[id]
		if !ok {
			continue
		}
		manager := asString(row["role"]) == "manager"
		if !manager {
			for _, key := range []string{"runs", "running", "unpriced_runs", "equivalent_runs", "provider_price_runs"} {
				cost[key] = asInt(cost[key]) + asInt(row[key])
			}
			addMoney(cost, "cost_usd", row["cost_usd"])
			role, _ := cost["by_role"].(map[string]any)[asString(row["role"])].(map[string]any)
			if role != nil {
				addMoney(role, "cost_usd", row["cost_usd"])
				role["runs"] = asInt(role["runs"]) + asInt(row["runs"])
			}
		}
		bucket := models[id]
		if manager {
			bucket = mgrModels[id]
		}
		key := modelKey(row["model"])
		acc := bucket[key]
		if acc == nil {
			acc = &modelAcc{model: row["model"]}
			bucket[key] = acc
		}
		acc.runs += asInt(row["runs"])
		acc.running += asInt(row["running"])
		acc.unpriced += asInt(row["unpriced_runs"])
		acc.interrupted += asInt(row["interrupted_runs"])
		acc.input += asInt(row["input_tokens"])
		acc.output += asInt(row["output_tokens"])
		acc.cacheRead += asInt(row["cache_read_tokens"])
		acc.cacheCreate += asInt(row["cache_creation_tokens"])
		if row["cost_usd"] != nil {
			v, _ := asFloat(row["cost_usd"])
			if acc.cost == nil {
				z := 0.0
				acc.cost = &z
			}
			*acc.cost += v
		}
	}
	for _, id := range ids {
		cost := out[id]
		cost["cost_usd"] = roundN(mustFloat(cost["cost_usd"]), 6)
		byRole, _ := cost["by_role"].(map[string]any)
		for _, role := range byRole {
			m := role.(map[string]any)
			m["cost_usd"] = roundN(mustFloat(m["cost_usd"]), 6)
		}
		cost["by_model"] = sortModels(models[id])
		cost["manager_by_model"] = sortModels(mgrModels[id])
	}
	return out
}

type modelAcc struct {
	model       any
	cost        *float64
	runs        int
	running     int
	unpriced    int
	interrupted int
	input       int
	output      int
	cacheRead   int
	cacheCreate int
}

func (m *modelAcc) view() map[string]any {
	var cost any
	if m.cost != nil {
		cost = roundN(*m.cost, 6)
	}
	return map[string]any{
		"model":                 m.model,
		"cost_usd":              cost,
		"runs":                  m.runs,
		"running":               m.running,
		"unpriced_runs":         m.unpriced,
		"interrupted_runs":      m.interrupted,
		"input_tokens":          m.input,
		"output_tokens":         m.output,
		"cache_read_tokens":     m.cacheRead,
		"cache_creation_tokens": m.cacheCreate,
	}
}

func sortModels(bucket map[string]*modelAcc) []any {
	list := make([]map[string]any, 0, len(bucket))
	for _, acc := range bucket {
		list = append(list, acc.view())
	}
	sort.Slice(list, func(i, j int) bool {
		a, b := list[i], list[j]
		aNil, bNil := a["cost_usd"] == nil, b["cost_usd"] == nil
		if aNil != bNil {
			return !aNil
		}
		if !aNil {
			af, bf := mustFloat(a["cost_usd"]), mustFloat(b["cost_usd"])
			if af != bf {
				return af > bf
			}
		}
		if asInt(a["runs"]) != asInt(b["runs"]) {
			return asInt(a["runs"]) > asInt(b["runs"])
		}
		if (a["model"] == nil) != (b["model"] == nil) {
			return a["model"] != nil
		}
		return asString(a["model"]) < asString(b["model"])
	})
	out := make([]any, len(list))
	for i, row := range list {
		out[i] = row
	}
	return out
}

func emptyCost() map[string]any {
	role := func() map[string]any {
		return map[string]any{"cost_usd": 0.0, "runs": 0}
	}
	return map[string]any{
		"cost_usd":            0.0,
		"runs":                0,
		"running":             0,
		"unpriced_runs":       0,
		"equivalent_runs":     0,
		"provider_price_runs": 0,
		"by_role": map[string]any{
			"planner":  role(),
			"worker":   role(),
			"reviewer": role(),
		},
		"by_model":         []any{},
		"manager_by_model": []any{},
	}
}

func addMoney(m map[string]any, key string, v any) {
	add, ok := asFloat(v)
	if !ok {
		return
	}
	cur, _ := asFloat(m[key])
	m[key] = cur + add
}

func modelKey(v any) string {
	if v == nil {
		return "\x00"
	}
	return "m:" + asString(v)
}

func roundN(v float64, n int) float64 {
	p := math.Pow(10, float64(n))
	return math.Round(v*p) / p
}

func asString(v any) string {
	s, _ := v.(string)
	return s
}

func strOr(v any, fallback string) string {
	if v == nil {
		return fallback
	}
	s, ok := v.(string)
	if !ok {
		return fallback
	}
	return s
}

func asInt(v any) int {
	n, _ := asIntOK(v)
	return n
}

func asIntOK(v any) (int, bool) {
	switch t := v.(type) {
	case int:
		return t, true
	case int32:
		return int(t), true
	case int64:
		return int(t), true
	case float64:
		return int(t), true
	case json.Number:
		n, err := t.Int64()
		return int(n), err == nil
	default:
		return 0, false
	}
}

func asFloat(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case float32:
		return float64(t), true
	case int:
		return float64(t), true
	case int32:
		return float64(t), true
	case int64:
		return float64(t), true
	default:
		return 0, false
	}
}

func mustFloat(v any) float64 {
	f, _ := asFloat(v)
	return f
}

func asBool(v any) bool {
	b, _ := v.(bool)
	return b
}
