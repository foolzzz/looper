package httpapi

import (
	"errors"
	"log/slog"
	"net/http"
	"regexp"

	"github.com/foolzzz/looper/go/internal/db"
)

var reKnowledge = regexp.MustCompile(`^/api/projects/(proj-[a-zA-Z0-9]+)/knowledge(?:/(drafts|doc-[a-zA-Z0-9]+))?$`)
var reProjectSkill = regexp.MustCompile(`^/api/projects/(proj-[a-zA-Z0-9]+)/skills/(skill-[a-zA-Z0-9]+)$`)

func (s *Server) handleKnowledge(w http.ResponseWriter, r *http.Request) {
	m := reKnowledge.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	if s.Reads == nil || s.RuntimeRoot == "" {
		knowledgeFailed(w, "knowledge", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	var body map[string]any
	var err error
	switch m[2] {
	case "":
		body, err = s.Reads.Knowledge(ctx, m[1], s.RuntimeRoot)
	case "drafts":
		body, err = s.Reads.Drafts(ctx, m[1])
	default:
		body, err = s.Reads.Document(ctx, m[1], m[2], s.RuntimeRoot)
	}
	switch {
	case errors.Is(err, db.ErrProjectNotFound):
		missingProject(w, m[1])
	case errors.Is(err, db.ErrDocumentNotFound):
		writeJSON(w, http.StatusNotFound, errorBody{Error: "project " + pythonRepr(m[1]) + " has no document " + pythonRepr(m[2])})
	case err != nil:
		knowledgeFailed(w, "knowledge", err)
	default:
		writeJSON(w, http.StatusOK, body)
	}
}

func (s *Server) handleProjectSkill(w http.ResponseWriter, r *http.Request) {
	m := reProjectSkill.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	if s.Reads == nil {
		knowledgeFailed(w, "catalog", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.ProjectSkill(ctx, m[1], m[2])
	switch {
	case errors.Is(err, db.ErrSkillNotFound):
		writeJSON(w, http.StatusNotFound, errorBody{Error: "project " + pythonRepr(m[1]) + " has no skill " + pythonRepr(m[2])})
	case err != nil:
		knowledgeFailed(w, "catalog", err)
	default:
		writeJSON(w, http.StatusOK, body)
	}
}

func knowledgeFailed(w http.ResponseWriter, kind string, err error) {
	slog.Warn(kind+" request failed", "err", db.LogText(err))
	writeJSON(w, http.StatusInternalServerError, errorBody{Error: "the " + kind + " request failed; see the server log"})
}
