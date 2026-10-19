package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/foolzzz/looper/go/internal/db"
)

// requestBodyMaxBytes is Python REQUEST_BODY_MAX_BYTES: 4 times the 2 MiB
// document cap. A larger Content-Length is refused unread.
const requestBodyMaxBytes = 8 << 20

// requestBodyTimeout is the wait_for around Python's readexactly.
const requestBodyTimeout = 30 * time.Second

type okBody struct {
	OK bool `json:"ok"`
}

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
	if r.URL.EscapedPath() != "/api/chat/messages" {
		notFound(w)
		return
	}
	switch r.Method {
	case http.MethodGet:
		s.handleChatMessagesGet(w, r)
	case http.MethodPost:
		s.handleChatSaveMessage(w, r)
	default:
		notFound(w)
	}
}

func (s *Server) handleChatMessagesGet(w http.ResponseWriter, r *http.Request) {
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

func (s *Server) handleChatSaveMessage(w http.ResponseWriter, r *http.Request) {
	// A missing Content-Length is 0. The body is not read, matching a request
	// Python treats as empty before json.loads.
	if r.ContentLength < 0 {
		writePythonJSON(w, http.StatusBadRequest, errorBody{Error: "invalid JSON"})
		return
	}
	if r.ContentLength > requestBodyMaxBytes {
		writePythonJSON(w, http.StatusRequestEntityTooLarge, errorBody{
			Error: fmt.Sprintf(
				"the request body has %d bytes; the server takes at most 8 MiB (%d bytes)",
				r.ContentLength, requestBodyMaxBytes,
			),
		})
		return
	}
	var body []byte
	if r.ContentLength > 0 {
		// ReadHeaderTimeout ends at the header block. Python then allows
		// 30 seconds for the body and closes the connection on timeout,
		// without a status line.
		if err := setBodyReadDeadline(w, time.Now().Add(s.chatBodyTimeout())); err != nil {
			slog.Error("chat save", "err", err)
			writePythonJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
			return
		}
		var err error
		body, err = io.ReadAll(io.LimitReader(r.Body, r.ContentLength))
		if bodyReadTimedOut(err) {
			slog.Error("chat save", "err", fmt.Errorf("read body: %w", err))
			closeWithoutResponse(w)
			return
		}
		if err != nil || int64(len(body)) != r.ContentLength {
			slog.Error("chat save", "err", "request body")
			writePythonJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
			return
		}
		if err := setBodyReadDeadline(w, time.Time{}); err != nil {
			slog.Error("chat save", "err", err)
			writePythonJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
			return
		}
	}
	// Python decodes the body as UTF-8 before json.loads. encoding/json
	// accepts an invalid sequence inside a string and replaces it with
	// U+FFFD, so the bytes are checked first.
	if len(body) == 0 || !utf8.Valid(body) || !json.Valid(body) {
		writePythonJSON(w, http.StatusBadRequest, errorBody{Error: "invalid JSON"})
		return
	}
	var raw any
	if err := json.Unmarshal(body, &raw); err != nil {
		writePythonJSON(w, http.StatusBadRequest, errorBody{Error: "invalid JSON"})
		return
	}
	obj, ok := raw.(map[string]any)
	if !ok {
		slog.Error("chat save", "err", "request JSON is not an object")
		writePythonJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return
	}
	sessionID := jsonField(obj, "session_id")
	messageID := jsonField(obj, "message_id")
	role := jsonField(obj, "role")
	content := jsonField(obj, "content")
	if !jsonTruthy(sessionID) || !jsonTruthy(messageID) || !jsonTruthy(role) {
		writePythonJSON(w, http.StatusBadRequest, errorBody{Error: "session_id, message_id, role required"})
		return
	}
	session, sessionOK := sessionID.(string)
	message, messageOK := messageID.(string)
	roleName, roleOK := role.(string)
	text, contentOK := content.(string)
	if !sessionOK || !messageOK || !roleOK || !contentOK {
		slog.Debug("failed to save chat message", "err", "chat message fields are not strings")
		writePythonJSON(w, http.StatusOK, okBody{OK: true})
		return
	}
	saver, ok := s.Reads.(chatSaver)
	if !ok || saver == nil {
		slog.Debug("failed to save chat message", "err", "no database")
		writePythonJSON(w, http.StatusOK, okBody{OK: true})
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	if err := saver.SaveChatMessage(ctx, session, message, roleName, text); err != nil {
		slog.Debug("failed to save chat message", "err", db.LogText(err))
	}
	writePythonJSON(w, http.StatusOK, okBody{OK: true})
}

func (s *Server) chatBodyTimeout() time.Duration {
	if s != nil && s.bodyReadTimeout > 0 {
		return s.bodyReadTimeout
	}
	return requestBodyTimeout
}

func setBodyReadDeadline(w http.ResponseWriter, deadline time.Time) error {
	err := http.NewResponseController(w).SetReadDeadline(deadline)
	if errors.Is(err, http.ErrNotSupported) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("set body read deadline: %w", err)
	}
	return nil
}

func bodyReadTimedOut(err error) bool {
	var netErr net.Error
	return err != nil && errors.As(err, &netErr) && netErr.Timeout()
}

// closeWithoutResponse ends the socket the way Python's connection handler
// does after TimeoutError: the writer is closed and no status is sent.
func closeWithoutResponse(w http.ResponseWriter) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		writePythonJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return
	}
	conn, _, err := hj.Hijack()
	if err != nil {
		slog.Error("chat save", "err", fmt.Errorf("close timed out body: %w", err))
		writePythonJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return
	}
	_ = conn.Close()
}

// jsonField is dict.get(key, ""). A present null stays null.
func jsonField(obj map[string]any, key string) any {
	value, ok := obj[key]
	if !ok {
		return ""
	}
	return value
}

// jsonTruthy is Python truthiness for a JSON value decoded by encoding/json.
func jsonTruthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case string:
		return t != ""
	case bool:
		return t
	case float64:
		return t != 0
	case []any:
		return len(t) > 0
	case map[string]any:
		return len(t) > 0
	default:
		return true
	}
}
