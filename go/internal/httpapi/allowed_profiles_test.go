package httpapi

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/foolzzz/looper/go/internal/db"
)

func TestAllowedProfilesRoute(t *testing.T) {
	reads := &recordingReads{allowedProfiles: map[string]any{
		"project_id": "proj-alpha0001",
		"restricted": true,
		"profiles":   []any{},
		"choosable":  []any{},
	}}
	h := tailHandler(reads)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/allowed-profiles"))
	if rr.Code != http.StatusOK || reads.projectIDSeen != "proj-alpha0001" || !strings.Contains(rr.Body.String(), `"restricted":true`) {
		t.Fatalf("status %d id %s body %s calls %v", rr.Code, reads.projectIDSeen, rr.Body.String(), reads.calls)
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodPost, "/api/projects/proj-alpha0001/allowed-profiles"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"not found"}` {
		t.Fatalf("post status %d body %s", rr.Code, rr.Body.String())
	}

	before := len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha000%31/allowed-profiles"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"not found"}` || len(reads.calls) != before {
		t.Fatalf("encoded status %d body %s calls %d", rr.Code, rr.Body.String(), len(reads.calls))
	}

	gone := &recordingReads{allowedProfilesErr: db.ErrProjectNotFound}
	rr = httptest.NewRecorder()
	tailHandler(gone).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-missing01/allowed-profiles"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"project 'proj-missing01' not found"}` {
		t.Fatalf("missing status %d body %s", rr.Code, rr.Body.String())
	}
}
