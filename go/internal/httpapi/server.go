// Package httpapi is the Looper HTTP server.
package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path"
	"strings"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
)

// DBPinger checks the database. The production implementation runs SELECT 1.
type DBPinger interface {
	Ping(ctx context.Context) error
}

// Reader is the read-only API. A nil Reads keeps the status task counts at zero
// and the chat target list at the Manager. Overview, projects, tasks, task
// detail, usage, one record, and its events answer 500. The llm-records list
// answers 200 with an empty list. Chat messages, activity, and conversation
// do too. Activity detail answers 404. Chat sessions answer 500. Role and
// channel lists answer 200 with an empty list. One role or channel answers
// 404. Gate settings, role templates, and role stats answer 500.
// Gate messages and a task room answer 500 too: both read the database
// before they can say the task or the gate is missing.
type Reader interface {
	StatusTaskCounts(ctx context.Context) (total, running, completed int, err error)
	Overview(ctx context.Context, since time.Time) (map[string]any, error)
	Projects(ctx context.Context) (map[string]any, error)
	TaskExists(ctx context.Context, id string) (bool, error)
	Tasks(ctx context.Context, q db.TaskQuery) (map[string]any, error)
	Task(ctx context.Context, id string) (map[string]any, error)
	AgentUsage(ctx context.Context, agentID string, turns int) (map[string]any, error)
	ListRecords(ctx context.Context, q db.RecordQuery) ([]map[string]any, error)
	GetRecord(ctx context.Context, id int64) (map[string]any, error)
	ListEvents(ctx context.Context, id, after, limit int64) ([]map[string]any, error)
	ChatTargets(ctx context.Context) ([]map[string]any, error)
	ListChatSessions(ctx context.Context) ([]map[string]any, error)
	ListChatMessages(ctx context.Context, sessionID string) ([]map[string]any, error)
	ListActivity(ctx context.Context, taskID string, since, limit int64) ([]map[string]any, error)
	ActivityDetail(ctx context.Context, seq int64) (map[string]any, error)
	ListConversation(ctx context.Context, taskID string) ([]map[string]any, error)
	ListRoleConfigs(ctx context.Context) ([]map[string]any, error)
	GetRoleConfig(ctx context.Context, role string) (map[string]any, error)
	ListChannelConfigs(ctx context.Context) ([]map[string]any, error)
	GetChannelConfig(ctx context.Context, id string) (map[string]any, error)
	GateSettings(ctx context.Context) (map[string]any, error)
	ProjectExists(ctx context.Context, id string) (bool, error)
	RoleTemplates(ctx context.Context, projectID string) (map[string]any, error)
	ProjectRoleStats(ctx context.Context, projectID string, since *time.Time) (map[string]any, error)
	GateMessages(ctx context.Context, taskID, label string) (map[string]any, error)
	TaskRoom(ctx context.Context, taskID string, q db.RoomQuery) (map[string]any, error)
}

// Server serves the read-only API and, when WebDir is set, the dashboard files.
// BindHost is the listen address used by the Host header guard. Empty means
// 127.0.0.1, which is what cmd/looper listens on.
// Now is the clock for today's overview. Nil uses time.Now. The parity harness
// sets it so spend and skipped runs do not follow the wall clock.
type Server struct {
	Version  string
	DB       DBPinger
	WebDir   string
	Reads    Reader
	BindHost string
	Now      func() time.Time
}

// New builds a Server. A nil DB is reported as a database error, not a panic.
func New(version string, pinger DBPinger, webDir string) *Server {
	return &Server{Version: version, DB: pinger, WebDir: webDir}
}

// Handler routes the read-only API and the optional static dashboard.
// Data routes are registered without a method so a POST is 404, as in Python.
// GET /api/status stays method-specific. POST and PUT miss that pattern and
// the /api/ handler answers 404, which is what Python's router does.
// A pattern registered as GET also matches HEAD. The usage and llm-record
// handlers reject that HEAD themselves; Python's method check returns 404.
// The Host/Origin guard runs before routing, matching request_guard.refusal.
// The path is urllib.parse.urlparse(target).path before the mux sees it.
// A semicolon after the last slash is a path parameter and is not part of
// the path. An API path ServeMux would redirect (a "." or ".." segment, or
// a duplicate slash) is JSON 404: Python keeps that path and matches no
// route. A static path is not redirected either. Python's static handler
// joins it and lets pathlib collapse "." and "..", so the collapsed path is
// served here, including when the collapse lands under /api/.
//
// This slice serves GET /api/status, GET /api/projects, GET /api/overview,
// GET /api/tasks, GET /api/tasks/<id>, GET /api/agents/<id>/usage,
// GET /api/llm-records (the list, one record, and its events),
// GET /api/chat/targets, GET /api/chat/sessions, GET /api/chat/messages,
// and a task's activity list, activity detail, and conversation.
// It also serves GET /api/roles, GET /api/roles/<role>, GET /api/channel-configs,
// GET /api/channel-configs/<id>, GET /api/gate-settings, and a project's
// role templates and role stats.
// It also serves GET /api/tasks/<id>/gates/<label>/messages and
// GET /api/tasks/<id>/room.
// GET /api/chat/events and GET /api/tasks/<id>/activity/stream are SSE and
// are not served here; /activity/stream is 404 until that route exists.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/status", s.handleStatus)
	mux.HandleFunc("/api/overview", s.handleOverview)
	mux.HandleFunc("/api/projects", s.handleProjects)
	mux.HandleFunc("/api/tasks", s.handleTasks)
	mux.HandleFunc("/api/tasks/{id}/activity/{seq}", s.handleActivityDetail)
	mux.HandleFunc("/api/tasks/{id}/activity", s.handleActivity)
	mux.HandleFunc("/api/tasks/{id}/conversation", s.handleConversation)
	mux.HandleFunc("/api/tasks/{id}/gates/{label}/messages", s.handleGateMessages)
	mux.HandleFunc("/api/tasks/{id}/room", s.handleTaskRoom)
	mux.HandleFunc("/api/tasks/{id}", s.handleTask)
	mux.HandleFunc("/api/chat/targets", s.handleChatTargets)
	mux.HandleFunc("/api/chat/sessions", s.handleChatSessions)
	mux.HandleFunc("/api/chat/messages", s.handleChatMessages)
	mux.HandleFunc("GET /api/agents/{id}/usage", s.handleAgentUsage)
	mux.HandleFunc("GET /api/llm-records/{id}/events", s.handleRecordEvents)
	mux.HandleFunc("GET /api/llm-records/{id}", s.handleGetRecord)
	mux.HandleFunc("GET /api/llm-records", s.handleListRecords)
	mux.HandleFunc("/api/roles", s.handleRoles)
	mux.HandleFunc("/api/roles/{role}", s.handleRole)
	mux.HandleFunc("/api/channel-configs", s.handleChannels)
	mux.HandleFunc("/api/channel-configs/{id}", s.handleChannel)
	mux.HandleFunc("/api/gate-settings", s.handleGateSettings)
	mux.HandleFunc("/api/projects/{id}/role-templates", s.handleRoleTemplates)
	mux.HandleFunc("/api/projects/{id}/role-stats", s.handleRoleStats)
	mux.HandleFunc("/api/", s.handleAPI)
	mux.HandleFunc("/", s.handleRoot)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if reason := requestRefusal(s.bindHost(), r); reason != "" {
			slog.Warn("refused request", "reason", reason, "remote", r.RemoteAddr)
			writeJSON(w, http.StatusForbidden, errorBody{Error: reason})
			return
		}
		escaped := pythonURLPath(requestTarget(r))
		cleaned := canonicalPath(escaped)
		if escaped != cleaned {
			if strings.HasPrefix(escaped, "/api/") {
				notFound(w)
				return
			}
			s.handleRoot(w, withPythonPath(r, cleaned))
			return
		}
		mux.ServeHTTP(w, withPythonPath(r, escaped))
	})
}

// bindHost is the address the process listens on. serve binds loopback only.
func (s *Server) bindHost() string {
	if s.BindHost != "" {
		return s.BindHost
	}
	return "127.0.0.1"
}

type statusTasks struct {
	Total     int `json:"total"`
	Running   int `json:"running"`
	Completed int `json:"completed"`
}

type webHealth struct {
	Channel          string `json:"channel"`
	Healthy          bool   `json:"healthy"`
	ActiveSSEStreams int    `json:"active_sse_streams"`
}

type statusChannels struct {
	Web webHealth `json:"web"`
}

// statusBody is Python _handle_status. implementation, version, and db are not
// in that object. The database probe is logged beside it and is not a field.
type statusBody struct {
	Status   string         `json:"status"`
	Agents   map[string]any `json:"agents"`
	Tasks    statusTasks    `json:"tasks"`
	Channels statusChannels `json:"channels"`
	Checks   []any          `json:"checks"`
}

type errorBody struct {
	Error string `json:"error"`
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	// ServeMux unescapes each segment, so /api/%73tatus matches this pattern.
	// Python compares the path with the escapes left in place.
	if r.URL.EscapedPath() != "/api/status" {
		notFound(w)
		return
	}
	// Probe the database for the log. Python's status body does not carry the result.
	switch {
	case s.DB == nil:
		slog.Warn("database check failed", "err", "error: no database")
	default:
		ctx, cancel := context.WithTimeout(r.Context(), db.ProbeTimeout)
		err := s.DB.Ping(ctx)
		cancel()
		if err != nil {
			slog.Warn("database check failed", "err", db.ErrorText(err))
		}
	}
	tasks := statusTasks{}
	if s.Reads != nil {
		ctx, cancel := context.WithTimeout(r.Context(), db.ProbeTimeout)
		total, running, completed, err := s.Reads.StatusTaskCounts(ctx)
		cancel()
		if err != nil {
			slog.Warn("status task counts", "err", db.LogText(err))
		} else {
			tasks = statusTasks{Total: total, Running: running, Completed: completed}
		}
	}
	writeJSON(w, http.StatusOK, statusBody{
		Status: "running",
		Agents: map[string]any{},
		Tasks:  tasks,
		Channels: statusChannels{Web: webHealth{
			Channel:          "web",
			Healthy:          false,
			ActiveSSEStreams: 0,
		}},
		Checks: []any{},
	})
}

func (s *Server) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

func (s *Server) handleAPI(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusNotFound, errorBody{Error: "not found"})
}

func (s *Server) handleRoot(w http.ResponseWriter, r *http.Request) {
	if s.WebDir == "" {
		writeJSON(w, http.StatusNotFound, errorBody{Error: "not found"})
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		methodNotAllowed(w)
		return
	}
	// Python joins the undecoded request-target path. EscapedPath re-encodes
	// raw UTF-8, so /é.js would look up "%C3%A9.js". /%C3%A9.js is that other
	// file, and /%61pp.js is not app.js. A miss falls back to index.html.
	f, name, err := openStatic(s.WebDir, pythonURLPath(requestTarget(r)))
	if err != nil {
		if errors.Is(err, errNotFound) {
			writeJSON(w, http.StatusNotFound, errorBody{Error: "not found"})
			return
		}
		slog.Error("static file", "err", err)
		writeJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		slog.Error("stat web file", "err", err)
		writeJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return
	}
	// ServeContent, not ServeFile: ServeFile redirects /index.html to /.
	http.ServeContent(w, r, name, info.ModTime(), f)
}

func methodNotAllowed(w http.ResponseWriter) {
	w.Header().Set("Allow", "GET, HEAD")
	writeJSON(w, http.StatusMethodNotAllowed, errorBody{Error: "method not allowed"})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	// Python json.dumps does not escape &, <, or >. The dashboard parses JSON.
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		slog.Error("encode json", "err", err)
		http.Error(w, `{"error":"internal server error"}`, http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_, _ = w.Write(bytes.TrimRight(buf.Bytes(), "\n"))
}

var (
	errNotFound = errors.New("not found")
	errUseIndex = errors.New("use index")
)

// openStatic opens a regular file for urlPath inside rootDir.
// os.Root follows symlinks and refuses one that leaves the directory, so a
// path such as /link/secret.txt is refused when link itself points outside.
// A missing path or a directory falls back to index.html.
func openStatic(rootDir, urlPath string) (*os.File, string, error) {
	root, err := os.OpenRoot(rootDir)
	if err != nil {
		return nil, "", fmt.Errorf("web dir: %w", err)
	}
	defer root.Close()

	rel := staticRel(urlPath)
	if rel != "" {
		f, err := openRegular(root, rel)
		switch {
		case err == nil:
			return f, path.Base(rel), nil
		case errors.Is(err, os.ErrNotExist), errors.Is(err, errUseIndex):
		default:
			return nil, "", err
		}
	}
	f, err := openRegular(root, "index.html")
	if err != nil {
		if errors.Is(err, os.ErrNotExist) || errors.Is(err, errUseIndex) || errors.Is(err, errNotFound) {
			return nil, "", errNotFound
		}
		return nil, "", err
	}
	return f, "index.html", nil
}

func staticRel(urlPath string) string {
	rel := strings.TrimPrefix(path.Clean("/"+urlPath), "/")
	if rel == "" || rel == "." {
		return ""
	}
	return rel
}

func openRegular(root *os.Root, name string) (*os.File, error) {
	info, err := root.Stat(name)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, err
		}
		if pathEscapes(err) {
			return nil, errNotFound
		}
		return nil, fmt.Errorf("stat web file: %w", err)
	}
	if info.IsDir() {
		return nil, errUseIndex
	}
	if !info.Mode().IsRegular() {
		return nil, errNotFound
	}
	f, err := root.Open(name)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, err
		}
		if pathEscapes(err) {
			return nil, errNotFound
		}
		return nil, fmt.Errorf("open web file: %w", err)
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, fmt.Errorf("stat web file: %w", err)
	}
	if st.IsDir() {
		f.Close()
		return nil, errUseIndex
	}
	if !st.Mode().IsRegular() {
		f.Close()
		return nil, errNotFound
	}
	return f, nil
}

// pathEscapes reports os.Root's refusal to follow a symlink out of the root.
// That error is a PathError whose text is "path escapes from parent".
// It is not a public sentinel.
func pathEscapes(err error) bool {
	var pe *os.PathError
	return errors.As(err, &pe) && pe.Err != nil && pe.Err.Error() == "path escapes from parent"
}
