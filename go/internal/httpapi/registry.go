package httpapi

import (
	"log/slog"
	"net/http"

	"github.com/foolzzz/looper/go/internal/db"
)

func (s *Server) handleRegistry(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/registry" {
		notFound(w)
		return
	}
	if s.Reads == nil {
		registryFailed(w, errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.Registry(ctx, s.claudeBin(), s.loginEnviron())
	if err != nil {
		registryFailed(w, err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) claudeBin() string {
	if s == nil || s.ClaudeBin == "" {
		return "claude"
	}
	return s.ClaudeBin
}

func (s *Server) loginEnviron() []string {
	if s != nil && s.LoginEnviron != nil {
		return s.LoginEnviron()
	}
	return nil
}

// registryFailed is _handle_registry's uncaught Exception. The body is not
// the other reads' "internal server error".
func registryFailed(w http.ResponseWriter, err error) {
	slog.Error("registry request failed", "err", db.LogText(err))
	writeJSON(w, http.StatusInternalServerError, errorBody{Error: "the request failed; see the server log"})
}
