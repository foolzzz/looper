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

type recordingReads struct {
	total, running, completed int
	countsErr                 error
	overview                  map[string]any
	overviewErr               error
	projects                  map[string]any
	projectsErr               error
	tasksBody                 map[string]any
	tasksErr                  error
	tasksQuery                db.TaskQuery
	tasksCtx                  context.Context
	exists                    bool
	existsErr                 error
	existsID                  string
	taskBody                  map[string]any
	taskErr                   error
	taskID                    string
	taskCtx                   context.Context
	usage                     map[string]any
	usageErr                  error
	usageAgent                string
	usageTurns                int
	records                   []map[string]any
	recordsErr                error
	recordQuery               db.RecordQuery
	record                    map[string]any
	recordErr                 error
	recordID                  int64
	events                    []map[string]any
	eventsErr                 error
	eventsID                  int64
	eventsAfter               int64
	eventsLimit               int64
	chatTargets               []map[string]any
	chatTargetsErr            error
	sessions                  []map[string]any
	sessionsErr               error
	messages                  []map[string]any
	messagesErr               error
	messageSession            string
	activity                  []map[string]any
	activityErr               error
	activityTask              string
	activitySince             int64
	activityLimit             int64
	activityRow               map[string]any
	activityRowErr            error
	activitySeq               int64
	conversation              []map[string]any
	conversationErr           error
	conversationTask          string
	roleConfigs               []map[string]any
	roleConfigsErr            error
	roleConfig                map[string]any
	roleConfigErr             error
	roleName                  string
	channelConfigs            []map[string]any
	channelConfigsErr         error
	channelConfig             map[string]any
	channelConfigErr          error
	channelID                 string
	gate                      map[string]any
	gateErr                   error
	projectOK                 bool
	projectExistsErr          error
	projectIDSeen             string
	templates                 map[string]any
	templatesErr              error
	allowedProfiles           map[string]any
	allowedProfilesErr        error
	roleStatsBody             map[string]any
	roleStatsErr              error
	roleStatsProject          string
	roleStatsSince            *time.Time
	gateMessages              map[string]any
	gateMessagesErr           error
	gateTask                  string
	gateLabel                 string
	roomBody                  map[string]any
	roomErr                   error
	roomTask                  string
	roomQuery                 db.RoomQuery
	calls                     []string
	since                     time.Time
	scheduleErr               error
	scheduleQuery             db.ScheduleRunsQuery
	scheduleLimit             int
	workspaceProject          db.ProjectWorkspace
	workspaceErr              error
	active                    []db.ActiveWorkspace
	activeErr                 error
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

func (r *recordingReads) TaskExists(_ context.Context, id string) (bool, error) {
	r.calls = append(r.calls, "exists")
	r.existsID = id
	if r.existsErr != nil {
		return false, r.existsErr
	}
	return r.exists, nil
}

func (r *recordingReads) Tasks(ctx context.Context, q db.TaskQuery) (map[string]any, error) {
	r.calls = append(r.calls, "tasks")
	r.tasksQuery = q
	r.tasksCtx = ctx
	if r.tasksErr != nil {
		return nil, r.tasksErr
	}
	if r.tasksBody == nil {
		return map[string]any{"tasks": []any{}, "next_before": nil, "counts": map[string]any{}}, nil
	}
	return r.tasksBody, nil
}

func (r *recordingReads) Task(ctx context.Context, id string) (map[string]any, error) {
	r.calls = append(r.calls, "task")
	r.taskID = id
	r.taskCtx = ctx
	if r.taskErr != nil {
		return nil, r.taskErr
	}
	if r.taskBody == nil {
		return map[string]any{}, nil
	}
	return r.taskBody, nil
}

func (r *recordingReads) AgentUsage(_ context.Context, agentID string, turns int) (map[string]any, error) {
	r.calls = append(r.calls, "usage")
	r.usageAgent = agentID
	r.usageTurns = turns
	if r.usageErr != nil {
		return nil, r.usageErr
	}
	if r.usage == nil {
		return map[string]any{}, nil
	}
	return r.usage, nil
}

func (r *recordingReads) ListRecords(_ context.Context, q db.RecordQuery) ([]map[string]any, error) {
	r.calls = append(r.calls, "records")
	r.recordQuery = q
	if r.recordsErr != nil {
		return nil, r.recordsErr
	}
	if r.records == nil {
		return []map[string]any{}, nil
	}
	return r.records, nil
}

func (r *recordingReads) GetRecord(_ context.Context, id int64) (map[string]any, error) {
	r.calls = append(r.calls, "record")
	r.recordID = id
	if r.recordErr != nil {
		return nil, r.recordErr
	}
	if r.record == nil {
		return nil, db.ErrNotFound
	}
	return r.record, nil
}

func (r *recordingReads) ListEvents(_ context.Context, id, after, limit int64) ([]map[string]any, error) {
	r.calls = append(r.calls, "events")
	r.eventsID = id
	r.eventsAfter = after
	r.eventsLimit = limit
	if r.eventsErr != nil {
		return nil, r.eventsErr
	}
	if r.events == nil {
		return []map[string]any{}, nil
	}
	return r.events, nil
}

func (r *recordingReads) ChatTargets(context.Context) ([]map[string]any, error) {
	r.calls = append(r.calls, "targets")
	if r.chatTargetsErr != nil {
		return nil, r.chatTargetsErr
	}
	if r.chatTargets == nil {
		return []map[string]any{}, nil
	}
	return r.chatTargets, nil
}

func (r *recordingReads) ListChatSessions(context.Context) ([]map[string]any, error) {
	r.calls = append(r.calls, "sessions")
	if r.sessionsErr != nil {
		return nil, r.sessionsErr
	}
	if r.sessions == nil {
		return []map[string]any{}, nil
	}
	return r.sessions, nil
}

func (r *recordingReads) ListChatMessages(_ context.Context, sessionID string) ([]map[string]any, error) {
	r.calls = append(r.calls, "messages")
	r.messageSession = sessionID
	if r.messagesErr != nil {
		return nil, r.messagesErr
	}
	if r.messages == nil {
		return []map[string]any{}, nil
	}
	return r.messages, nil
}

func (r *recordingReads) ListActivity(_ context.Context, taskID string, since, limit int64) ([]map[string]any, error) {
	r.calls = append(r.calls, "activity")
	r.activityTask = taskID
	r.activitySince = since
	r.activityLimit = limit
	if r.activityErr != nil {
		return nil, r.activityErr
	}
	if r.activity == nil {
		return []map[string]any{}, nil
	}
	return r.activity, nil
}

func (r *recordingReads) ActivityDetail(_ context.Context, seq int64) (map[string]any, error) {
	r.calls = append(r.calls, "activity-detail")
	r.activitySeq = seq
	if r.activityRowErr != nil {
		return nil, r.activityRowErr
	}
	return r.activityRow, nil
}

func (r *recordingReads) ListRoleConfigs(context.Context) ([]map[string]any, error) {
	r.calls = append(r.calls, "roles")
	if r.roleConfigsErr != nil {
		return nil, r.roleConfigsErr
	}
	if r.roleConfigs == nil {
		return []map[string]any{}, nil
	}
	return r.roleConfigs, nil
}

func (r *recordingReads) GetRoleConfig(_ context.Context, role string) (map[string]any, error) {
	r.calls = append(r.calls, "role")
	r.roleName = role
	if r.roleConfigErr != nil {
		return nil, r.roleConfigErr
	}
	return r.roleConfig, nil
}

func (r *recordingReads) ListChannelConfigs(context.Context) ([]map[string]any, error) {
	r.calls = append(r.calls, "channels")
	if r.channelConfigsErr != nil {
		return nil, r.channelConfigsErr
	}
	if r.channelConfigs == nil {
		return []map[string]any{}, nil
	}
	return r.channelConfigs, nil
}

func (r *recordingReads) GetChannelConfig(_ context.Context, id string) (map[string]any, error) {
	r.calls = append(r.calls, "channel")
	r.channelID = id
	if r.channelConfigErr != nil {
		return nil, r.channelConfigErr
	}
	return r.channelConfig, nil
}

func (r *recordingReads) GateSettings(context.Context) (map[string]any, error) {
	r.calls = append(r.calls, "gate")
	if r.gateErr != nil {
		return nil, r.gateErr
	}
	if r.gate == nil {
		return map[string]any{}, nil
	}
	return r.gate, nil
}

func (r *recordingReads) ProjectExists(_ context.Context, id string) (bool, error) {
	r.calls = append(r.calls, "project-exists")
	r.projectIDSeen = id
	if r.projectExistsErr != nil {
		return false, r.projectExistsErr
	}
	return r.projectOK, nil
}

func (r *recordingReads) RoleTemplates(_ context.Context, id string) (map[string]any, error) {
	r.calls = append(r.calls, "templates")
	r.projectIDSeen = id
	if r.templatesErr != nil {
		return nil, r.templatesErr
	}
	if r.templates == nil {
		return map[string]any{}, nil
	}
	return r.templates, nil
}

func (r *recordingReads) AllowedProfiles(_ context.Context, id string) (map[string]any, error) {
	r.calls = append(r.calls, "allowed-profiles")
	r.projectIDSeen = id
	if r.allowedProfilesErr != nil {
		return nil, r.allowedProfilesErr
	}
	if r.allowedProfiles == nil {
		return map[string]any{}, nil
	}
	return r.allowedProfiles, nil
}

func (r *recordingReads) ProjectRoleStats(_ context.Context, id string, since *time.Time) (map[string]any, error) {
	r.calls = append(r.calls, "role-stats")
	r.roleStatsProject = id
	if since != nil {
		copied := *since
		r.roleStatsSince = &copied
	}
	if r.roleStatsErr != nil {
		return nil, r.roleStatsErr
	}
	if r.roleStatsBody == nil {
		return map[string]any{}, nil
	}
	return r.roleStatsBody, nil
}

func (r *recordingReads) GateMessages(_ context.Context, taskID, label string) (map[string]any, error) {
	r.calls = append(r.calls, "gate-messages")
	r.gateTask = taskID
	r.gateLabel = label
	if r.gateMessagesErr != nil {
		return nil, r.gateMessagesErr
	}
	if r.gateMessages == nil {
		return map[string]any{}, nil
	}
	return r.gateMessages, nil
}

func (r *recordingReads) TaskRoom(_ context.Context, taskID string, q db.RoomQuery) (map[string]any, error) {
	r.calls = append(r.calls, "task-room")
	r.roomTask = taskID
	r.roomQuery = q
	if r.roomErr != nil {
		return nil, r.roomErr
	}
	if r.roomBody == nil {
		return map[string]any{}, nil
	}
	return r.roomBody, nil
}

func (r *recordingReads) ListConversation(_ context.Context, taskID string) ([]map[string]any, error) {
	r.calls = append(r.calls, "conversation")
	r.conversationTask = taskID
	if r.conversationErr != nil {
		return nil, r.conversationErr
	}
	if r.conversation == nil {
		return []map[string]any{}, nil
	}
	return r.conversation, nil
}

func (r *recordingReads) ProjectWorkspace(_ context.Context, id string) (db.ProjectWorkspace, error) {
	r.calls = append(r.calls, "project-workspace")
	r.projectIDSeen = id
	if r.workspaceErr != nil {
		return db.ProjectWorkspace{}, r.workspaceErr
	}
	return r.workspaceProject, nil
}

func (r *recordingReads) ActiveWorkspaces(context.Context) ([]db.ActiveWorkspace, error) {
	r.calls = append(r.calls, "active-workspaces")
	if r.activeErr != nil {
		return nil, r.activeErr
	}
	if r.active == nil {
		return []db.ActiveWorkspace{}, nil
	}
	return r.active, nil
}

func (r *recordingReads) ListProjectSchedules(_ context.Context, id string) (map[string]any, error) {
	r.calls = append(r.calls, "project-schedules")
	r.projectIDSeen = id
	if r.scheduleErr != nil {
		return nil, r.scheduleErr
	}
	return map[string]any{"schedules": []any{}, "limits": map[string]any{}}, nil
}

func (r *recordingReads) ListSchedules(context.Context) (map[string]any, error) {
	r.calls = append(r.calls, "schedules")
	if r.scheduleErr != nil {
		return nil, r.scheduleErr
	}
	return map[string]any{"schedules": []any{}}, nil
}

func (r *recordingReads) GetSchedule(_ context.Context, id string) (map[string]any, error) {
	r.calls = append(r.calls, "schedule")
	r.projectIDSeen = id
	if r.scheduleErr != nil {
		return nil, r.scheduleErr
	}
	return map[string]any{"schedule": map[string]any{}}, nil
}

func (r *recordingReads) ListScheduleRuns(_ context.Context, id string, q db.ScheduleRunsQuery) (map[string]any, error) {
	r.calls = append(r.calls, "schedule-runs")
	r.projectIDSeen = id
	r.scheduleQuery = q
	if r.scheduleErr != nil {
		return nil, r.scheduleErr
	}
	return map[string]any{
		"runs":        []any{},
		"next_before": nil,
		"routine_skips": map[string]any{
			"skipped_gate":    0,
			"skipped_overlap": 0,
		},
	}, nil
}

func (r *recordingReads) ListScheduleNotifications(_ context.Context, id string, limit int) (map[string]any, error) {
	r.calls = append(r.calls, "schedule-notifications")
	r.projectIDSeen = id
	r.scheduleLimit = limit
	if r.scheduleErr != nil {
		return nil, r.scheduleErr
	}
	return map[string]any{"notifications": []any{}}, nil
}

func TestUnroutedAPI(t *testing.T) {
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
		{http.MethodPost, "/api/tasks", http.StatusNotFound, `{"error": "not found"}`},
		{http.MethodGet, "/api/tasks/create", http.StatusNotFound, `{"error": "not found"}`},
		{http.MethodPut, "/api/llm-records", http.StatusNotFound, `{"error": "not found"}`},
		{http.MethodPost, "/api/agents/worker-1/usage", http.StatusNotFound, `{"error": "not found"}`},
		{http.MethodGet, "/api/llm-records/nope", http.StatusNotFound, `{"error": "not found"}`},
		{http.MethodGet, "/api/llm-records/12/nope", http.StatusNotFound, `{"error": "not found"}`},
		{http.MethodGet, "/api/agents/bad.id/usage", http.StatusNotFound, `{"error": "not found"}`},
		// Python's urlparse does not percent-decode. ServeMux unescapes the
		// segment and would otherwise serve GET /api/tasks.
		{http.MethodGet, "/api/%74asks", http.StatusNotFound, `{"error": "not found"}`},
		{http.MethodGet, "/api/%6fverview", http.StatusNotFound, `{"error": "not found"}`},
		{http.MethodGet, "/api/%73tatus", http.StatusNotFound, `{"error": "not found"}`},
	}
	for _, tt := range tests {
		t.Run(tt.method+" "+tt.path, func(t *testing.T) {
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(tt.method, tt.path))
			if rr.Code != tt.want || rr.Body.String() != tt.body {
				t.Fatalf("status %d body %s, want %d %s", rr.Code, rr.Body.String(), tt.want, tt.body)
			}
			if ct := rr.Header().Get("Content-Type"); ct != "application/json; charset=utf-8" {
				t.Fatalf("content type %q", ct)
			}
		})
	}
	if len(reads.calls) != 0 {
		t.Fatalf("unrouted request read the database: %v", reads.calls)
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
	const wantStatus = `{"status": "running", "agents": {}, "tasks": {"total": 5, "running": 2, "completed": 1}, "channels": {"web": {"channel": "web", "healthy": false, "active_sse_streams": 0}}, "checks": []}`
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
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` {
			t.Fatalf("%s post %d body %s", path, rr.Code, rr.Body.String())
		}
	}
}

func TestReadErrorsDoNotLeak(t *testing.T) {
	reads := &recordingReads{
		overviewErr: errors.New("dial SECRET"),
		projectsErr: errors.New("dial SECRET"),
		tasksErr:    errors.New("dial SECRET"),
	}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	h := srv.Handler()
	for _, path := range []string{"/api/overview", "/api/projects", "/api/tasks"} {
		t.Run(path, func(t *testing.T) {
			logs := captureLogs(t)
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
			if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "database unavailable"}` {
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
	for _, path := range []string{
		"/api/overview", "/api/projects", "/api/tasks",
		"/api/agents/worker-1/usage", "/api/llm-records/12", "/api/llm-records/12/events",
	} {
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
		if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "database unavailable"}` {
			t.Fatalf("%s %d body %s", path, rr.Code, rr.Body.String())
		}
	}
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"records":[]}` {
		t.Fatalf("list without a database %d body %s", rr.Code, rr.Body.String())
	}
}
