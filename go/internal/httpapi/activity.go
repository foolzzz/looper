package httpapi

import (
	"errors"
	"log/slog"
	"net/http"
	"regexp"

	"github.com/foolzzz/looper/go/internal/db"
)

const activityLimitMax int64 = 500

var (
	activityListPattern = regexp.MustCompile(`^/api/tasks/(task-[a-zA-Z0-9]+)/activity$`)
	activitySeqPattern  = regexp.MustCompile(`^/api/tasks/(task-[a-zA-Z0-9]+)/activity/([0-9]+)$`)
	conversationPattern = regexp.MustCompile(`^/api/tasks/(task-[a-zA-Z0-9]+)/conversation$`)
)

func (s *Server) handleActivity(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	match := activityListPattern.FindStringSubmatch(r.URL.EscapedPath())
	if match == nil {
		notFound(w)
		return
	}
	// int() runs before the handler's try. A non-integer, including a value
	// past Python's digit limit, is the request loop's 500.
	values := pythonQuery(r.URL.RawQuery)
	sinceRaw := queryValue(values, "since")
	if sinceRaw == "" {
		sinceRaw = "0"
	}
	limitRaw := queryValue(values, "limit")
	if limitRaw == "" {
		limitRaw = "100"
	}
	since, sinceOver, ok1 := pythonInt(sinceRaw)
	limit, limitOver, ok2 := pythonInt(limitRaw)
	if !ok1 || !ok2 {
		internalServerError(w, errors.New("since or limit is not an integer"))
		return
	}
	// A value that does not fit int64 is what the database driver rejects,
	// except a positive limit, which min() caps before the query.
	if sinceOver != 0 || limitOver < 0 {
		emptyActivity(w, errors.New("since or limit does not fit"))
		return
	}
	if limitOver > 0 || limit > activityLimitMax {
		limit = activityLimitMax
	}
	if s.Reads == nil {
		emptyActivity(w, errors.New("no database"))
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	rows, err := s.Reads.ListActivity(ctx, match[1], since, limit)
	if err != nil {
		emptyActivity(w, err)
		return
	}
	if rows == nil {
		rows = []map[string]any{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"activity": rows})
}

func (s *Server) handleActivityDetail(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	// /activity/stream is a different route. Digits only, matching Python's
	// activity seq pattern. "stream" does not reach this handler on GET.
	match := activitySeqPattern.FindStringSubmatch(r.URL.EscapedPath())
	if match == nil {
		notFound(w)
		return
	}
	seq, overflow, ok := pythonInt(match[2])
	if !ok {
		internalServerError(w, errors.New("activity seq is not an integer"))
		return
	}
	// The route's int() succeeded. A value that does not fit int64 is a
	// driver error, and this handler answers 404.
	if overflow != 0 || s.Reads == nil {
		activityNotFound(w, errors.New("activity seq out of range"))
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	row, err := s.Reads.ActivityDetail(ctx, seq)
	if err != nil || row == nil {
		activityNotFound(w, err)
		return
	}
	writeJSON(w, http.StatusOK, row)
}

func (s *Server) handleConversation(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	match := conversationPattern.FindStringSubmatch(r.URL.EscapedPath())
	if match == nil {
		notFound(w)
		return
	}
	if s.Reads == nil {
		emptyConversation(w, errors.New("no database"))
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	rows, err := s.Reads.ListConversation(ctx, match[1])
	if err != nil {
		emptyConversation(w, err)
		return
	}
	if rows == nil {
		rows = []map[string]any{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"conversation": rows})
}

func emptyActivity(w http.ResponseWriter, err error) {
	slog.Error("activity", "err", db.LogText(err))
	writeJSON(w, http.StatusOK, map[string]any{"activity": []any{}})
}

func activityNotFound(w http.ResponseWriter, err error) {
	if err != nil {
		slog.Error("activity detail", "err", db.LogText(err))
	}
	writeJSON(w, http.StatusNotFound, errorBody{Error: "activity not found"})
}

func emptyConversation(w http.ResponseWriter, err error) {
	slog.Error("conversation", "err", db.LogText(err))
	writeJSON(w, http.StatusOK, map[string]any{"conversation": []any{}})
}
