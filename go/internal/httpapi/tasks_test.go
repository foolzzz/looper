package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
)

// Status lists are written out. A reorder in the group tables should fail here.
var (
	inProgressStatuses = []string{"assigned", "running", "planning", "executing", "verifying", "stopping", "pending"}
	otherStatuses      = []string{
		"assigned", "running", "planning", "executing", "verifying", "stopping", "waiting_user",
		"pending", "init", "failed", "stopped", "completed", "cancelled",
	}
)

func TestParseTaskQuery(t *testing.T) {
	tests := []struct {
		name     string
		query    string
		want     db.TaskQuery
		wantErr  string
		outside  bool
		statuses []string
	}{
		{name: "default", want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "blank limit", query: "limit=", want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "needs you", query: "group=needs_you", statuses: []string{"waiting_user"}, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "in progress", query: "group=in_progress", statuses: inProgressStatuses, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "ready", query: "group=ready", statuses: []string{"init"}, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "attention", query: "group=attention", statuses: []string{"failed", "stopped"}, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "completed", query: "group=completed", statuses: []string{"completed"}, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "cancelled", query: "group=cancelled", statuses: []string{"cancelled"}, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "other", query: "group=other", statuses: otherStatuses, outside: true, want: db.TaskQuery{Limit: 50}},
		{name: "bad group", query: "group=nope", wantErr: errGroup},
		{name: "group case", query: "group=OTHER", wantErr: errGroup},
		{name: "bad status", query: "status=archived", wantErr: errStatus},
		{name: "status case", query: "status=Completed", wantErr: errStatus},
		{name: "status running", query: "status=running", want: db.TaskQuery{Status: "running", Inside: true, Limit: 50}},
		{name: "limit zero", query: "limit=0", wantErr: errLimit},
		{name: "limit 201", query: "limit=201", wantErr: errLimit},
		{name: "limit word", query: "limit=abc", wantErr: errLimit},
		{name: "limit negative", query: "limit=-1", wantErr: errLimit},
		{name: "limit plus", query: "limit=+1", wantErr: errLimit},
		{name: "limit fraction", query: "limit=1.5", wantErr: errLimit},
		{name: "limit one", query: "limit=1", want: db.TaskQuery{Inside: true, Limit: 1}},
		{name: "limit max", query: "limit=200", want: db.TaskQuery{Inside: true, Limit: 200}},
		{name: "limit leading zero", query: "limit=01", want: db.TaskQuery{Inside: true, Limit: 1}},
		{name: "limit two digits", query: "limit=02", want: db.TaskQuery{Inside: true, Limit: 2}},
		{name: "limit zeros", query: "limit=00", wantErr: errLimit},
		{name: "limit last wins", query: "limit=1&limit=2", want: db.TaskQuery{Inside: true, Limit: 2}},
		{name: "limit blank dropped", query: "limit=2&limit=", want: db.TaskQuery{Inside: true, Limit: 2}},
		{name: "group last wins", query: "group=needs_you&group=ready", statuses: []string{"init"}, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "group blank dropped", query: "group=ready&group=", statuses: []string{"init"}, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "bad group then good", query: "group=nope&group=needs_you", statuses: []string{"waiting_user"}, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "good group then bad", query: "group=needs_you&group=nope", wantErr: errGroup},
		{name: "group before status and limit", query: "group=nope&status=nope&limit=0", wantErr: errGroup},
		{name: "status before limit", query: "status=nope&limit=0", wantErr: errStatus},
		{name: "limit after a good group", query: "group=ready&limit=0", wantErr: errLimit},
		{
			name:     "filters",
			query:    "project_id=proj-beta00002&schedule_id=sched-nightly01&before=task-runalpha1&status=running&group=in_progress&limit=3",
			statuses: inProgressStatuses,
			want: db.TaskQuery{
				ProjectID: "proj-beta00002", Status: "running", ScheduleID: "sched-nightly01",
				Before: "task-runalpha1", Inside: true, Limit: 3,
			},
		},
		{name: "offset ignored", query: "offset=10&limit=4", want: db.TaskQuery{Inside: true, Limit: 4}},
		{name: "fullwidth two", query: "limit=" + url.QueryEscape("２"), want: db.TaskQuery{Inside: true, Limit: 2}},
		{name: "fullwidth zero", query: "limit=" + url.QueryEscape("０"), wantErr: errLimit},
		{name: "fullwidth max", query: "limit=" + url.QueryEscape("２００"), want: db.TaskQuery{Inside: true, Limit: 200}},
		{name: "fullwidth leading zero", query: "limit=" + url.QueryEscape("０２"), want: db.TaskQuery{Inside: true, Limit: 2}},
		{name: "superscript", query: "limit=" + url.QueryEscape("²"), wantErr: errLimit},
		{name: "digit superscript", query: "limit=" + url.QueryEscape("2²"), wantErr: errLimit},
		{name: "overflow", query: "limit=" + strings.Repeat("9", 40), wantErr: errLimit},
		// A raw semicolon or a bad % is part of the value. url.URL.Query drops
		// the pair, which used to fall through to the default list.
		{name: "limit semicolon", query: "limit=1;", wantErr: errLimit},
		{name: "limit semicolon and group", query: "limit=1;&group=needs_you", wantErr: errLimit},
		{name: "group semicolon", query: "group=completed;foo=bar", wantErr: errGroup},
		{name: "group semicolon after a good value", query: "group=ready&group=completed;x", wantErr: errGroup},
		{name: "limit bare percent", query: "limit=%", wantErr: errLimit},
		{name: "limit bad hex", query: "limit=%ZZ", wantErr: errLimit},
		{name: "limit short percent", query: "limit=%2", wantErr: errLimit},
		{name: "group trailing percent", query: "group=completed%", wantErr: errGroup},
		{name: "encoded semicolon", query: "limit=1%3B", wantErr: errLimit},
		{name: "percent encoded limit", query: "limit=%32", want: db.TaskQuery{Inside: true, Limit: 2}},
		{name: "percent encoded group", query: "group=%6e%65%65%64%73%5F%79%6F%75", statuses: []string{"waiting_user"}, want: db.TaskQuery{Inside: true, Limit: 50}},
		{name: "before replacement byte", query: "before=%FF", want: db.TaskQuery{Before: "\uFFFD", Inside: true, Limit: 50}},
		{name: "before unicode replacement", query: "before=%EF%BF%BD", want: db.TaskQuery{Before: "\uFFFD", Inside: true, Limit: 50}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			req := loopbackRequest(http.MethodGet, "/api/tasks")
			req.URL.RawQuery = tt.query
			got, msg, err := parseTaskQuery(req)
			if tt.wantErr != "" {
				if err == nil || msg != tt.wantErr {
					t.Fatalf("err %v msg %q, want %q", err, msg, tt.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			tt.want.Statuses = tt.statuses
			if tt.outside {
				tt.want.Inside = false
			}
			if !reflect.DeepEqual(got, tt.want) {
				t.Fatalf("got %+v\nwant %+v", got, tt.want)
			}
		})
	}
}

func TestPythonRepr(t *testing.T) {
	tests := []struct{ in, want string }{
		{"missing", `'missing'`},
		{"it's", `"it's"`},
		{"say \"hi\"", `'say "hi"'`},
		{"a'b\"c", `'a\'b"c'`},
		{"a\\b", `'a\\b'`},
		{"a\nb", `'a\nb'`},
		{"", `''`},
	}
	for _, tt := range tests {
		t.Run(tt.in, func(t *testing.T) {
			if got := pythonRepr(tt.in); got != tt.want {
				t.Fatalf("repr %q, want %q", got, tt.want)
			}
		})
	}
}

func TestTasksListUsesReader(t *testing.T) {
	reads := &recordingReads{tasksBody: map[string]any{
		"tasks": []any{}, "next_before": "task-asame0001", "counts": map[string]any{"running": 1},
	}}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks?group=in_progress&project_id=proj-alpha0001&status=running&schedule_id=sched-nightly01&limit=2"))
	const want = `{"counts":{"running":1},"next_before":"task-asame0001","tasks":[]}`
	if rr.Code != http.StatusOK || rr.Body.String() != want {
		t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
	}
	got := reads.tasksQuery
	wantQuery := db.TaskQuery{
		ProjectID: "proj-alpha0001", Status: "running", ScheduleID: "sched-nightly01",
		Statuses: inProgressStatuses, Inside: true, Limit: 2,
	}
	if !reflect.DeepEqual(got, wantQuery) {
		t.Fatalf("query %+v", got)
	}
	deadline, ok := reads.tasksCtx.Deadline()
	if !ok {
		t.Fatal("tasks read has no deadline")
	}
	limit := time.Until(deadline)
	if limit < readTimeout-200*time.Millisecond || limit > readTimeout+50*time.Millisecond {
		t.Fatalf("deadline %s, want about %s", limit, readTimeout)
	}
}

func TestTasksBefore(t *testing.T) {
	t.Run("known", func(t *testing.T) {
		reads := &recordingReads{exists: true}
		srv := New("dev", &fakeDB{}, "")
		srv.Reads = reads
		rr := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks?before=task-runalpha1"))
		if rr.Code != http.StatusOK {
			t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
		}
		if reads.existsID != "task-runalpha1" || reads.tasksQuery.Before != "task-runalpha1" {
			t.Fatalf("exists %q query %+v", reads.existsID, reads.tasksQuery)
		}
		if !reflect.DeepEqual(reads.calls, []string{"exists", "tasks"}) {
			t.Fatalf("calls %v", reads.calls)
		}
	})
	t.Run("percent FF", func(t *testing.T) {
		reads := &recordingReads{}
		srv := New("dev", &fakeDB{}, "")
		srv.Reads = reads
		rr := httptest.NewRecorder()
		req := loopbackRequest(http.MethodGet, "/api/tasks")
		req.URL.RawQuery = "before=%FF"
		srv.Handler().ServeHTTP(rr, req)
		id := "\uFFFD"
		want, err := json.Marshal(errorBody{Error: "before must be a task id; " + pythonRepr(id) + " is not one"})
		if err != nil {
			t.Fatal(err)
		}
		if rr.Code != http.StatusBadRequest || rr.Body.String() != string(want) {
			t.Fatalf("status %d body %s, want %s", rr.Code, rr.Body.String(), want)
		}
		if reads.existsID != id {
			t.Fatalf("exists id %q", reads.existsID)
		}
	})
	t.Run("unicode replacement", func(t *testing.T) {
		reads := &recordingReads{}
		srv := New("dev", &fakeDB{}, "")
		srv.Reads = reads
		rr := httptest.NewRecorder()
		req := loopbackRequest(http.MethodGet, "/api/tasks")
		req.URL.RawQuery = "before=%EF%BF%BD"
		srv.Handler().ServeHTTP(rr, req)
		id := "\uFFFD"
		if reads.existsID != id {
			t.Fatalf("exists id %q (%d runes)", reads.existsID, len([]rune(reads.existsID)))
		}
		want, err := json.Marshal(errorBody{Error: "before must be a task id; " + pythonRepr(id) + " is not one"})
		if err != nil {
			t.Fatal(err)
		}
		if rr.Code != http.StatusBadRequest || rr.Body.String() != string(want) {
			t.Fatalf("status %d body %s, want %s", rr.Code, rr.Body.String(), want)
		}
	})
	for _, id := range []string{"missing", "it's"} {
		t.Run(id, func(t *testing.T) {
			reads := &recordingReads{}
			srv := New("dev", &fakeDB{}, "")
			srv.Reads = reads
			logs := captureLogs(t)
			rr := httptest.NewRecorder()
			req := loopbackRequest(http.MethodGet, "/api/tasks")
			req.URL.RawQuery = "before=" + url.QueryEscape(id)
			srv.Handler().ServeHTTP(rr, req)
			want, err := json.Marshal(errorBody{Error: "before must be a task id; " + pythonRepr(id) + " is not one"})
			if err != nil {
				t.Fatal(err)
			}
			if rr.Code != http.StatusBadRequest || rr.Body.String() != string(want) {
				t.Fatalf("status %d body %s, want %s", rr.Code, rr.Body.String(), want)
			}
			if !reflect.DeepEqual(reads.calls, []string{"exists"}) {
				t.Fatalf("calls %v", reads.calls)
			}
			if strings.Contains(logs.String(), "database unavailable") {
				t.Fatal("unknown before was logged as a database error")
			}
		})
	}
}

func TestTasksErrorsDoNotLeak(t *testing.T) {
	tests := []struct {
		name string
		path string
		set  func(*recordingReads)
	}{
		{name: "exists", path: "/api/tasks?before=task-missing1", set: func(r *recordingReads) { r.existsErr = errors.New("dial SECRET") }},
		{name: "tasks", path: "/api/tasks", set: func(r *recordingReads) { r.tasksErr = errors.New("dial SECRET") }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reads := &recordingReads{}
			tt.set(reads)
			srv := New("dev", &fakeDB{}, "")
			srv.Reads = reads
			logs := captureLogs(t)
			rr := httptest.NewRecorder()
			srv.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, tt.path))
			if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"database unavailable"}` {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if strings.Contains(rr.Body.String(), "SECRET") || strings.Contains(logs.String(), "SECRET") {
				t.Fatal("response or log leaked SECRET")
			}
		})
	}
}

func TestTasksBadQuerySkipsDatabase(t *testing.T) {
	h := New("dev", &fakeDB{}, "").Handler()
	logs := captureLogs(t)
	tests := []struct{ path, msg string }{
		{"/api/tasks?group=nope", errGroup},
		{"/api/tasks?status=archived", errStatus},
		{"/api/tasks?limit=0", errLimit},
		{"/api/tasks?limit=1;", errLimit},
		{"/api/tasks?group=completed;foo=bar", errGroup},
		{"/api/tasks?limit=%", errLimit},
		{"/api/tasks?limit=%ZZ", errLimit},
		{"/api/tasks?limit=%2", errLimit},
		{"/api/tasks?group=completed%", errGroup},
		{"/api/tasks?limit=1;&group=needs_you", errLimit},
		{"/api/tasks?group=ready&group=completed;x", errGroup},
	}
	for _, tt := range tests {
		t.Run(tt.path, func(t *testing.T) {
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(http.MethodGet, tt.path))
			want := `{"error":"` + tt.msg + `"}`
			if rr.Code != http.StatusBadRequest || rr.Body.String() != want {
				t.Fatalf("status %d body %s, want %s", rr.Code, rr.Body.String(), want)
			}
		})
	}
	if strings.Contains(logs.String(), "database unavailable") {
		t.Fatal("400 was logged as a database error")
	}
}

func TestTasksOtherMethodsAreNotFound(t *testing.T) {
	reads := &recordingReads{}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	for _, method := range []string{http.MethodPost, http.MethodHead, http.MethodPut} {
		rr := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rr, loopbackRequest(method, "/api/tasks?limit=0"))
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"not found"}` {
			t.Fatalf("%s %d body %s", method, rr.Code, rr.Body.String())
		}
	}
	if len(reads.calls) != 0 {
		t.Fatalf("calls %v", reads.calls)
	}
}
