// Package httpapi is the Looper HTTP server.
package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path"
	"strings"

	"github.com/foolzzz/looper/go/internal/db"
)

// DBPinger checks the database. The production implementation runs SELECT 1.
type DBPinger interface {
	Ping(ctx context.Context) error
}

// Server serves GET /api/status and, when WebDir is set, the dashboard files.
type Server struct {
	Version string
	DB      DBPinger
	WebDir  string
}

// New builds a Server. A nil DB is reported as a database error, not a panic.
func New(version string, pinger DBPinger, webDir string) *Server {
	return &Server{Version: version, DB: pinger, WebDir: webDir}
}

// Handler routes the G0 API and the optional static dashboard.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/status", s.handleStatus)
	mux.HandleFunc("/api/", s.handleAPI)
	mux.HandleFunc("/", s.handleRoot)
	return mux
}

type statusBody struct {
	Status         string `json:"status"`
	Implementation string `json:"implementation"`
	Version        string `json:"version"`
	DB             string `json:"db"`
}

type errorBody struct {
	Error string `json:"error"`
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	dbStatus := "ok"
	switch {
	case s.DB == nil:
		dbStatus = "error: no database"
	default:
		ctx, cancel := context.WithTimeout(r.Context(), db.ProbeTimeout)
		defer cancel()
		if err := s.DB.Ping(ctx); err != nil {
			dbStatus = db.ErrorText(err)
			slog.Warn("database check failed", "err", dbStatus)
		}
	}
	writeJSON(w, http.StatusOK, statusBody{
		Status:         "running",
		Implementation: "go",
		Version:        s.Version,
		DB:             dbStatus,
	})
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

func writeJSON(w http.ResponseWriter, code int, v any) {
	data, err := json.Marshal(v)
	if err != nil {
		slog.Error("encode json", "err", err)
		http.Error(w, `{"error":"internal server error"}`, http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_, _ = w.Write(data)
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
