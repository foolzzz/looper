package httpapi

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRegistryRoute(t *testing.T) {
	reads := &recordingReads{registry: map[string]any{"runtime_types": []any{}}}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	srv.ClaudeBin = "/tmp/loopgo-parity-bin/claude"
	srv.LoginEnviron = func() []string {
		return []string{"GROK_HOME=/tmp/loopgo-parity-grok-home", "CODEX_HOME=/tmp/loopgo-parity-codex-home"}
	}
	h := srv.Handler()

	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/registry"))
	if rr.Code != http.StatusOK || reads.registryClaude != "/tmp/loopgo-parity-bin/claude" || len(reads.registryEnv) != 2 {
		t.Fatalf("status %d claude %q env %v body %s", rr.Code, reads.registryClaude, reads.registryEnv, rr.Body.String())
	}

	before := len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodPost, "/api/registry"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` || len(reads.calls) != before {
		t.Fatalf("post status %d body %s calls %d", rr.Code, rr.Body.String(), len(reads.calls))
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodHead, "/api/registry"))
	if rr.Code != http.StatusNotFound || len(reads.calls) != before {
		t.Fatalf("head status %d calls %d", rr.Code, len(reads.calls))
	}

	broken := &recordingReads{registryErr: errors.New("dial SECRET")}
	rr = httptest.NewRecorder()
	handler := New("dev", &fakeDB{}, "")
	handler.Reads = broken
	handler.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/registry"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "the request failed; see the server log"}` {
		t.Fatalf("error status %d body %s", rr.Code, rr.Body.String())
	}

	rr = httptest.NewRecorder()
	New("dev", &fakeDB{}, "").Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/registry"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "the request failed; see the server log"}` {
		t.Fatalf("nil reads status %d body %s", rr.Code, rr.Body.String())
	}
}
