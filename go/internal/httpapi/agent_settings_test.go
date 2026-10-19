package httpapi

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/foolzzz/looper/go/internal/db"
)

func TestAgentSettingsRoute(t *testing.T) {
	reads := &recordingReads{
		agentSettings:        map[string]any{"marker": "global-settings"},
		projectAgentSettings: map[string]any{"project_id": "proj-alpha0001", "marker": "alpha-settings"},
	}
	h := tailHandler(reads)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agent-settings"))
	if rr.Code != http.StatusOK || !strings.Contains(rr.Body.String(), `"marker":"global-settings"`) || len(reads.calls) != 1 || reads.calls[0] != "agent-settings" {
		t.Fatalf("status %d body %s calls %v", rr.Code, rr.Body.String(), reads.calls)
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/agent-settings"))
	if rr.Code != http.StatusOK || reads.projectIDSeen != "proj-alpha0001" || !strings.Contains(rr.Body.String(), `"marker":"alpha-settings"`) {
		t.Fatalf("project status %d id %s body %s", rr.Code, reads.projectIDSeen, rr.Body.String())
	}

	for _, method := range []string{http.MethodPost, http.MethodHead} {
		rr = httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(method, "/api/agent-settings"))
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` {
			t.Fatalf("%s status %d body %s", method, rr.Code, rr.Body.String())
		}
		rr = httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(method, "/api/projects/proj-alpha0001/agent-settings"))
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` {
			t.Fatalf("project %s status %d body %s", method, rr.Code, rr.Body.String())
		}
	}

	before := len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha000%31/agent-settings"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` || len(reads.calls) != before {
		t.Fatalf("encoded status %d body %s calls %d", rr.Code, rr.Body.String(), len(reads.calls))
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agent-settings/roles/worker"))
	if rr.Code != http.StatusNotFound || len(reads.calls) != before {
		t.Fatalf("role path status %d calls %d", rr.Code, len(reads.calls))
	}

	gone := &recordingReads{projectAgentSettingsErr: db.ErrProjectNotFound}
	rr = httptest.NewRecorder()
	tailHandler(gone).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-missing01/agent-settings"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "project 'proj-missing01' not found"}` {
		t.Fatalf("missing status %d body %s", rr.Code, rr.Body.String())
	}

	broken := &recordingReads{agentSettingsErr: errors.New("dial SECRET"), projectAgentSettingsErr: errors.New("dial SECRET")}
	rr = httptest.NewRecorder()
	tailHandler(broken).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agent-settings"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "internal server error"}` || strings.Contains(rr.Body.String(), "SECRET") {
		t.Fatalf("error status %d body %s", rr.Code, rr.Body.String())
	}

	none := tailHandler(nil)
	rr = httptest.NewRecorder()
	none.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agent-settings"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "internal server error"}` {
		t.Fatalf("nil status %d body %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	none.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/agent-settings"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "internal server error"}` {
		t.Fatalf("nil project status %d body %s", rr.Code, rr.Body.String())
	}
}
