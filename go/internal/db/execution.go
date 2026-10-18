package db

import (
	"fmt"
	"log/slog"
	"math"
	"strings"
	"unicode/utf8"
)

// Execution settings follow looper/agents/project_execution.py.
// A stored value that no longer validates is left unset (null in the API),
// which inherits, and a warning is logged. distill_lessons is never unset:
// null, "", and a value that does not validate are "off".
const (
	superviseMaxSeconds   = 86400
	stepRetriesMax        = 20
	roleTextMinChars      = 500
	roleTextMaxChars      = 20000
	gateReminderMaxHours  = 168
	budgetMaxUSD          = 100000
	consultBudgetMin      = 0.01
	consultBudgetMax      = 5
	workspaceLinksMax     = 20
	workspaceLinkMaxChars = 255
)

var executionFields = []string{
	"task_budget_usd", "task_budget_action", "supervise_interval_seconds", "review_policy",
	"distill_lessons", "step_workspaces", "workspace_links", "max_step_retries", "planner_roles",
	"role_text_max_chars", "user_gates", "gate_reminder_hours", "agent_room", "room_post_per_minute",
	"room_broadcasts_per_run", "room_message_max_chars", "room_messages_per_task", "room_ping_pong_rounds",
	"room_wait_max_seconds", "room_consult_budget_usd", "room_consult_timeout_seconds", "room_consults_per_task",
	"task_workspaces", "operator_notes",
}

// roomLimit is low, high. The default is not returned; null inherits it.
var roomLimit = map[string][2]int{
	"room_post_per_minute":         {1, 60},
	"room_broadcasts_per_run":      {0, 50},
	"room_message_max_chars":       {500, 20000},
	"room_messages_per_task":       {20, 5000},
	"room_ping_pong_rounds":        {2, 50},
	"room_wait_max_seconds":        {0, 120},
	"room_consult_timeout_seconds": {30, 1800},
	"room_consults_per_task":       {0, 100},
}

var executionChoice = map[string][]string{
	"task_budget_action": {"wrap_up", "fail"},
	"review_policy":      {"planner", "all"},
	"step_workspaces":    {"shared", "worktree"},
	"task_workspaces":    {"in_place", "worktree"},
	"operator_notes":     {"off", "on"},
	"planner_roles":      {"off", "on"},
	"user_gates":         {"off", "on"},
	"agent_room":         {"off", "on"},
}

// executionView is ProjectExecution.from_row(row).view().
func executionView(row map[string]any) map[string]any {
	out := make(map[string]any, len(executionFields))
	for _, field := range executionFields {
		out[field] = nil
	}
	out["distill_lessons"] = "off"
	if row == nil {
		return out
	}
	for _, field := range executionFields {
		raw := row[field]
		if field != "distill_lessons" && (raw == nil || raw == "") {
			continue
		}
		val, err := parseExecution(field, raw)
		if err != nil {
			slog.Warn("ignoring project execution setting",
				"project_id", asString(row["id"]), "field", field, "reason", err.Error())
			continue
		}
		out[field] = val
	}
	return out
}

func parseExecution(field string, raw any) (any, error) {
	if field == "distill_lessons" {
		return parseLessons(raw)
	}
	if raw == nil || raw == "" {
		return nil, nil
	}
	if choices, ok := executionChoice[field]; ok {
		return parseChoice(field, raw, choices)
	}
	if bounds, ok := roomLimit[field]; ok {
		return parseWhole(field, raw, bounds[0], bounds[1])
	}
	switch field {
	case "task_budget_usd":
		return parseBudget(field, raw)
	case "room_consult_budget_usd":
		value, err := parseBudget(field, raw)
		if err != nil {
			return nil, err
		}
		if value < consultBudgetMin || value > consultBudgetMax {
			return nil, fmt.Errorf("%s must be from %g to %g USD", field, float64(consultBudgetMin), float64(consultBudgetMax))
		}
		return value, nil
	case "workspace_links":
		return parseLinks(raw)
	case "supervise_interval_seconds":
		return parseWhole(field, raw, 0, superviseMaxSeconds)
	case "max_step_retries":
		return parseWhole(field, raw, 0, stepRetriesMax)
	case "role_text_max_chars":
		return parseWhole(field, raw, roleTextMinChars, roleTextMaxChars)
	case "gate_reminder_hours":
		return parseWhole(field, raw, 0, gateReminderMaxHours)
	default:
		return nil, fmt.Errorf("unknown execution field %q", field)
	}
}

func parseLessons(raw any) (string, error) {
	if raw == nil || raw == "" {
		return "off", nil
	}
	if on, ok := raw.(bool); ok {
		if on {
			return "write", nil
		}
		return "off", nil
	}
	text, ok := raw.(string)
	if !ok || (text != "off" && text != "draft" && text != "write") {
		return "", fmt.Errorf("distill_lessons must be one of off, draft, write, or null for off")
	}
	return text, nil
}

func parseChoice(field string, raw any, choices []string) (string, error) {
	text, ok := raw.(string)
	if ok {
		for _, choice := range choices {
			if text == choice {
				return text, nil
			}
		}
	}
	return "", fmt.Errorf("%s must be one of %s, or null to inherit", field, strings.Join(choices, ", "))
}

func parseWhole(field string, raw any, low, high int) (int, error) {
	n, ok := wholeInt(raw)
	if !ok || n < low || n > high {
		return 0, fmt.Errorf("%s must be a whole number from %d to %d, or null to inherit", field, low, high)
	}
	return n, nil
}

// parseBudget is agent_settings.budget_usd: more than 0, at most budgetMaxUSD, at most 4 decimals.
// A bool is not a number. A float passes only when Python's round(amount, 4) == amount.
func parseBudget(field string, raw any) (float64, error) {
	value, ok := money(raw)
	if !ok || math.IsNaN(value) || math.IsInf(value, 0) || !(value > 0 && value <= budgetMaxUSD) || !atMostFourDecimals(value) {
		return 0, fmt.Errorf("%s must be more than 0 and at most %g US dollars, with at most 4 decimals", field, float64(budgetMaxUSD))
	}
	return value, nil
}

func parseLinks(raw any) (any, error) {
	items, ok := stringItems(raw)
	if !ok {
		return nil, fmt.Errorf("workspace_links must be a list of paths relative to the task directory, or null")
	}
	if len(items) > workspaceLinksMax {
		return nil, fmt.Errorf("workspace_links may have at most %d paths, not %d", workspaceLinksMax, len(items))
	}
	var paths []string
	for _, item := range items {
		text := strings.TrimSpace(item)
		if problem := linkProblem(text, paths); problem != "" {
			return nil, fmt.Errorf("workspace_links: %q %s", item, problem)
		}
		paths = append(paths, text)
	}
	if len(paths) == 0 {
		return nil, nil
	}
	out := make([]any, len(paths))
	for i, path := range paths {
		out[i] = path
	}
	return out, nil
}

func linkProblem(text string, paths []string) string {
	parts := strings.Split(text, "/")
	switch {
	case text == "" || utf8.RuneCountInString(text) > workspaceLinkMaxChars || strings.ContainsAny(text, "\x00\n\r\\"):
		return "must be 1 to 255 characters, without line breaks or backslashes"
	case strings.HasPrefix(text, "/"):
		return "must be relative to the task directory"
	case badLinkPart(parts):
		return "must not have empty, . or .. parts"
	case linkPart(parts, ".git"):
		return "must not be or be in .git"
	case linkListed(paths, text):
		return "is listed twice"
	default:
		for _, other := range paths {
			if pathUnder(text, other) || pathUnder(other, text) {
				return fmt.Sprintf("overlaps %q: a link cannot be inside another", other)
			}
		}
		return ""
	}
}

func badLinkPart(parts []string) bool {
	for _, part := range parts {
		if part == "" || part == "." || part == ".." {
			return true
		}
	}
	return false
}

func linkPart(parts []string, want string) bool {
	for _, part := range parts {
		if part == want {
			return true
		}
	}
	return false
}

func linkListed(paths []string, text string) bool {
	for _, path := range paths {
		if path == text {
			return true
		}
	}
	return false
}

// pathUnder reports PurePosixPath(path).is_relative_to(parent), including equality.
func pathUnder(path, parent string) bool {
	return path == parent || strings.HasPrefix(path, parent+"/")
}

func stringItems(raw any) ([]string, bool) {
	switch items := raw.(type) {
	case []string:
		return items, true
	case []any:
		out := make([]string, len(items))
		for i, item := range items {
			text, ok := item.(string)
			if !ok {
				return nil, false
			}
			out[i] = text
		}
		return out, true
	default:
		return nil, false
	}
}

// wholeInt is a Python int that is not a bool.
func wholeInt(v any) (int, bool) {
	switch t := v.(type) {
	case int:
		return t, true
	case int8:
		return int(t), true
	case int16:
		return int(t), true
	case int32:
		return int(t), true
	case int64:
		if int64(int(t)) != t {
			return 0, false
		}
		return int(t), true
	default:
		return 0, false
	}
}

func money(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case float32:
		return float64(t), true
	case int:
		return float64(t), true
	case int8:
		return float64(t), true
	case int16:
		return float64(t), true
	case int32:
		return float64(t), true
	case int64:
		return float64(t), true
	default:
		return 0, false
	}
}

// atMostFourDecimals is Python's round(amount, 4) == amount.
// Equality is exact. A tolerance accepts 1.00000000001, which Python rejects.
// The rounding matches CPython for ndigits=4: scale by 1e4, round half away
// from zero, and on an exact tie round half to even.
func atMostFourDecimals(v float64) bool {
	const scale = 10000.0
	y := v * scale
	if math.IsInf(y, 0) || math.IsNaN(y) {
		return false
	}
	z := math.Round(y)
	if math.Abs(y-z) == 0.5 {
		z = 2 * math.Round(y/2)
	}
	return z/scale == v
}
