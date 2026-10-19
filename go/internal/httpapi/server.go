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
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/pyjson"
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
// 404. Gate settings, role templates, role stats, and agent settings answer 500.
// Gate messages and a task room answer 500 too: both read the database
// before they can say the task or the gate is missing.
// A project schedule list, one schedule, its runs, and its notifications
// answer 500 when the store is missing, after a runs limit and skips value
// that can be refused without it. A workspace check answers 500 once a
// directory has been accepted and the active-task list is required. A
// project id is looked up before that directory check.
type Reader interface {
	Knowledge(ctx context.Context, projectID, runtimeRoot string) (map[string]any, error)
	Document(ctx context.Context, projectID, documentID, runtimeRoot string) (map[string]any, error)
	Drafts(ctx context.Context, projectID string) (map[string]any, error)
	ProjectSkill(ctx context.Context, projectID, skillID string) (map[string]any, error)
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
	AllowedProfiles(ctx context.Context, projectID string) (map[string]any, error)
	ProjectRoleStats(ctx context.Context, projectID string, since *time.Time) (map[string]any, error)
	AgentSettings(ctx context.Context) (map[string]any, error)
	ProjectAgentSettings(ctx context.Context, projectID string) (map[string]any, error)
	GateMessages(ctx context.Context, taskID, label string) (map[string]any, error)
	TaskRoom(ctx context.Context, taskID string, q db.RoomQuery) (map[string]any, error)
	ProjectWorkspace(ctx context.Context, id string) (db.ProjectWorkspace, error)
	ActiveWorkspaces(ctx context.Context) ([]db.ActiveWorkspace, error)
	ListProjectSchedules(ctx context.Context, projectID string) (map[string]any, error)
	ListSchedules(ctx context.Context) (map[string]any, error)
	GetSchedule(ctx context.Context, id string) (map[string]any, error)
	ListScheduleRuns(ctx context.Context, id string, q db.ScheduleRunsQuery) (map[string]any, error)
	ListScheduleNotifications(ctx context.Context, id string, limit int) (map[string]any, error)
	// Registry is GET /api/registry. claudeBin is the Claude Code executable
	// an empty runtime row resolves. environ nil tells the store to use the
	// process environment. A test passes a temporary environment so the login
	// probe does not read the operator's Codex or Grok login.
	Registry(ctx context.Context, claudeBin string, environ []string) (map[string]any, error)
}

// Server serves the read-only API and, when WebDir is set, the dashboard files.
// BindHost is the listen address used by the Host header guard. Empty means
// 127.0.0.1, which is what cmd/looper listens on.
// Now is the clock for today's overview. Nil uses time.Now. The parity harness
// sets it so spend and skipped runs do not follow the wall clock.
type Server struct {
	// RuntimeRoot names stored knowledge read paths. Empty refuses knowledge GETs,
	// matching a Python server started without a runtime root. GETs never write it.
	RuntimeRoot string
	Version     string
	DB          DBPinger
	WebDir      string
	Reads       Reader
	BindHost    string
	Now         func() time.Time
	// ClaudeBin is the Claude Code executable an empty claude-code runtime
	// resolves. Empty means "claude", which is Python's default.
	ClaudeBin string
	// LoginEnviron, when set, is the environment login probes and executable
	// lookups use. Nil uses the process environment.
	LoginEnviron func() []string
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
// route. A static path is not redirected either. Python joins the path and
// asks pathlib whether that file exists. pathlib drops "." and empty
// segments, but a ".." segment is a real lookup: if the directory it leaves
// is missing, the file is missing too, even when the lexical result names a
// file that is there. The original path is what the static handler opens.
// os.Root still refuses a symlink that leaves the web directory.
//
// This slice serves GET /api/status, GET /api/projects, GET /api/overview,
// GET /api/tasks, GET /api/tasks/<id>, GET /api/agents/<id>/usage,
// GET /api/llm-records (the list, one record, and its events),
// GET /api/chat/targets, GET /api/chat/sessions, GET /api/chat/messages,
// and a task's activity list, activity detail, and conversation.
// It also serves GET /api/roles, GET /api/roles/<role>, GET /api/channel-configs,
// GET /api/channel-configs/<id>, GET /api/gate-settings, and a project's
// role templates, allowed profiles, and role stats.
// It also serves GET /api/tasks/<id>/gates/<label>/messages and
// GET /api/tasks/<id>/room.
// It also serves GET /api/workspaces/check, GET /api/projects/<id>/schedules,
// GET /api/schedules, GET /api/schedules/<id>, its runs, and its notifications.
// It also serves GET /api/agent-settings and GET /api/projects/<id>/agent-settings.
// It also serves GET /api/registry, project knowledge, documents, drafts, and owned skills.
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
	mux.HandleFunc("/api/projects/{id}/allowed-profiles", s.handleAllowedProfiles)
	mux.HandleFunc("/api/registry", s.handleRegistry)
	mux.HandleFunc("/api/agent-settings", s.handleAgentSettings)
	mux.HandleFunc("/api/projects/{id}/agent-settings", s.handleProjectAgentSettings)
	mux.HandleFunc("/api/projects/{id}/role-stats", s.handleRoleStats)
	mux.HandleFunc("/api/projects/{id}/knowledge", s.handleKnowledge)
	mux.HandleFunc("/api/projects/{id}/knowledge/{document}", s.handleKnowledge)
	mux.HandleFunc("/api/projects/{id}/skills/{skill}", s.handleProjectSkill)
	mux.HandleFunc("/api/workspaces/check", s.handleWorkspaceCheck)
	mux.HandleFunc("/api/projects/{id}/schedules", s.handleProjectSchedules)
	mux.HandleFunc("/api/schedules/{id}/runs", s.handleScheduleRuns)
	mux.HandleFunc("/api/schedules/{id}/notifications", s.handleScheduleNotifications)
	mux.HandleFunc("/api/schedules/{id}", s.handleSchedule)
	mux.HandleFunc("/api/schedules", s.handleSchedules)
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
			s.handleRoot(w, withPythonPath(r, escaped))
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
// Agents stays an empty map until a supervisor is wired. pyjson refuses a
// non-empty map; a later card has to build a pyjson.Object in Python's order.
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
	writePythonJSON(w, http.StatusOK, statusBody{
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
	// Error bodies use Python's spelling. Success bodies are still compact
	// until each route keeps Python's key order; see raw/PENDING.txt.
	if _, ok := v.(errorBody); ok {
		writePythonJSON(w, code, v)
		return
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	// Python json.dumps does not escape &, <, or >. The dashboard parses JSON.
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		slog.Error("encode json", "err", err)
		// http.Error would send text/plain, compact JSON, and a trailing newline.
		writePythonJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_, _ = w.Write(bytes.TrimRight(buf.Bytes(), "\n"))
}

// writePythonJSON is Python _json_response: UTF-8, separator spaces, no
// trailing newline, Content-Type with charset, and Content-Length of that
// body. A value pyjson cannot encode becomes 500 with a fixed JSON body.
func writePythonJSON(w http.ResponseWriter, code int, v any) {
	payload, err := pyjson.Marshal(v)
	if err != nil {
		slog.Error("encode json", "err", err)
		payload = []byte(`{"error": "internal server error"}`)
		code = http.StatusInternalServerError
	}
	h := w.Header()
	h.Set("Content-Type", pyjson.ContentType)
	h.Set("Content-Length", strconv.Itoa(len(payload)))
	w.WriteHeader(code)
	_, _ = w.Write(payload)
}

var (
	errNotFound     = errors.New("not found")
	errUseIndex     = errors.New("use index")
	errStaticEscape = errors.New("static path escapes")
)

// openStatic opens a regular file for urlPath inside rootDir.
// os.Root follows symlinks and refuses one that leaves the directory, so a
// path such as /link/secret.txt is refused when link itself points outside.
// A missing path or a directory falls back to index.html. An index.html
// symlink that leaves the directory is a 404, the same as a missing index.
func openStatic(rootDir, urlPath string) (*os.File, string, error) {
	root, err := os.OpenRoot(rootDir)
	if err != nil {
		return nil, "", fmt.Errorf("web dir: %w", err)
	}
	defer root.Close()

	f, name, err := openPythonStatic(root, urlPath)
	switch {
	case err == nil:
		return f, name, nil
	case errors.Is(err, os.ErrNotExist), errors.Is(err, errUseIndex):
	default:
		return nil, "", err
	}
	f, err = openRegular(root, "index.html")
	if err != nil {
		// An index.html symlink that leaves the web root is a miss. Python's
		// resolve check answers 404. Returning the escape error here is a 500.
		miss := errors.Is(err, os.ErrNotExist) || errors.Is(err, errUseIndex) ||
			errors.Is(err, errNotFound) || errors.Is(err, errStaticEscape)
		if miss {
			return nil, "", errNotFound
		}
		return nil, "", err
	}
	return f, "index.html", nil
}

// openPythonStatic opens urlPath the way CPython pathlib does.
// "." and empty segments are dropped. ".." is kept for os.Root, which
// follows a symlink and then applies ".." to that target: inside ->
// assets/deep makes /inside/../app.js open assets/app.js. Popping ".."
// off the URL before that lookup would open the root file instead.
// A missing component or a file used as a directory is a missing file, so
// the caller serves index.html. A symlink os.Root refuses is errNotFound,
// a JSON 404 and not the index page. A ".." os.Root refuses is classified
// by openEscapedStatic: an existing file outside the root is also a 404.
// The returned name is the final URL component, for Content-Type.
func openPythonStatic(root *os.Root, urlPath string) (*os.File, string, error) {
	parts := pathlibParts(urlPath)
	if len(parts) == 0 {
		return nil, "", errUseIndex
	}
	f, err := openRegular(root, strings.Join(parts, "/"))
	if err == nil {
		return f, parts[len(parts)-1], nil
	}
	if errors.Is(err, errStaticEscape) && !staticSymlinkEscapes(root, parts) {
		return openEscapedStatic(root, parts)
	}
	if errors.Is(err, errStaticEscape) {
		return nil, "", errNotFound
	}
	return nil, "", err
}

// openEscapedStatic classifies a ".." that os.Root refused and that did not
// leave through a symlink. Python stats the joined path, ".." included, and
// only then checks where it resolved. An existing regular file outside the
// web root is a 404. The same lookup walks back inside for /../web/app.js
// and that file is served. A missing path, or anything that is not a regular
// file, is the index page. The outside file is never opened: a path that
// resolves inside is opened again through os.Root, under a relative name.
func openEscapedStatic(root *os.Root, parts []string) (*os.File, string, error) {
	// filepath.Join cleans ".." before the lookup. Python and the kernel
	// apply ".." only after each symlink, so a missing component stays missing.
	raw := strings.TrimRight(root.Name(), "/") + "/" + strings.Join(parts, "/")
	info, err := os.Stat(raw)
	if err != nil || !info.Mode().IsRegular() {
		return nil, "", os.ErrNotExist
	}
	resolved, err := filepath.EvalSymlinks(raw)
	if err != nil {
		return nil, "", errNotFound
	}
	rootResolved, err := filepath.EvalSymlinks(root.Name())
	if err != nil {
		return nil, "", errNotFound
	}
	// A path boundary, not a string prefix: /tmp/web must not accept /tmp/web-evil.
	if !pathInside(rootResolved, resolved) {
		return nil, "", errNotFound
	}
	rel, err := filepath.Rel(rootResolved, resolved)
	if err != nil || !relativeInside(rel) {
		return nil, "", errNotFound
	}
	f, err := openRegular(root, rel)
	if err != nil {
		if errors.Is(err, errStaticEscape) || errors.Is(err, errNotFound) {
			return nil, "", errNotFound
		}
		return nil, "", err
	}
	return f, parts[len(parts)-1], nil
}

func relativeInside(rel string) bool {
	if rel == "" || rel == "." || filepath.IsAbs(rel) {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// staticSymlinkEscapes reports whether following parts leaves the root
// through a symlink. A symlink is replaced by its target before ".." is
// applied: deep -> assets/deep makes /deep/../out look at assets/out, not a
// sibling of the URL name deep. Popping ".." off that name first hides the
// link, and a path os.Root refuses is then served as the index page. An
// absolute link is an escape only when its target leaves the root. One
// whose target stays inside is followed, and ".." in the rest of the URL
// applies to that target. A relative link whose ".." leaves the root is an
// escape. A ".." in the URL that leaves the root is not a symlink escape;
// openEscapedStatic decides whether that path names an existing file.
// os.Root still opens every file that is served, so a refused path is never read.
func staticSymlinkEscapes(root *os.Root, parts []string) bool {
	_, escaped := walkStatic(root, nil, parts, false, 0)
	return escaped
}

// walkStatic applies parts to stack. inLink is true while the parts are a
// symlink target: ".." that climbs out of the root is an escape there, and
// not one when it is a URL segment. depth stops a link loop.
func walkStatic(root *os.Root, stack, parts []string, inLink bool, depth int) ([]string, bool) {
	if depth > 8 {
		return stack, true
	}
	for i, part := range parts {
		if part == "" || part == "." {
			continue
		}
		if part == ".." {
			if len(stack) == 0 {
				return stack, inLink
			}
			stack = stack[:len(stack)-1]
			continue
		}
		stack = append(stack, part)
		rel := strings.Join(stack, "/")
		info, err := root.Lstat(rel)
		if err != nil {
			return stack, pathEscapes(err)
		}
		if info.Mode()&os.ModeSymlink == 0 {
			continue
		}
		// Go 1.24's os.Root can Lstat a link but cannot read its text.
		// The text is only used to tell a JSON 404 from the index page.
		// os.Root still opens the file, so this read cannot disclose it.
		target, err := os.Readlink(filepath.Join(root.Name(), rel))
		if err != nil {
			return stack, true
		}
		if filepath.IsAbs(target) {
			inside, targetRel := absoluteLinkInside(root, target)
			if !inside {
				return stack, true
			}
			// The absolute target replaces this path. ".." later in the URL
			// applies to that target, not to the name that pointed at it.
			next := append(linkParts(targetRel), parts[i+1:]...)
			return walkStatic(root, nil, next, true, depth+1)
		}
		parent := append([]string{}, stack[:len(stack)-1]...)
		next, escaped := walkStatic(root, parent, linkParts(target), true, depth+1)
		if escaped {
			return next, true
		}
		stack = next
	}
	return stack, false
}

// absoluteLinkInside reports whether an absolute symlink target stays inside
// the static root. rel is the slash-separated path of that target from the
// root. A target that cannot be resolved is judged from its cleaned path: a
// missing file inside the root is not an escape, and a missing file outside
// is. The caller still opens a served file through os.Root.
func absoluteLinkInside(root *os.Root, target string) (inside bool, rel string) {
	rootResolved, err := filepath.EvalSymlinks(root.Name())
	if err != nil {
		return false, ""
	}
	resolved, err := filepath.EvalSymlinks(target)
	if err != nil {
		cleaned := filepath.Clean(target)
		if inside, rel = relativeLink(rootResolved, cleaned); inside {
			return true, rel
		}
		if cleanRoot := filepath.Clean(root.Name()); cleanRoot != rootResolved {
			return relativeLink(cleanRoot, cleaned)
		}
		return false, ""
	}
	return relativeLink(rootResolved, resolved)
}

func relativeLink(root, path string) (bool, string) {
	if path == root {
		return true, ""
	}
	if !pathInside(root, path) {
		return false, ""
	}
	rel, err := filepath.Rel(root, path)
	if err != nil || !relativeInside(rel) {
		return false, ""
	}
	return true, filepath.ToSlash(rel)
}

func linkParts(target string) []string {
	raw := strings.Split(target, "/")
	parts := make([]string, 0, len(raw))
	for _, part := range raw {
		if part == "" || part == "." {
			continue
		}
		parts = append(parts, part)
	}
	return parts
}

// pathlibParts splits a URL path the way pathlib joins it: empty segments
// and "." are dropped, and ".." is kept for the caller to look up.
func pathlibParts(urlPath string) []string {
	raw := strings.Split(strings.TrimPrefix(urlPath, "/"), "/")
	parts := make([]string, 0, len(raw))
	for _, part := range raw {
		if part == "" || part == "." {
			continue
		}
		parts = append(parts, part)
	}
	return parts
}

func openRegular(root *os.Root, name string) (*os.File, error) {
	info, err := root.Stat(name)
	if err != nil {
		if os.IsNotExist(err) || errors.Is(err, syscall.ENOTDIR) {
			return nil, os.ErrNotExist
		}
		if pathEscapes(err) {
			return nil, errStaticEscape
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
		if os.IsNotExist(err) || errors.Is(err, syscall.ENOTDIR) {
			return nil, os.ErrNotExist
		}
		if pathEscapes(err) {
			return nil, errStaticEscape
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
