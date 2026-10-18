package httpapi

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
)

const notImplementedBody = `{"error":"not implemented in the Go server yet"}`

type recordingReads struct {
	total, running, completed int
	countsErr                 error
	overview                  map[string]any
	overviewErr               error
	projects                  map[string]any
	projectsErr               error
	calls                     []string
	since                     time.Time
}

func (r *recordingReads) StatusTaskCounts(context.Context) (int, int, int, error) {
	r.calls = append(r.calls, "counts")
	return r.total, r.running, r.completed, r.countsErr
}

func (r *recordingReads) Overview(_ context.Context, since time.Time) (map[string]any, error) {
	r.calls = append(r.calls, "overview")
	r.since = since
	return r.overview, r.overviewErr
}

func (r *recordingReads) Projects(context.Context) (map[string]any, error) {
	r.calls = append(r.calls, "projects")
	return r.projects, r.projectsErr
}

func (r *recordingReads) TaskExists(context.Context, string) (bool, error) {
	r.calls = append(r.calls, "exists")
	return false, errors.New("not used")
}

func (r *recordingReads) Tasks(context.Context, db.TaskQuery) (map[string]any, error) {
	r.calls = append(r.calls, "tasks")
	return nil, errors.New("not used")
}

func (r *recordingReads) Task(context.Context, string) (map[string]any, error) {
	r.calls = append(r.calls, "task")
	return nil, errors.New("not used")
}

func TestNotImplementedRoutes(t *testing.T) {
	reads := &recordingReads{}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	h := srv.Handler()
	tests := []struct {
		method string
		path   string
		want   int
		body   string
	}{
		{http.MethodGet, "/api/tasks?group=completed", http.StatusNotImplemented, notImplementedBody},
		{http.MethodPost, "/api/tasks", http.StatusNotImplemented, notImplementedBody},
		{http.MethodGet, "/api/tasks/task-abc123", http.StatusNotImplemented, notImplementedBody},
		{http.MethodPost, "/api/tasks/task-abc123", http.StatusNotImplemented, notImplementedBody},
		{http.MethodGet, "/api/tasks/create", http.StatusNotFound, `{"error":"not found"}`},
		{http.MethodGet, "/api/agents/worker-1/usage", http.StatusNotImplemented, notImplementedBody},
		{http.MethodGet, "/api/llm-records", http.StatusNotImplemented, notImplementedBody},
		{http.MethodPut, "/api/llm-records", http.StatusNotImplemented, notImplementedBody},
		{http.MethodGet, "/api/llm-records/12", http.StatusNotImplemented, notImplementedBody},
		{http.MethodGet, "/api/llm-records/12/events", http.StatusNotImplemented, notImplementedBody},
	}
	for _, tt := range tests {
		t.Run(tt.method+" "+tt.path, func(t *testing.T) {
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(tt.method, tt.path))
			if rr.Code != tt.want || rr.Body.String() != tt.body {
				t.Fatalf("status %d body %s, want %d %s", rr.Code, rr.Body.String(), tt.want, tt.body)
			}
			if ct := rr.Header().Get("Content-Type"); ct != "application/json" {
				t.Fatalf("content type %q", ct)
			}
		})
	}
	for _, call := range reads.calls {
		if call == "tasks" || call == "task" || call == "exists" {
			t.Fatalf("unimplemented route read the database: %v", reads.calls)
		}
	}
}

func TestReadEndpoints(t *testing.T) {
	reads := &recordingReads{
		total: 5, running: 2, completed: 1,
		overview: map[string]any{"note": "a < b & c"},
		projects: map[string]any{"projects": []any{}},
	}
	srv := New("1.2.3", &fakeDB{}, "")
	srv.Reads = reads
	h := srv.Handler()

	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/status"))
	const wantStatus = `{"status":"running","agents":{},"tasks":{"total":5,"running":2,"completed":1},"channels":{"web":{"channel":"web","healthy":false,"active_sse_streams":0}},"checks":[]}`
	if rr.Code != http.StatusOK || rr.Body.String() != wantStatus {
		t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
	}

	before := time.Now()
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/overview"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"note":"a < b & c"}` {
		t.Fatalf("overview %d body %s", rr.Code, rr.Body.String())
	}
	if strings.Contains(rr.Body.String(), "\n") {
		t.Fatal("json body has a trailing newline")
	}
	midnight := localMidnight(before)
	if reads.since.Sub(midnight) > 2*time.Second || midnight.Sub(reads.since) > 2*time.Second {
		t.Fatalf("since %s, local midnight %s", reads.since, midnight)
	}

	fixed := time.Date(2026, 6, 15, 12, 0, 0, 0, time.FixedZone("parity", 8*3600))
	srv.Now = func() time.Time { return fixed }
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/overview"))
	if rr.Code != http.StatusOK {
		t.Fatalf("clocked overview %d", rr.Code)
	}
	wantSince := time.Date(2026, 6, 15, 0, 0, 0, 0, fixed.Location())
	if !reads.since.Equal(wantSince) {
		t.Fatalf("since %s, want %s", reads.since, wantSince)
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/projects"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"projects":[]}` {
		t.Fatalf("projects %d body %s", rr.Code, rr.Body.String())
	}

	for _, path := range []string{"/api/overview", "/api/projects"} {
		rr = httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodPost, path))
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"not found"}` {
			t.Fatalf("%s post %d body %s", path, rr.Code, rr.Body.String())
		}
	}
}

func TestReadErrorsDoNotLeak(t *testing.T) {
	reads := &recordingReads{
		overviewErr: errors.New("dial SECRET"),
		projectsErr: errors.New("dial SECRET"),
	}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	h := srv.Handler()
	for _, path := range []string{"/api/overview", "/api/projects"} {
		t.Run(path, func(t *testing.T) {
			logs := captureLogs(t)
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
			if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"database unavailable"}` {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if strings.Contains(rr.Body.String(), "SECRET") || strings.Contains(logs.String(), "SECRET") {
				t.Fatal("response or log leaked SECRET")
			}
		})
	}
}

func TestReadContextDeadline(t *testing.T) {
	tests := []struct {
		name   string
		parent time.Duration
		want   time.Duration
	}{
		{name: "no parent deadline", want: readTimeout},
		{name: "parent hours away", parent: 2 * time.Hour, want: readTimeout},
		{name: "parent sooner", parent: 5 * time.Second, want: 5 * time.Second},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			parent := context.Background()
			if tt.parent > 0 {
				var cancel context.CancelFunc
				parent, cancel = context.WithTimeout(parent, tt.parent)
				t.Cleanup(cancel)
			}
			req := loopbackRequest(http.MethodGet, "/api/projects")
			req = req.WithContext(parent)
			ctx, cancel := readContext(req)
			t.Cleanup(cancel)
			deadline, ok := ctx.Deadline()
			if !ok {
				t.Fatal("read context has no deadline")
			}
			limit := time.Until(deadline)
			if limit < tt.want-200*time.Millisecond || limit > tt.want+50*time.Millisecond {
				t.Fatalf("deadline %s, want about %s", limit, tt.want)
			}
		})
	}
}

func TestReadContextFollowsParentCancel(t *testing.T) {
	parent, cancelParent := context.WithCancel(context.Background())
	req := loopbackRequest(http.MethodGet, "/api/projects")
	req = req.WithContext(parent)
	ctx, cancel := readContext(req)
	defer cancel()
	cancelParent()
	select {
	case <-ctx.Done():
		if !errors.Is(ctx.Err(), context.Canceled) {
			t.Fatalf("err %v", ctx.Err())
		}
	case <-time.After(time.Second):
		t.Fatal("parent cancel did not reach the read context")
	}
}

func TestReadWithoutDatabase(t *testing.T) {
	h := New("dev", &fakeDB{}, "").Handler()
	for _, path := range []string{"/api/overview", "/api/projects"} {
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
		if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"database unavailable"}` {
			t.Fatalf("%s %d body %s", path, rr.Code, rr.Body.String())
		}
	}
}
