package httpapi

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/foolzzz/looper/go/internal/db"
)

const (
	tasksPage    = 50
	tasksPageMax = 200
	readTimeout  = 30 * time.Second

	errGroup  = "group must be one of needs_you, in_progress, ready, attention, completed, cancelled, other"
	errStatus = "status must be one of assigned, running, planning, executing, verifying, stopping, waiting_user, pending, init, failed, stopped, completed, cancelled"
	errLimit  = "limit must be a whole number from 1 to 200"
)

// taskIDPattern is Python's task detail path: /api/tasks/(task-[a-zA-Z0-9]+).
var taskIDPattern = regexp.MustCompile(`^task-[a-zA-Z0-9]+$`)

func (s *Server) handleOverview(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	if s.Reads == nil {
		databaseUnavailable(w, r, errors.New("no database"))
		return
	}
	body, err := s.Reads.Overview(ctx, localMidnight(s.now()))
	if err != nil {
		databaseUnavailable(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleProjects(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	if s.Reads == nil {
		databaseUnavailable(w, r, errors.New("no database"))
		return
	}
	body, err := s.Reads.Projects(ctx)
	if err != nil {
		databaseUnavailable(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

// handleTasks is GET /api/tasks. The list reader stays in the db package.
// This slice does not serve it.
func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	notImplemented(w, r)
}

// handleTask is GET /api/tasks/<id> for a task id. Other single segments,
// such as /api/tasks/create, stay 404.
func (s *Server) handleTask(w http.ResponseWriter, r *http.Request) {
	if !taskIDPattern.MatchString(r.PathValue("id")) {
		notFound(w)
		return
	}
	notImplemented(w, r)
}

func parseTaskQuery(r *http.Request) (db.TaskQuery, string, error) {
	values := r.URL.Query()
	group := queryValue(values, "group")
	if group != "" && !knownGroup(group) {
		return db.TaskQuery{}, errGroup, errors.New("group")
	}
	status := queryValue(values, "status")
	if status != "" && !knownTaskStatus(status) {
		return db.TaskQuery{}, errStatus, errors.New("status")
	}
	rawLimit := queryValue(values, "limit")
	limit := tasksPage
	if rawLimit != "" {
		n, ok := wholeNumber(rawLimit)
		if !ok || n < 1 || n > tasksPageMax {
			return db.TaskQuery{}, errLimit, errors.New("limit")
		}
		limit = n
	}
	statuses, inside := db.StatusesOf(group)
	return db.TaskQuery{
		ProjectID:  queryValue(values, "project_id"),
		Status:     status,
		ScheduleID: queryValue(values, "schedule_id"),
		Before:     queryValue(values, "before"),
		Statuses:   statuses,
		Inside:     inside,
		Limit:      limit,
	}, "", nil
}

// queryValue keeps the last non-blank value. Python parse_qsl drops blanks, so
// group=completed&group= still filters.
func queryValue(values map[string][]string, key string) string {
	found := ""
	for _, v := range values[key] {
		if v != "" {
			found = v
		}
	}
	return found
}

func knownGroup(group string) bool {
	switch group {
	case "needs_you", "in_progress", "ready", "attention", "completed", "cancelled", "other":
		return true
	default:
		return false
	}
}

func knownTaskStatus(status string) bool {
	switch status {
	case "assigned", "running", "planning", "executing", "verifying", "stopping", "waiting_user",
		"pending", "init", "failed", "stopped", "completed", "cancelled":
		return true
	default:
		return false
	}
}

// wholeNumber is Python's limit check: str.isdigit() and int().
// A non-ASCII digit such as fullwidth ２ makes str.isdigit() true and int()
// reject it. That is the same 400 as a non-digit. Overflow is the same 400.
func wholeNumber(s string) (int, bool) {
	if s == "" {
		return 0, false
	}
	for _, r := range s {
		if !unicode.IsDigit(r) || r < '0' || r > '9' {
			return 0, false
		}
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return 0, false
	}
	return n, true
}

// pythonRepr is CPython repr for a str: single quotes, unless the string
// contains an apostrophe and no double quote.
func pythonRepr(s string) string {
	quote := '\''
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteRune(quote)
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r == quote {
				b.WriteByte('\\')
				b.WriteRune(quote)
				continue
			}
			if r < 32 || r == 127 || (r < 0x100 && !strconv.IsPrint(r)) {
				fmt.Fprintf(&b, `\x%02x`, r)
				continue
			}
			if strconv.IsPrint(r) {
				b.WriteRune(r)
				continue
			}
			if r <= 0xFFFF {
				fmt.Fprintf(&b, `\u%04x`, r)
				continue
			}
			fmt.Fprintf(&b, `\U%08x`, r)
		}
	}
	b.WriteRune(quote)
	return b.String()
}

func readContext(r *http.Request) (context.Context, context.CancelFunc) {
	// Always start the 30s window. WithTimeout keeps the sooner of that
	// window and a deadline the caller already set.
	return context.WithTimeout(r.Context(), readTimeout)
}

func localMidnight(now time.Time) time.Time {
	loc := now.Location()
	y, m, d := now.In(loc).Date()
	return time.Date(y, m, d, 0, 0, 0, 0, loc)
}

func notFound(w http.ResponseWriter) {
	writeJSON(w, http.StatusNotFound, errorBody{Error: "not found"})
}

func databaseUnavailable(w http.ResponseWriter, r *http.Request, err error) {
	slog.Error("database unavailable", "path", r.URL.Path, "err", db.LogText(err))
	writeJSON(w, http.StatusInternalServerError, errorBody{Error: "database unavailable"})
}
