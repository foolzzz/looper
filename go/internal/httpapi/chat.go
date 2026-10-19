package httpapi

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"syscall"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/foolzzz/looper/go/internal/db"
)

// errNoDatabase is a store that was never opened. list_sessions calls
// get_pool(), which raises RuntimeError when the pool was not initialised.
var errNoDatabase = errors.New("no database")

func (s *Server) handleChatTargets(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/chat/targets" {
		notFound(w)
		return
	}
	// There is no task router in this process, so live planners stay empty.
	// A failed history read still answers 200 with the Manager, as Python does.
	targets := []map[string]any{{
		"kind": "manager", "id": "manager", "can_send": true,
	}}
	if s.Reads != nil {
		ctx, cancel := readContext(r)
		defer cancel()
		rows, err := s.Reads.ChatTargets(ctx)
		if err != nil {
			slog.Warn("chat targets", "err", db.LogText(err))
		} else if len(rows) > 0 {
			targets = rows
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"targets": targets})
}

func (s *Server) handleChatSessions(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/chat/sessions" {
		notFound(w)
		return
	}
	if s.Reads == nil {
		chatSessionsUnavailable(w, errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	rows, err := s.Reads.ListChatSessions(ctx)
	if err != nil {
		chatSessionsUnavailable(w, err)
		return
	}
	if rows == nil {
		rows = []map[string]any{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": rows})
}

func chatSessionsUnavailable(w http.ResponseWriter, err error) {
	slog.Warn("chat sessions", "err", db.LogText(err))
	writeJSON(w, http.StatusInternalServerError, errorBody{
		Error: "the database could not be read (" + sessionErrorName(err) + ")",
	})
}

// sessionErrorName is type(exc).__name__ for the exception Python's session
// list would show. A server error is the asyncpg class for its SQLSTATE, even
// when the driver error is wrapped. Other failures use the matching builtin
// or asyncpg interface name, not the Go type.
func sessionErrorName(err error) string {
	if err == nil {
		return "Exception"
	}
	if errors.Is(err, errNoDatabase) {
		return "RuntimeError"
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return asyncpgErrorName(pgErr.Code)
	}
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return "TimeoutError"
	case errors.Is(err, context.Canceled):
		return "CancelledError"
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "TimeoutError"
	}
	if errors.Is(err, syscall.ECONNREFUSED) {
		return "ConnectionRefusedError"
	}
	return "InterfaceError"
}

func (s *Server) handleChatMessages(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/chat/messages" {
		notFound(w)
		return
	}
	sessionID := queryValue(pythonQuery(r.URL.RawQuery), "session_id")
	if sessionID == "" {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: "session_id is required"})
		return
	}
	if s.Reads == nil {
		emptyMessages(w, errors.New("no database"))
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	rows, err := s.Reads.ListChatMessages(ctx, sessionID)
	if err != nil {
		emptyMessages(w, err)
		return
	}
	if rows == nil {
		rows = []map[string]any{}
	}
	writeJSON(w, http.StatusOK, map[string]any{"messages": rows})
}

func emptyMessages(w http.ResponseWriter, err error) {
	slog.Error("chat messages", "err", db.LogText(err))
	writeJSON(w, http.StatusOK, map[string]any{"messages": []any{}})
}
