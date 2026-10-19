package httpapi

import (
	"errors"
	"net/http"
	"regexp"

	"github.com/foolzzz/looper/go/internal/db"
)

var (
	reCatalogItem     = regexp.MustCompile(`^/api/catalog/([a-z]+-[a-zA-Z0-9]+)$`)
	reProjectInstalls = regexp.MustCompile(`^/api/projects/(proj-[a-zA-Z0-9]+)/installs$`)
)

func (s *Server) handleCatalog(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/catalog" {
		notFound(w)
		return
	}
	if s.Reads == nil {
		knowledgeFailed(w, "catalog", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.Catalog(ctx)
	if err != nil {
		knowledgeFailed(w, "catalog", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleCatalogItem(w http.ResponseWriter, r *http.Request) {
	m := reCatalogItem.FindStringSubmatch(r.URL.EscapedPath())
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
	body, err := s.Reads.CatalogItem(ctx, m[1])
	switch {
	case errors.Is(err, db.ErrCatalogItemNotFound):
		writeJSON(w, http.StatusNotFound, errorBody{Error: "no catalog item " + pythonRepr(m[1])})
	case err != nil:
		knowledgeFailed(w, "catalog", err)
	default:
		writeJSON(w, http.StatusOK, body)
	}
}

func (s *Server) handleProjectInstalls(w http.ResponseWriter, r *http.Request) {
	m := reProjectInstalls.FindStringSubmatch(r.URL.EscapedPath())
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
	body, err := s.Reads.ProjectInstalls(ctx, m[1])
	switch {
	case errors.Is(err, db.ErrProjectNotFound):
		missingProject(w, m[1])
	case err != nil:
		knowledgeFailed(w, "catalog", err)
	default:
		writeJSON(w, http.StatusOK, body)
	}
}
