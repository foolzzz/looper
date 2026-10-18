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
// and answers the data routes with 500.
type Reader interface {
	StatusTaskCounts(ctx context.Context) (total, running, completed int, err error)
	Overview(ctx context.Context, since time.Time) (map[string]any, error)
	Projects(ctx context.Context) (map[string]any, error)
	TaskExists(ctx context.Context, id string) (bool, error)
	Tasks(ctx context.Context, q db.TaskQuery) (map[string]any, error)
	Task(ctx context.Context, id string) (map[string]any, error)
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
// Data routes are registered without a method so a POST is 404, as in Python,
// except the routes that answer 501 below.
// GET /api/status stays method-specific so POST and PUT fall through to 405.
// The Host/Origin guard runs before routing, matching request_guard.refusal.
//
// This slice serves GET /api/status, GET /api/projects, GET /api/overview,
// and GET /api/tasks. The other read routes from the same milestone answer
// 501 and do not read the database: /api/tasks/<id>, and the cost routes
// /api/agents/<id>/usage and /api/llm-records.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/status", s.handleStatus)
	mux.HandleFunc("/api/overview", s.handleOverview)
	mux.HandleFunc("/api/projects", s.handleProjects)
	mux.HandleFunc("/api/tasks", s.handleTasks)
	mux.HandleFunc("/api/tasks/{id}", s.handleTask)
	mux.HandleFunc("/api/agents/{id}/usage", notImplemented)
	mux.HandleFunc("/api/llm-records", notImplemented)
	mux.HandleFunc("/api/llm-records/{id}", notImplemented)
	mux.HandleFunc("/api/llm-records/{id}/events", notImplemented)
	mux.HandleFunc("/api/", s.handleAPI)
	mux.HandleFunc("/", s.handleRoot)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if reason := requestRefusal(s.bindHost(), r); reason != "" {
			slog.Warn("refused request", "reason", reason, "remote", r.RemoteAddr)
			writeJSON(w, http.StatusForbidden, errorBody{Error: reason})
			return
		}
		mux.ServeHTTP(w, r)
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
	if r.URL.Path == "/api/status" {
		methodNotAllowed(w)
		return
	}
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
	f, name, err := openStatic(s.WebDir, r.URL.Path)
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

// notImplemented is a route the Python server serves and this process does not.
// The body is fixed. It is not a partial payload.
func notImplemented(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusNotImplemented, errorBody{Error: "not implemented in the Go server yet"})
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
