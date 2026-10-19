package httpapi

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPythonInt(t *testing.T) {
	tests := []struct {
		name     string
		in       string
		want     int64
		overflow int
		wantOK   bool
	}{
		{name: "plain", in: "42", want: 42, wantOK: true},
		{name: "underscores", in: "1_000", want: 1000, wantOK: true},
		{name: "sign and space", in: "  -10 ", want: -10, wantOK: true},
		{name: "plus", in: "+7", want: 7, wantOK: true},
		{name: "fullwidth", in: "２", want: 2, wantOK: true},
		{name: "leading zeros", in: "01", want: 1, wantOK: true},
		{name: "zero", in: "0", want: 0, wantOK: true},
		{name: "negative zero", in: "-0", want: 0, wantOK: true},
		{name: "blank", in: "  ", wantOK: false},
		{name: "empty", in: "", wantOK: false},
		{name: "fraction", in: "1.5", wantOK: false},
		{name: "word", in: "many", wantOK: false},
		{name: "trailing underscore", in: "1_", wantOK: false},
		{name: "double underscore", in: "1__0", wantOK: false},
		{name: "leading underscore", in: "_1", wantOK: false},
		{name: "fullwidth plus", in: "＋2", wantOK: false},
		{name: "positive overflow", in: "1" + strings.Repeat("0", 30), overflow: 1, wantOK: true},
		{name: "negative overflow", in: "-" + strings.Repeat("9", 30), overflow: -1, wantOK: true},
		{name: "digit limit", in: strings.Repeat("9", pythonIntMaxStrDigits), overflow: 1, wantOK: true},
		{name: "digit limit with sign and space", in: " -" + strings.Repeat("9", pythonIntMaxStrDigits) + " ", overflow: -1, wantOK: true},
		{name: "digit limit ignores underscores", in: "1_" + strings.Repeat("0", pythonIntMaxStrDigits-1), overflow: 1, wantOK: true},
		{name: "digit limit leading zeros", in: strings.Repeat("0", pythonIntMaxStrDigits), want: 0, wantOK: true},
		{name: "one past digit limit", in: strings.Repeat("9", pythonIntMaxStrDigits+1), wantOK: false},
		{name: "one past digit limit with sign", in: "-" + strings.Repeat("9", pythonIntMaxStrDigits+1), wantOK: false},
		{name: "underscore does not hide an extra digit", in: "1_" + strings.Repeat("0", pythonIntMaxStrDigits), wantOK: false},
		// str.isspace() is true for U+001C–U+001F. int() rejects them.
		{name: "file separator", in: "\x1c2", wantOK: false},
		{name: "group separator", in: "\x1d2\x1e", wantOK: false},
		{name: "record separator", in: "\x1e2", wantOK: false},
		{name: "unit separator", in: "2\x1f", wantOK: false},
		{name: "nbsp", in: "\u00a02", want: 2, wantOK: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, overflow, ok := pythonInt(tt.in)
			if ok != tt.wantOK || overflow != tt.overflow || (ok && overflow == 0 && got != tt.want) {
				t.Fatalf("pythonInt(%q) = %d, %d, %v", tt.in, got, overflow, ok)
			}
		})
	}
}

func TestRunRole(t *testing.T) {
	tests := []struct {
		name string
		raw  any
		want map[string]any
	}{
		{
			name: "work",
			raw:  map[string]any{"id": "backend", "kind": "work", "version": float64(2)},
			want: map[string]any{"id": "backend", "kind": "work", "version": 2},
		},
		{
			name: "review and truncated version",
			raw:  map[string]any{"id": float64(1), "kind": "review", "version": float64(2.9)},
			want: map[string]any{"id": "1", "kind": "review", "version": 2},
		},
		{name: "bad kind", raw: map[string]any{"id": "backend", "kind": "other", "version": float64(1)}},
		{name: "missing version", raw: map[string]any{"id": "backend", "kind": "work"}},
		{name: "not a map", raw: "backend"},
		{name: "string version", raw: map[string]any{"id": "backend", "kind": "work", "version": "2"},
			want: map[string]any{"id": "backend", "kind": "work", "version": 2}},
		{name: "fraction string", raw: map[string]any{"id": "backend", "kind": "work", "version": "2.5"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := runRole(tt.raw)
			if tt.want == nil {
				if got != nil {
					t.Fatalf("got %#v", got)
				}
				return
			}
			if got["id"] != tt.want["id"] || got["kind"] != tt.want["kind"] || got["version"] != tt.want["version"] {
				t.Fatalf("got %#v, want %#v", got, tt.want)
			}
		})
	}
	record := map[string]any{"metadata": map[string]any{"prompt_layers": map[string]any{
		"task_role": map[string]any{"id": "backend", "kind": "work", "version": float64(2)},
	}}}
	with := withRunRole(record)
	role := with["task_role"].(map[string]any)
	if role["id"] != "backend" || role["version"] != 2 {
		t.Fatalf("record %#v", with)
	}
	plain := withRunRole(map[string]any{"metadata": map[string]any{}})
	if _, ok := plain["task_role"]; ok {
		t.Fatal("built-in role kept task_role")
	}
}

func TestRecordRoutes(t *testing.T) {
	reads := &recordingReads{
		usage:  map[string]any{"agent_id": "usage-agent01"},
		record: map[string]any{"id": int64(12), "metadata": map[string]any{}},
		records: []map[string]any{{
			"id": int64(1),
			"metadata": map[string]any{"prompt_layers": map[string]any{
				"task_role": map[string]any{"id": "backend", "kind": "work", "version": float64(2)},
			}},
		}},
		events: []map[string]any{{"seq": int64(2)}},
	}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	h := srv.Handler()

	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agents/usage-agent01/usage"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"agent_id":"usage-agent01"}` || reads.usageTurns != 100 {
		t.Fatalf("usage %d %s turns %d", rr.Code, rr.Body.String(), reads.usageTurns)
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agents/usage-agent01/usage?turns=0"))
	if rr.Code != http.StatusOK || reads.usageTurns != 1 {
		t.Fatalf("turns 0 -> %d status %d", reads.usageTurns, rr.Code)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agents/usage-agent01/usage?turns=1001"))
	if reads.usageTurns != 1000 {
		t.Fatalf("turns cap %d", reads.usageTurns)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agents/usage-agent01/usage?turns=２"))
	if rr.Code != http.StatusOK || reads.usageTurns != 2 {
		t.Fatalf("fullwidth turns %d status %d", reads.usageTurns, rr.Code)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agents/usage-agent01/usage?turns=many"))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error":"turns must be an integer"}` {
		t.Fatalf("bad turns %d %s", rr.Code, rr.Body.String())
	}
	for _, enc := range []string{"%1C", "%1D", "%1E", "%1F"} {
		before := len(reads.calls)
		rr = httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agents/usage-agent01/usage?turns="+enc+"2"))
		if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error":"turns must be an integer"}` || len(reads.calls) != before {
			t.Fatalf("turns %s %d %s calls %d", enc, rr.Code, rr.Body.String(), len(reads.calls)-before)
		}
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records?agent_id=usage-agent01&role=worker&limit=1_000&offset=2"))
	if rr.Code != http.StatusOK || reads.recordQuery.Limit != 500 || reads.recordQuery.Offset != 2 || reads.recordQuery.Role != "worker" {
		t.Fatalf("list query %+v status %d body %s", reads.recordQuery, rr.Code, rr.Body.String())
	}
	if !strings.Contains(rr.Body.String(), `"task_role":{"id":"backend","kind":"work","version":2}`) {
		t.Fatalf("list body %s", rr.Body.String())
	}

	before := len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records?limit=many"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"internal server error"}` || len(reads.calls) != before {
		t.Fatalf("bad limit %d %s calls %v", rr.Code, rr.Body.String(), reads.calls)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records?offset=1.5"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"internal server error"}` {
		t.Fatalf("bad offset %d %s", rr.Code, rr.Body.String())
	}
	for _, enc := range []string{"%1C", "%1D", "%1E", "%1F"} {
		before := len(reads.calls)
		rr = httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records?limit="+enc+"2"))
		if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"internal server error"}` || len(reads.calls) != before {
			t.Fatalf("limit %s %d %s calls %d", enc, rr.Code, rr.Body.String(), len(reads.calls)-before)
		}
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/01"))
	if rr.Code != http.StatusOK || reads.recordID != 1 || !strings.Contains(rr.Body.String(), `"record":`) {
		t.Fatalf("record %d id %d body %s", rr.Code, reads.recordID, rr.Body.String())
	}
	reads.record = nil
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/01"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"llm record 1 not found"}` {
		t.Fatalf("missing %d %s", rr.Code, rr.Body.String())
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/12/events?after=1&limit=2"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"events":[{"seq":2}]}` || reads.eventsAfter != 1 || reads.eventsLimit != 2 {
		t.Fatalf("events %d %s after %d limit %d", rr.Code, rr.Body.String(), reads.eventsAfter, reads.eventsLimit)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/12/events?after=nope"))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error":"after and limit must be integers"}` {
		t.Fatalf("bad events %d %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/12/events?limit=-1"))
	if rr.Code != http.StatusOK || reads.eventsLimit != -1 {
		t.Fatalf("negative limit %d passed %d", rr.Code, reads.eventsLimit)
	}
}

func TestRecordIntegerDigitLimit(t *testing.T) {
	reads := &recordingReads{
		usage:   map[string]any{"agent_id": "usage-agent01"},
		record:  map[string]any{"id": int64(12)},
		records: []map[string]any{{"id": int64(1)}},
		events:  []map[string]any{{"seq": int64(2)}},
	}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	h := srv.Handler()
	oversized := strings.Repeat("9", 40)
	past := strings.Repeat("9", pythonIntMaxStrDigits+1)
	atLimit := strings.Repeat("9", pythonIntMaxStrDigits)

	before := len(reads.calls)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agents/usage-agent01/usage?turns="+atLimit))
	if rr.Code != http.StatusOK || reads.usageTurns != usageTurnsMax || len(reads.calls) != before+1 {
		t.Fatalf("4300-digit turns %d turns %d calls %d body %s", rr.Code, reads.usageTurns, len(reads.calls)-before, rr.Body.String())
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agents/usage-agent01/usage?turns="+past))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error":"turns must be an integer"}` || len(reads.calls) != before {
		t.Fatalf("4301-digit turns %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}

	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records?limit="+past))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"internal server error"}` || len(reads.calls) != before {
		t.Fatalf("4301-digit records limit %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}

	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/"+oversized+"/events?after=nope"))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error":"after and limit must be integers"}` || len(reads.calls) != before {
		t.Fatalf("oversized id after=nope %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/"+oversized+"/events?after=1"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"database unavailable"}` || len(reads.calls) != before {
		t.Fatalf("oversized id valid after %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/12/events?limit="+past))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error":"after and limit must be integers"}` || len(reads.calls) != before {
		t.Fatalf("4301-digit events limit %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/12/events?limit="+atLimit))
	if rr.Code != http.StatusOK || reads.eventsLimit != eventsLimitMax || len(reads.calls) != before+1 {
		t.Fatalf("4300-digit events limit %d limit %d calls %d", rr.Code, reads.eventsLimit, len(reads.calls)-before)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/"+past+"/events?after=nope"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"internal server error"}` || len(reads.calls) != before {
		t.Fatalf("4301-digit id %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/"+past))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"internal server error"}` || len(reads.calls) != before {
		t.Fatalf("4301-digit record %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
}

func TestRecordHEADAndEncodedListAreNotFound(t *testing.T) {
	reads := &recordingReads{
		usage:   map[string]any{"agent_id": "usage-agent01"},
		record:  map[string]any{"id": int64(12)},
		records: []map[string]any{{"id": int64(1)}},
		events:  []map[string]any{{"seq": int64(1)}},
	}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	h := srv.Handler()
	for _, path := range []string{
		"/api/agents/usage-agent01/usage",
		"/api/llm-records",
		"/api/llm-records/12",
		"/api/llm-records/12/events",
	} {
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodHead, path))
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"not found"}` {
			t.Fatalf("HEAD %s %d %s", path, rr.Code, rr.Body.String())
		}
	}
	for _, path := range []string{"/api/llm%2drecords", "/api/llm%2Drecords", "/api/llm%2drecords?limit=1"} {
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"not found"}` {
			t.Fatalf("GET %s %d %s", path, rr.Code, rr.Body.String())
		}
	}
	if len(reads.calls) != 0 {
		t.Fatalf("not-found routes read the database: %v", reads.calls)
	}
}

func TestListRecordErrorsStayEmpty(t *testing.T) {
	reads := &recordingReads{recordsErr: errors.New("dial SECRET")}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	logs := captureLogs(t)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"records":[]}` {
		t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
	}
	if strings.Contains(rr.Body.String(), "SECRET") || strings.Contains(logs.String(), "SECRET") {
		t.Fatal("response or log leaked SECRET")
	}
}

func TestRecordDatabaseErrors(t *testing.T) {
	reads := &recordingReads{
		usageErr:  errors.New("dial SECRET"),
		recordErr: errors.New("dial SECRET"),
		eventsErr: errors.New("dial SECRET"),
	}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	logs := captureLogs(t)
	h := srv.Handler()
	for _, path := range []string{"/api/agents/usage-agent01/usage", "/api/llm-records/12", "/api/llm-records/12/events"} {
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
		if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"database unavailable"}` {
			t.Fatalf("%s %d %s", path, rr.Code, rr.Body.String())
		}
	}
	if strings.Contains(logs.String(), "SECRET") {
		t.Fatal("log leaked SECRET")
	}
}
