package httpapi

import (
	"errors"
	"log/slog"
	"net/http"
	"regexp"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
)

// These paths are the Python router's. ServeMux decodes a percent-encoded
// segment before matching, so each handler compares EscapedPath with the
// same expression. A segment that matches the mux and not the expression is
// 404 and does not read the database. GET /api/roles/<role>/reset-prompt is
// not this route.
var (
	reRoleConfig      = regexp.MustCompile(`^/api/roles/([a-zA-Z0-9_-]+)$`)
	reChannelConfig   = regexp.MustCompile(`^/api/channel-configs/([a-zA-Z0-9_-]+)$`)
	reRoleTemplates   = regexp.MustCompile(`^/api/projects/(proj-[a-zA-Z0-9]+)/role-templates$`)
	reAllowedProfiles = regexp.MustCompile(`^/api/projects/(proj-[a-zA-Z0-9]+)/allowed-profiles$`)
	reRoleStats       = regexp.MustCompile(`^/api/projects/(proj-[a-zA-Z0-9]+)/role-stats$`)
)

func (s *Server) handleRoles(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/roles" {
		notFound(w)
		return
	}
	configs := []map[string]any{}
	if s.Reads != nil {
		ctx, cancel := readContext(r)
		defer cancel()
		rows, err := s.Reads.ListRoleConfigs(ctx)
		if err != nil {
			slog.Warn("role configs", "err", db.LogText(err))
		} else if rows != nil {
			configs = rows
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"configs": configs})
}

func (s *Server) handleRole(w http.ResponseWriter, r *http.Request) {
	m := reRoleConfig.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	role := m[1]
	if s.Reads == nil {
		missingQuoted(w, "role", role)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	row, err := s.Reads.GetRoleConfig(ctx, role)
	if err != nil {
		slog.Warn("role config", "err", db.LogText(err))
		missingQuoted(w, "role", role)
		return
	}
	if row == nil {
		missingQuoted(w, "role", role)
		return
	}
	writeJSON(w, http.StatusOK, row)
}

func (s *Server) handleChannels(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/channel-configs" {
		notFound(w)
		return
	}
	configs := []map[string]any{}
	if s.Reads != nil {
		ctx, cancel := readContext(r)
		defer cancel()
		rows, err := s.Reads.ListChannelConfigs(ctx)
		if err != nil {
			slog.Warn("channel configs", "err", db.LogText(err))
		} else if rows != nil {
			configs = rows
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"configs": configs})
}

func (s *Server) handleChannel(w http.ResponseWriter, r *http.Request) {
	m := reChannelConfig.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	id := m[1]
	if s.Reads == nil {
		missingQuoted(w, "channel", id)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	row, err := s.Reads.GetChannelConfig(ctx, id)
	if err != nil {
		slog.Warn("channel config", "err", db.LogText(err))
		missingQuoted(w, "channel", id)
		return
	}
	if row == nil {
		missingQuoted(w, "channel", id)
		return
	}
	writeJSON(w, http.StatusOK, row)
}

func (s *Server) handleGateSettings(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/gate-settings" {
		notFound(w)
		return
	}
	if s.Reads == nil {
		requestFailed(w, "gate settings", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.GateSettings(ctx)
	if err != nil {
		requestFailed(w, "gate settings", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleRoleTemplates(w http.ResponseWriter, r *http.Request) {
	m := reRoleTemplates.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	id := m[1]
	if s.Reads == nil {
		requestFailed(w, "role templates", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.RoleTemplates(ctx, id)
	if errors.Is(err, db.ErrProjectNotFound) {
		missingProject(w, id)
		return
	}
	if err != nil {
		requestFailed(w, "role templates", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleAllowedProfiles(w http.ResponseWriter, r *http.Request) {
	m := reAllowedProfiles.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	id := m[1]
	if s.Reads == nil {
		requestFailed(w, "allowed profiles", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.AllowedProfiles(ctx, id)
	if errors.Is(err, db.ErrProjectNotFound) {
		missingProject(w, id)
		return
	}
	if err != nil {
		requestFailed(w, "allowed profiles", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleRoleStats(w http.ResponseWriter, r *http.Request) {
	m := reRoleStats.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	id := m[1]
	// role_api.role_stats checks the project before it parses since. A store
	// that was never opened fails before either, the way get_pool does.
	if s.Reads == nil {
		requestFailed(w, "role stats", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	ok, err := s.Reads.ProjectExists(ctx, id)
	if err != nil {
		requestFailed(w, "role stats", err)
		return
	}
	if !ok {
		missingProject(w, id)
		return
	}
	var since *time.Time
	if raw := queryValue(pythonQuery(r.URL.RawQuery), "since"); raw != "" {
		day, ok := parseSinceDay(raw)
		if !ok {
			writeJSON(w, http.StatusBadRequest, errorBody{Error: sinceError(raw)})
			return
		}
		y, month, d := day.UTC().Date()
		local := time.Date(y, month, d, 0, 0, 0, 0, time.Local)
		since = &local
	}
	body, err := s.Reads.ProjectRoleStats(ctx, id, since)
	if err != nil {
		requestFailed(w, "role stats", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

// missingQuoted is f"{kind} '{id}' not found". The quotes are written as
// characters. Python's repr is not used, so an apostrophe stays an apostrophe.
func missingQuoted(w http.ResponseWriter, kind, id string) {
	writeJSON(w, http.StatusNotFound, errorBody{Error: kind + " '" + id + "' not found"})
}

func missingProject(w http.ResponseWriter, id string) {
	writeJSON(w, http.StatusNotFound, errorBody{Error: "project " + pythonRepr(id) + " not found"})
}

// requestFailed is the request loop's uncaught Exception: 500, and a log line
// that does not carry the driver text. _answer's RequestError is not this.
func requestFailed(w http.ResponseWriter, what string, err error) {
	slog.Warn(what, "err", db.LogText(err))
	writeJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
}

// sinceError is RequestError for a day date.fromisoformat refuses.
// since[:40] is the first 40 code points.
func sinceError(since string) string {
	shown := since
	if runes := []rune(since); len(runes) > 40 {
		shown = string(runes[:40])
	}
	return "since must be a day (YYYY-MM-DD), not " + pythonRepr(shown)
}
