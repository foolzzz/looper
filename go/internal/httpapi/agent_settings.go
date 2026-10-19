package httpapi

import (
	"errors"
	"net/http"
	"regexp"

	"github.com/foolzzz/looper/go/internal/db"
)

// Python matches these after ServeMux would already have decoded a percent-encoded
// segment. EscapedPath keeps the original segment, so a decoded id is 404 and
// does not read the database.
var (
	reAgentSettings        = regexp.MustCompile(`^/api/agent-settings$`)
	reProjectAgentSettings = regexp.MustCompile(`^/api/projects/(proj-[a-zA-Z0-9]+)/agent-settings$`)
)

func (s *Server) handleAgentSettings(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || !reAgentSettings.MatchString(r.URL.EscapedPath()) {
		notFound(w)
		return
	}
	if s.Reads == nil {
		requestFailed(w, "agent settings", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.AgentSettings(ctx)
	if err != nil {
		requestFailed(w, "agent settings", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleProjectAgentSettings(w http.ResponseWriter, r *http.Request) {
	m := reProjectAgentSettings.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	id := m[1]
	if s.Reads == nil {
		requestFailed(w, "project agent settings", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.ProjectAgentSettings(ctx, id)
	if errors.Is(err, db.ErrProjectNotFound) {
		missingProject(w, id)
		return
	}
	if err != nil {
		requestFailed(w, "project agent settings", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}
