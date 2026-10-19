package httpapi

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
)

func TestReadTailRouting(t *testing.T) {
	reads := &recordingReads{}
	h := tailHandler(reads)
	tests := []struct {
		method, path string
	}{
		{http.MethodPost, "/api/roles"},
		{http.MethodPut, "/api/roles/planner"},
		{http.MethodHead, "/api/gate-settings"},
		{http.MethodPost, "/api/channel-configs"},
		{http.MethodPut, "/api/channel-configs/lark"},
		{http.MethodGet, "/api/%72oles"},
		{http.MethodGet, "/api/roles/pl%61nner"},
		{http.MethodGet, "/api/roles/planner/reset-prompt"},
		{http.MethodGet, "/api/projects/nope/role-templates"},
		{http.MethodGet, "/api/projects/proj-/role-stats"},
		{http.MethodGet, "/api/projects/proj-alpha0001/role-templates/backend"},
	}
	for _, tt := range tests {
		t.Run(tt.method+" "+tt.path, func(t *testing.T) {
			before := len(reads.calls)
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(tt.method, tt.path))
			if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"not found"}` {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if len(reads.calls) != before {
				t.Fatalf("calls %v", reads.calls)
			}
		})
	}
}

func TestReadTailNilReads(t *testing.T) {
	h := tailHandler(nil)
	logs := captureLogs(t)
	tests := []struct {
		path, body string
		status     int
	}{
		{path: "/api/roles", status: http.StatusOK, body: `{"configs":[]}`},
		{path: "/api/roles/planner", status: http.StatusNotFound, body: `{"error":"role 'planner' not found"}`},
		{path: "/api/channel-configs", status: http.StatusOK, body: `{"configs":[]}`},
		{path: "/api/channel-configs/lark", status: http.StatusNotFound, body: `{"error":"channel 'lark' not found"}`},
		{path: "/api/gate-settings", status: http.StatusInternalServerError, body: `{"error":"internal server error"}`},
		{path: "/api/projects/proj-alpha0001/role-templates", status: http.StatusInternalServerError, body: `{"error":"internal server error"}`},
		{path: "/api/projects/proj-alpha0001/role-stats?since=nope", status: http.StatusInternalServerError, body: `{"error":"internal server error"}`},
	}
	for _, tt := range tests {
		t.Run(tt.path, func(t *testing.T) {
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(http.MethodGet, tt.path))
			if rr.Code != tt.status || rr.Body.String() != tt.body {
				t.Fatalf("status %d body %s, want %d %s", rr.Code, rr.Body.String(), tt.status, tt.body)
			}
		})
	}
	if strings.Contains(logs.String(), "role config") || strings.Contains(logs.String(), "channel config") {
		t.Fatalf("nil read was logged: %s", logs.String())
	}
}

func TestReadTailDatabaseErrors(t *testing.T) {
	secret := errors.New("SECRET")
	tests := []struct {
		name   string
		reads  *recordingReads
		path   string
		status int
		body   string
		calls  []string
		log    string
	}{
		{
			name: "role list", reads: &recordingReads{roleConfigsErr: secret},
			path: "/api/roles", status: http.StatusOK, body: `{"configs":[]}`,
			calls: []string{"roles"}, log: "role configs",
		},
		{
			name: "role get", reads: &recordingReads{roleConfigErr: secret},
			path: "/api/roles/planner", status: http.StatusNotFound,
			body:  `{"error":"role 'planner' not found"}`,
			calls: []string{"role"}, log: "role config",
		},
		{
			name: "channel list", reads: &recordingReads{channelConfigsErr: secret},
			path: "/api/channel-configs", status: http.StatusOK, body: `{"configs":[]}`,
			calls: []string{"channels"}, log: "channel configs",
		},
		{
			name: "channel get", reads: &recordingReads{channelConfigErr: secret},
			path: "/api/channel-configs/lark", status: http.StatusNotFound,
			body:  `{"error":"channel 'lark' not found"}`,
			calls: []string{"channel"}, log: "channel config",
		},
		{
			name: "gate", reads: &recordingReads{gateErr: secret},
			path: "/api/gate-settings", status: http.StatusInternalServerError,
			body:  `{"error":"internal server error"}`,
			calls: []string{"gate"}, log: "gate settings",
		},
		{
			name: "templates", reads: &recordingReads{templatesErr: secret},
			path:   "/api/projects/proj-alpha0001/role-templates",
			status: http.StatusInternalServerError, body: `{"error":"internal server error"}`,
			calls: []string{"templates"}, log: "role templates",
		},
		{
			name: "stats exists", reads: &recordingReads{projectExistsErr: secret},
			path:   "/api/projects/proj-alpha0001/role-stats?since=nope",
			status: http.StatusInternalServerError, body: `{"error":"internal server error"}`,
			calls: []string{"project-exists"}, log: "role stats",
		},
		{
			name: "stats query", reads: &recordingReads{projectOK: true, roleStatsErr: secret},
			path:   "/api/projects/proj-alpha0001/role-stats",
			status: http.StatusInternalServerError, body: `{"error":"internal server error"}`,
			calls: []string{"project-exists", "role-stats"}, log: "role stats",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			logs := captureLogs(t)
			rr := httptest.NewRecorder()
			tailHandler(tt.reads).ServeHTTP(rr, loopbackRequest(http.MethodGet, tt.path))
			if rr.Code != tt.status || rr.Body.String() != tt.body {
				t.Fatalf("status %d body %s, want %d %s", rr.Code, rr.Body.String(), tt.status, tt.body)
			}
			if strings.Join(tt.reads.calls, ",") != strings.Join(tt.calls, ",") {
				t.Fatalf("calls %v, want %v", tt.reads.calls, tt.calls)
			}
			if strings.Contains(rr.Body.String(), "SECRET") || strings.Contains(logs.String(), "SECRET") {
				t.Fatalf("leaked SECRET: body %s log %s", rr.Body.String(), logs.String())
			}
			// LogText of a plain error is ErrorText: "error: unreachable".
			if !strings.Contains(logs.String(), tt.log) || !strings.Contains(logs.String(), "error: unreachable") {
				t.Fatalf("log %s, want %s and error: unreachable", logs.String(), tt.log)
			}
		})
	}
}

func TestRoleStatsSince(t *testing.T) {
	logs := captureLogs(t)
	missing := &recordingReads{}
	rr := httptest.NewRecorder()
	tailHandler(missing).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-missing01/role-stats?since=nope"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"project 'proj-missing01' not found"}` {
		t.Fatalf("missing status %d body %s", rr.Code, rr.Body.String())
	}
	if strings.Join(missing.calls, ",") != "project-exists" {
		t.Fatalf("calls %v", missing.calls)
	}

	bad := &recordingReads{projectOK: true}
	rr = httptest.NewRecorder()
	tailHandler(bad).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/role-stats?since=nope"))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error":"since must be a day (YYYY-MM-DD), not 'nope'"}` {
		t.Fatalf("bad status %d body %s", rr.Code, rr.Body.String())
	}
	if strings.Join(bad.calls, ",") != "project-exists" {
		t.Fatalf("calls %v", bad.calls)
	}

	week := &recordingReads{projectOK: true}
	rr = httptest.NewRecorder()
	tailHandler(week).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/role-stats?since=2010-W01-1"))
	if rr.Code != http.StatusOK {
		t.Fatalf("week status %d body %s", rr.Code, rr.Body.String())
	}
	wantDay := time.Date(2010, 1, 4, 0, 0, 0, 0, time.Local)
	if week.roleStatsSince == nil || !week.roleStatsSince.Equal(wantDay) {
		t.Fatalf("since %#v, want local midnight %s", week.roleStatsSince, wantDay)
	}
	if week.roleStatsProject != "proj-alpha0001" {
		t.Fatalf("project %s", week.roleStatsProject)
	}

	blank := &recordingReads{projectOK: true}
	rr = httptest.NewRecorder()
	tailHandler(blank).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/role-stats?since="))
	if rr.Code != http.StatusOK || blank.roleStatsSince != nil {
		t.Fatalf("blank status %d since %#v body %s", rr.Code, blank.roleStatsSince, rr.Body.String())
	}

	last := &recordingReads{projectOK: true}
	rr = httptest.NewRecorder()
	tailHandler(last).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/role-stats?since=nope&since=2010-01-01"))
	wantJan := time.Date(2010, 1, 1, 0, 0, 0, 0, time.Local)
	if rr.Code != http.StatusOK || last.roleStatsSince == nil || !last.roleStatsSince.Equal(wantJan) {
		t.Fatalf("last status %d since %#v", rr.Code, last.roleStatsSince)
	}

	longRaw := strings.Repeat("x", 39) + "YZ"
	long := &recordingReads{projectOK: true}
	rr = httptest.NewRecorder()
	tailHandler(long).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/role-stats?since="+longRaw))
	wantErr := `{"error":"` + sinceError(longRaw) + `"}`
	if rr.Code != http.StatusBadRequest || rr.Body.String() != wantErr {
		t.Fatalf("long status %d body %s, want %s", rr.Code, rr.Body.String(), wantErr)
	}
	if strings.Contains(rr.Body.String(), "YZ") {
		t.Fatalf("body kept the 41st character: %s", rr.Body.String())
	}

	quote := &recordingReads{projectOK: true}
	rr = httptest.NewRecorder()
	tailHandler(quote).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/role-stats?since="+`bad'day`))
	if rr.Body.String() != `{"error":"since must be a day (YYYY-MM-DD), not \"bad'day\""}` {
		t.Fatalf("quote body %s", rr.Body.String())
	}

	gone := &recordingReads{templatesErr: db.ErrProjectNotFound}
	rr = httptest.NewRecorder()
	tailHandler(gone).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-missing01/role-templates"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"project 'proj-missing01' not found"}` {
		t.Fatalf("templates status %d body %s", rr.Code, rr.Body.String())
	}
	if strings.Contains(logs.String(), "role stats") || strings.Contains(logs.String(), "role templates") {
		t.Fatalf("a 400 or 404 was logged: %s", logs.String())
	}
}

func TestReadTailPassesIDs(t *testing.T) {
	reads := &recordingReads{
		roleConfig:    map[string]any{"role": "planner"},
		channelConfig: map[string]any{"channel_id": "lark"},
		projectOK:     true,
		templates:     map[string]any{"project_id": "proj-alpha0001"},
	}
	h := tailHandler(reads)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/roles/planner"))
	if rr.Code != http.StatusOK || reads.roleName != "planner" {
		t.Fatalf("role status %d name %s body %s", rr.Code, reads.roleName, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/channel-configs/telegram-empty"))
	if rr.Code != http.StatusOK || reads.channelID != "telegram-empty" {
		t.Fatalf("channel status %d id %s", rr.Code, reads.channelID)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects/proj-alpha0001/role-templates"))
	if rr.Code != http.StatusOK || reads.projectIDSeen != "proj-alpha0001" {
		t.Fatalf("templates status %d id %s body %s", rr.Code, reads.projectIDSeen, rr.Body.String())
	}
}

func tailHandler(reads Reader) http.Handler {
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	return srv.Handler()
}
