package httpapi

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
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

// rawTaskID is the id Python's route sees. The handler has already applied
// urlparse, so a semicolon parameter is gone and percent-escapes remain.
// The pattern allows only letters and digits, so
// /api/tasks/task-rich0000%31 is not a task id. PathValue decodes %31 to 1
// and would load task-rich00001.
func rawTaskID(r *http.Request) (string, bool) {
	const prefix = "/api/tasks/"
	path := r.URL.EscapedPath()
	id, found := strings.CutPrefix(path, prefix)
	if !found || strings.Contains(id, "/") || !taskIDPattern.MatchString(id) {
		return "", false
	}
	return id, true
}

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

// handleTasks is GET /api/tasks. Other methods are 404, as in Python.
func (s *Server) handleTasks(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	q, msg, err := parseTaskQuery(r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: msg})
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	if s.Reads == nil {
		databaseUnavailable(w, r, errors.New("no database"))
		return
	}
	if q.Before != "" {
		ok, err := s.Reads.TaskExists(ctx, q.Before)
		if err != nil {
			databaseUnavailable(w, r, err)
			return
		}
		if !ok {
			writeJSON(w, http.StatusBadRequest, errorBody{Error: "before must be a task id; " + pythonRepr(q.Before) + " is not one"})
			return
		}
	}
	body, err := s.Reads.Tasks(ctx, q)
	if err != nil {
		databaseUnavailable(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

// handleTask is GET /api/tasks/<id>. A task id that is not in the database is
// 404 {"error":"task not found"}. Any other method, or a segment that is not
// task-[A-Za-z0-9]+ on the raw path, is 404 {"error":"not found"} and does not
// read the database.
func (s *Server) handleTask(w http.ResponseWriter, r *http.Request) {
	id, ok := rawTaskID(r)
	if r.Method != http.MethodGet || !ok {
		notFound(w)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	if s.Reads == nil {
		databaseUnavailable(w, r, errors.New("no database"))
		return
	}
	body, err := s.Reads.Task(ctx, id)
	if errors.Is(err, db.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, errorBody{Error: "task not found"})
		return
	}
	if err != nil {
		databaseUnavailable(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func parseTaskQuery(r *http.Request) (db.TaskQuery, string, error) {
	values := pythonQuery(r.URL.RawQuery)
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

// wholeNumber is the numeric half of Python's limit check. Python accepts
// every Unicode decimal digit (Nd), including fullwidth ２, because int()
// does. A superscript such as ² is not a decimal digit: Python's int()
// raises ValueError and that handler sends no limit 400. This server
// answers the limit 400. Overflow is the same 400.
func wholeNumber(s string) (int, bool) {
	if s == "" {
		return 0, false
	}
	n := 0
	for _, r := range s {
		d, ok := decimalDigit(r)
		if !ok {
			return 0, false
		}
		if n > (math.MaxInt-d)/10 {
			return 0, false
		}
		n = n*10 + d
	}
	return n, true
}

// decimalDigit is the value of a Unicode decimal digit. Nd runs are groups
// of ten starting at zero; two groups can sit next to each other, so the
// value is the distance from the start of the contiguous run, modulo 10.
func decimalDigit(r rune) (int, bool) {
	if !unicode.IsDigit(r) {
		return 0, false
	}
	start := r
	for start > 0 && unicode.IsDigit(start-1) {
		start--
	}
	return int(r-start) % 10, true
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
