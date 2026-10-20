package parity

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/httpapi"
	"github.com/foolzzz/looper/go/internal/registry"
)

// Normalization, the same rule make_goldens.py applies before writing:
//
//   - Walk every JSON value.
//   - The value of a key in volatileTimes (since) becomes "<time>" and is not
//     walked. Those are generated when the request runs. Seeded timestamps
//     stay as written. volatileTimes is _VOLATILE_TIMES in make_goldens.py.
//   - The value of a key named uptime, pid, pids, or default_timezone becomes
//     "<volatile>" and is not walked. default_timezone is this machine's IANA
//     zone. A string version (a build id) becomes "<volatile>" too. A
//     numeric version, such as a requirement or role version, is compared.
//     implementation and db are compared as written.
//   - Object key order is not significant. Numbers compare by exact value, so 0
//     and 0.0 match, and two integers above 2^53 stay distinct. Comparison
//     remarshals both documents with encoding/json, which sorts object keys.
//
// When raw/<name>.http exists, the same response is also compared as bytes.
// That compare does not sort keys, re-encode numbers, or drop fields. See raw.go.
//
// The overview clock and the schedule clock are local noon on 2026-06-15, the
// same day make_goldens.py freezes. Seed rows at 2026-06-15T12:00:00Z count as
// today. Rows in 2020 do not. A schedule run at 2026-06-14T09:00:00Z is before
// that local midnight everywhere from UTC-12 through UTC+14.
//
// goldenCases is the legacy list make_goldens.py writes. Knowledge and skill
// cases have an isolated fixture in TestKnowledgeGoldenParity.
var (
	volatileTimes = map[string]bool{"since": true}
	volatileKeys  = map[string]bool{
		"uptime": true, "pid": true, "pids": true, "default_timezone": true,
	}
	goldenCases = []struct {
		name   string
		path   string
		status int
	}{
		{"status", "/api/status", http.StatusOK},
		{"projects", "/api/projects", http.StatusOK},
		{"overview", "/api/overview", http.StatusOK},
		{"tasks", "/api/tasks", http.StatusOK},
		{"tasks-group-needs_you", "/api/tasks?group=needs_you", http.StatusOK},
		{"tasks-group-in_progress", "/api/tasks?group=in_progress", http.StatusOK},
		{"tasks-group-ready", "/api/tasks?group=ready", http.StatusOK},
		{"tasks-group-attention", "/api/tasks?group=attention", http.StatusOK},
		{"tasks-group-completed", "/api/tasks?group=completed", http.StatusOK},
		{"tasks-group-cancelled", "/api/tasks?group=cancelled", http.StatusOK},
		{"tasks-group-other", "/api/tasks?group=other", http.StatusOK},
		{"tasks-project", "/api/tasks?project_id=proj-beta00002", http.StatusOK},
		{"tasks-limit", "/api/tasks?limit=2", http.StatusOK},
		{"tasks-bad-group", "/api/tasks?group=nope", http.StatusBadRequest},
		{"tasks-bad-limit", "/api/tasks?limit=0", http.StatusBadRequest},
		{"tasks-before", "/api/tasks?limit=2&before=task-asame0001", http.StatusOK},
		{"tasks-status", "/api/tasks?status=running", http.StatusOK},
		{"tasks-schedule", "/api/tasks?schedule_id=sched-nightly01", http.StatusOK},
		{"tasks-combined", "/api/tasks?project_id=proj-alpha0001&status=waiting_user&schedule_id=sched-nightly01", http.StatusOK},
		{"task-rich", "/api/tasks/task-rich00001", http.StatusOK},
		{"task-min", "/api/tasks/task-min000001", http.StatusOK},
		{"task-missing", "/api/tasks/task-missing01", http.StatusNotFound},
		{"usage", "/api/agents/usage-agent01/usage", http.StatusOK},
		{"usage-turns", "/api/agents/usage-agent01/usage?turns=1", http.StatusOK},
		{"usage-turns-zero", "/api/agents/usage-agent01/usage?turns=0", http.StatusOK},
		{"usage-bad-turns", "/api/agents/usage-agent01/usage?turns=many", http.StatusBadRequest},
		{"usage-bad-turns-1c", "/api/agents/usage-agent01/usage?turns=%1C2", http.StatusBadRequest},
		{"llm-records", "/api/llm-records", http.StatusOK},
		{"llm-records-agent", "/api/llm-records?agent_id=usage-agent01", http.StatusOK},
		{"llm-records-task", "/api/llm-records?task_id=task-rich00001", http.StatusOK},
		{"llm-records-role", "/api/llm-records?role=manager", http.StatusOK},
		{"llm-records-page", "/api/llm-records?limit=1&offset=1", http.StatusOK},
		{"llm-records-unknown-role", "/api/llm-records?role=nope", http.StatusOK},
		{"llm-records-limit-zero", "/api/llm-records?limit=0", http.StatusOK},
		{"llm-records-neg-offset", "/api/llm-records?offset=-1", http.StatusOK},
		{"llm-records-bad-limit", "/api/llm-records?limit=many", http.StatusInternalServerError},
		{"llm-records-bad-offset", "/api/llm-records?offset=1.5", http.StatusInternalServerError},
		{"llm-records-bad-limit-1c", "/api/llm-records?limit=%1C2", http.StatusInternalServerError},
		{"llm-record", "/api/llm-records/91001", http.StatusOK},
		{"llm-record-missing", "/api/llm-records/91009", http.StatusNotFound},
		{"llm-record-events", "/api/llm-records/91001/events", http.StatusOK},
		{"llm-record-events-after", "/api/llm-records/91001/events?after=1", http.StatusOK},
		{"llm-record-events-bad", "/api/llm-records/91001/events?after=nope", http.StatusBadRequest},
		{"llm-record-events-missing", "/api/llm-records/91009/events", http.StatusOK},
		{"llm-record-events-neg-limit", "/api/llm-records/91001/events?limit=-1", http.StatusInternalServerError},
		{"chat-targets", "/api/chat/targets", http.StatusOK},
		{"chat-sessions", "/api/chat/sessions", http.StatusOK},
		{"chat-messages", "/api/chat/messages?session_id=sess-lark-1", http.StatusOK},
		{"chat-messages-web", "/api/chat/messages?session_id=sess-web-1", http.StatusOK},
		{"chat-messages-missing", "/api/chat/messages?session_id=sess-missing", http.StatusOK},
		{"chat-messages-required", "/api/chat/messages", http.StatusBadRequest},
		{"chat-messages-spaces", "/api/chat/messages?session_id=%20", http.StatusOK},
		{"activity", "/api/tasks/task-rich00001/activity", http.StatusOK},
		{"activity-since", "/api/tasks/task-rich00001/activity?since=1", http.StatusOK},
		{"activity-limit-zero", "/api/tasks/task-rich00001/activity?limit=0", http.StatusOK},
		{"activity-neg-limit", "/api/tasks/task-rich00001/activity?limit=-1", http.StatusOK},
		{"activity-bad-since", "/api/tasks/task-rich00001/activity?since=nope", http.StatusInternalServerError},
		{"activity-bad-limit", "/api/tasks/task-rich00001/activity?limit=nope", http.StatusInternalServerError},
		{"activity-detail", "/api/tasks/task-rich00001/activity/1", http.StatusOK},
		{"activity-detail-other", "/api/tasks/task-min000001/activity/1", http.StatusOK},
		{"activity-missing", "/api/tasks/task-rich00001/activity/99", http.StatusNotFound},
		{"conversation", "/api/tasks/task-rich00001/conversation", http.StatusOK},
		{"conversation-empty", "/api/tasks/task-min000001/conversation", http.StatusOK},
		{"roles", "/api/roles", http.StatusOK},
		{"role-planner", "/api/roles/planner", http.StatusOK},
		{"role-missing", "/api/roles/missing", http.StatusNotFound},
		{"channel-configs", "/api/channel-configs", http.StatusOK},
		{"channel-lark", "/api/channel-configs/lark", http.StatusOK},
		{"channel-telegram-empty", "/api/channel-configs/telegram-empty", http.StatusOK},
		{"channel-web", "/api/channel-configs/web", http.StatusOK},
		{"channel-missing", "/api/channel-configs/missing", http.StatusNotFound},
		{"gate-settings", "/api/gate-settings", http.StatusOK},
		{"role-templates-alpha", "/api/projects/proj-alpha0001/role-templates", http.StatusOK},
		{"role-templates-beta", "/api/projects/proj-beta00002/role-templates", http.StatusOK},
		{"role-templates-missing", "/api/projects/proj-missing01/role-templates", http.StatusNotFound},
		{"allowed-profiles-alpha", "/api/projects/proj-alpha0001/allowed-profiles", http.StatusOK},
		{"allowed-profiles-beta", "/api/projects/proj-beta00002/allowed-profiles", http.StatusOK},
		{"allowed-profiles-missing", "/api/projects/proj-missing01/allowed-profiles", http.StatusNotFound},
		{"role-stats-alpha", "/api/projects/proj-alpha0001/role-stats", http.StatusOK},
		{"role-stats-since", "/api/projects/proj-alpha0001/role-stats?since=2010-01-01", http.StatusOK},
		{"role-stats-future", "/api/projects/proj-alpha0001/role-stats?since=2099-01-01", http.StatusOK},
		{"role-stats-week", "/api/projects/proj-alpha0001/role-stats?since=2010-W01-1", http.StatusOK},
		{"role-stats-compact", "/api/projects/proj-alpha0001/role-stats?since=20100101", http.StatusOK},
		{"role-stats-beta", "/api/projects/proj-beta00002/role-stats", http.StatusOK},
		{"role-stats-missing", "/api/projects/proj-missing01/role-stats", http.StatusNotFound},
		{"role-stats-bad-since", "/api/projects/proj-alpha0001/role-stats?since=nope", http.StatusBadRequest},
		{"role-stats-bad-week", "/api/projects/proj-alpha0001/role-stats?since=2010-W53-1", http.StatusBadRequest},
		{"gate-messages", "/api/tasks/task-rich00001/gates/g1/messages", http.StatusOK},
		{"gate-messages-missing-gate", "/api/tasks/task-rich00001/gates/nope/messages", http.StatusNotFound},
		{"gate-messages-missing-task", "/api/tasks/task-missing01/gates/g1/messages", http.StatusNotFound},
		{"gate-messages-min", "/api/tasks/task-min000001/gates/g1/messages", http.StatusNotFound},
		{"room", "/api/tasks/task-rich00001/room", http.StatusOK},
		{"room-min", "/api/tasks/task-min000001/room", http.StatusOK},
		{"room-missing", "/api/tasks/task-missing01/room", http.StatusNotFound},
		{"room-kind", "/api/tasks/task-rich00001/room?kind=question", http.StatusOK},
		{"room-from", "/api/tasks/task-rich00001/room?from=backend", http.StatusOK},
		{"room-unanswered", "/api/tasks/task-rich00001/room?unanswered=1", http.StatusOK},
		{"room-limit", "/api/tasks/task-rich00001/room?limit=1", http.StatusOK},
		{"room-after", "/api/tasks/task-rich00001/room?after=1", http.StatusOK},
		{"room-bad-limit", "/api/tasks/task-rich00001/room?limit=nope", http.StatusBadRequest},
		{"room-limit-superscript", "/api/tasks/task-rich00001/room?limit=%C2%B2", http.StatusInternalServerError},
		{"room-limit-circled", "/api/tasks/task-rich00001/room?limit=%E2%91%A1", http.StatusInternalServerError},
		{"room-after-superscript", "/api/tasks/task-rich00001/room?after=%C2%B2", http.StatusInternalServerError},
		{"room-after-circled", "/api/tasks/task-rich00001/room?after=%E2%91%A1", http.StatusInternalServerError},
		{"workspace-missing-project", "/api/workspaces/check?project_id=proj-missing01", http.StatusNotFound},
		{"workspace-missing-mode", "/api/workspaces/check?project_id=proj-missing01&mode=nope", http.StatusNotFound},
		{"workspace-no-dir", "/api/workspaces/check", http.StatusBadRequest},
		{"workspace-blank-project", "/api/workspaces/check?project_id=proj-blank0001", http.StatusBadRequest},
		{"workspace-relative", "/api/workspaces/check?dir=relative", http.StatusBadRequest},
		{"workspace-relative-mode", "/api/workspaces/check?dir=rel&mode=nope", http.StatusBadRequest},
		{"workspace-bad-mode", "/api/workspaces/check?dir=/loopgo-parity-absent-workspace&mode=nope", http.StatusBadRequest},
		{"workspace-base-inplace", "/api/workspaces/check?dir=/loopgo-parity-absent-workspace&mode=in_place&base=main", http.StatusBadRequest},
		{"workspace-absent", "/api/workspaces/check?dir=/loopgo-parity-absent-workspace", http.StatusOK},
		{"workspace-absent-asked", "/api/workspaces/check?dir=/loopgo-parity-absent-workspace&mode=worktree", http.StatusOK},
		{"workspace-absent-base", "/api/workspaces/check?dir=/loopgo-parity-absent-workspace&mode=worktree&base=main", http.StatusOK},
		{"workspace-absent-inplace", "/api/workspaces/check?dir=/loopgo-parity-absent-workspace&mode=in_place", http.StatusOK},
		{"workspace-absent-project", "/api/workspaces/check?project_id=proj-alpha0001&dir=/loopgo-parity-absent-workspace", http.StatusOK},
		{"schedules-project", "/api/projects/proj-alpha0001/schedules", http.StatusOK},
		{"schedules-project-blank", "/api/projects/proj-blank0001/schedules", http.StatusOK},
		{"schedules-project-missing", "/api/projects/proj-missing01/schedules", http.StatusNotFound},
		{"schedules", "/api/schedules", http.StatusOK},
		{"schedule", "/api/schedules/sched-nightly01", http.StatusOK},
		{"schedule-missing", "/api/schedules/sched-missing01", http.StatusNotFound},
		{"schedule-runs", "/api/schedules/sched-nightly01/runs", http.StatusOK},
		{"schedule-runs-limit", "/api/schedules/sched-nightly01/runs?limit=1", http.StatusOK},
		{"schedule-runs-hide", "/api/schedules/sched-nightly01/runs?skips=hide", http.StatusOK},
		{"schedule-runs-before", "/api/schedules/sched-nightly01/runs?before=run-skipbudgtdy", http.StatusOK},
		{"schedule-runs-bad-limit", "/api/schedules/sched-nightly01/runs?limit=0", http.StatusBadRequest},
		{"schedule-runs-bad-skips", "/api/schedules/sched-nightly01/runs?skips=nope", http.StatusBadRequest},
		{"schedule-runs-bad-before", "/api/schedules/sched-nightly01/runs?before=run-nope", http.StatusBadRequest},
		{"schedule-runs-missing", "/api/schedules/sched-missing01/runs", http.StatusNotFound},
		{"schedule-runs-limit-digits", "/api/schedules/sched-nightly01/runs?limit=" + strings.Repeat("9", 4300), http.StatusBadRequest},
		{"schedule-runs-limit-superscript", "/api/schedules/sched-nightly01/runs?limit=%C2%B2", http.StatusInternalServerError},
		{"schedule-runs-limit-overflow", "/api/schedules/sched-nightly01/runs?limit=" + strings.Repeat("9", 4301), http.StatusInternalServerError},
		{"schedule-notifications", "/api/schedules/sched-nightly01/notifications", http.StatusOK},
		{"schedule-notifications-limit", "/api/schedules/sched-nightly01/notifications?limit=1", http.StatusOK},
		{"schedule-notifications-missing", "/api/schedules/sched-missing01/notifications", http.StatusNotFound},
		{"schedule-notifications-bad-limit", "/api/schedules/sched-nightly01/notifications?limit=0", http.StatusBadRequest},
		{"schedule-notifications-superscript", "/api/schedules/sched-nightly01/notifications?limit=%C2%B2", http.StatusInternalServerError},
		{"agent-settings", "/api/agent-settings", http.StatusOK},
		{"agent-settings-alpha", "/api/projects/proj-alpha0001/agent-settings", http.StatusOK},
		{"agent-settings-beta", "/api/projects/proj-beta00002/agent-settings", http.StatusOK},
		{"agent-settings-blank", "/api/projects/proj-blank0001/agent-settings", http.StatusOK},
		{"agent-settings-missing", "/api/projects/proj-missing01/agent-settings", http.StatusNotFound},
		{"registry", "/api/registry", http.StatusOK},
	}
)

func TestGoldenParity(t *testing.T) {
	dsn := os.Getenv("LOOPGO_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("LOOPGO_TEST_DATABASE_URL is not set")
	}
	if !acceptableDatabase(dsn) {
		t.Fatal("refusing database URL: want 127.0.0.1:54329 and a loopgo_ database name")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	if err := db.ExecScript(ctx, dsn, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"); err != nil {
		t.Fatalf("reset schema: %v", err)
	}
	if err := db.EnsureSchema(ctx, dsn); err != nil {
		t.Fatalf("ensure schema: %v", err)
	}
	seed, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", "seed.sql"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.ExecScript(ctx, dsn, string(seed)); err != nil {
		t.Fatalf("seed: %v", err)
	}

	sqlDB, err := db.Open(ctx, dsn)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { sqlDB.Close() })

	// A non-empty root. An empty value is falsy and would read ~/.claude.
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", t.TempDir())

	if err := registry.PrepareParityLogin(); err != nil {
		t.Fatalf("parity login: %v", err)
	}
	t.Cleanup(registry.CloseParityLogin)
	// The seed stores the golden placeholder. The binaries live in the unique
	// directory. The response is rewritten back to the placeholder before compare.
	updated, err := sqlDB.ExecContext(ctx, `
		UPDATE agent_runtimes
		SET executable = replace(executable, $1, $2)
		WHERE executable LIKE $1 || '/%'`,
		registry.ParityBinPlaceholder, registry.ParityBin())
	if err != nil {
		t.Fatalf("parity executables: %v", err)
	}
	n, err := updated.RowsAffected()
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("parity executables updated %d", n)
	}
	api := httpapi.New("parity-test", db.Probe{DB: sqlDB}, "")
	// Login probes and runtime paths use this environment, not the process
	// environment, so the suite does not stat ~/.codex or ~/.grok.
	api.ClaudeBin = registry.ParityClaude()
	api.LoginEnviron = registry.ParityEnviron
	store := db.NewStore(sqlDB)
	// One harness noon for overview spend and schedule spent_today.
	noon := func() time.Time {
		return time.Date(2026, 6, 15, 12, 0, 0, 0, time.Local)
	}
	store.Now = noon
	api.Reads = store
	api.BindHost = "127.0.0.1"
	api.Now = noon
	token := useOperatorToken(t, api)
	srv := httptest.NewServer(api.Handler())
	t.Cleanup(srv.Close)
	if u, err := url.Parse(srv.URL); err == nil {
		if port, conv := strconv.Atoi(u.Port()); conv == nil {
			api.Port = port
		}
	}

	client := srv.Client()
	client.Timeout = 30 * time.Second
	useBearer(client, token)
	for _, tc := range goldenCases {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := client.Get(srv.URL + tc.path)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			body, err := io.ReadAll(resp.Body)
			if err != nil {
				t.Fatal(err)
			}
			if resp.StatusCode != tc.status {
				t.Fatalf("status %d body %s, want %d", resp.StatusCode, body, tc.status)
			}
			got := canonical(t, registry.RewriteParityPaths(body))
			golden, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", tc.name+".json"))
			if err != nil {
				t.Fatal(err)
			}
			want := canonical(t, golden)
			if string(got) != string(want) {
				t.Fatalf("canonical JSON differs\n got %s\nwant %s", got, want)
			}
			compareCaseRaw(t, tc.name, resp, body, false)
		})
	}

	// Python's urlparse drops a semicolon path parameter, so this is the rich
	// task. A "." segment or a duplicate slash is not cleaned: Python returns
	// JSON 404, and ServeMux would otherwise redirect with 301.
	client.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		return http.ErrUseLastResponse
	}
	extra := []struct {
		name   string
		path   string
		status int
		golden string
	}{
		{"semicolon", "/api/tasks/task-rich00001;foo", http.StatusOK, "task-rich.json"},
		{"dot segment", "/api/./tasks/task-rich00001", http.StatusNotFound, ""},
		{"dot in id", "/api/tasks/./task-rich00001", http.StatusNotFound, ""},
		{"duplicate slash", "/api//tasks/task-rich00001", http.StatusNotFound, ""},
		{"parent segment", "/api/tasks/../tasks/task-rich00001", http.StatusNotFound, ""},
		{"escaped tasks", "/api/%74asks", http.StatusNotFound, ""},
	}
	for _, tc := range extra {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := client.Get(srv.URL + tc.path)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			body, err := io.ReadAll(resp.Body)
			if err != nil {
				t.Fatal(err)
			}
			if resp.StatusCode != tc.status || resp.Header.Get("Location") != "" {
				t.Fatalf("status %d location %q body %s, want %d", resp.StatusCode, resp.Header.Get("Location"), body, tc.status)
			}
			if tc.golden == "" {
				if string(body) != `{"error": "not found"}` {
					t.Fatalf("body %s", body)
				}
				return
			}
			got := canonical(t, registry.RewriteParityPaths(body))
			golden, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", tc.golden))
			if err != nil {
				t.Fatal(err)
			}
			want := canonical(t, golden)
			if string(got) != string(want) {
				t.Fatalf("canonical JSON differs\n got %s\nwant %s", got, want)
			}
		})
	}

	// These are not goldenCases. The 403 Host is set explicitly: the test
	// server's port is not 8767, and the golden uses the header value.
	live := []struct {
		name   string
		path   string
		host   string
		origin string
	}{
		{name: "error-not-found", path: "/api/no-such-route"},
		{name: "error-bad-host", path: "/api/status", host: "evil.example:8767"},
		{name: "error-cross-origin", path: "/api/status", host: "127.0.0.1:8767", origin: "https://attacker.example"},
	}
	for _, tc := range live {
		t.Run(tc.name, func(t *testing.T) {
			req, err := http.NewRequest(http.MethodGet, srv.URL+tc.path, nil)
			if err != nil {
				t.Fatal(err)
			}
			if tc.host != "" {
				req.Host = tc.host
			}
			if tc.origin != "" {
				req.Header.Set("Origin", tc.origin)
			}
			resp, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			body, err := io.ReadAll(resp.Body)
			if err != nil {
				t.Fatal(err)
			}
			compareCaseRaw(t, tc.name, resp, body, true)
		})
	}
}

var knowledgeCases = []struct {
	name, path string
	status     int
}{
	{"knowledge", "/api/projects/proj-knowledge01/knowledge", 200},
	{"knowledge-empty", "/api/projects/proj-knowledge02/knowledge", 200},
	{"knowledge-missing", "/api/projects/proj-missing01/knowledge", 404},
	{"document-text", "/api/projects/proj-knowledge01/knowledge/doc-text001", 200},
	{"document-upload", "/api/projects/proj-knowledge01/knowledge/doc-upload01", 200},
	{"document-unicode", "/api/projects/proj-knowledge01/knowledge/doc-unicode01", 200},
	{"document-live", "/api/projects/proj-knowledge01/knowledge/doc-live001", 200},
	{"document-file-missing", "/api/projects/proj-knowledge01/knowledge/doc-missing01", 200},
	{"document-binary", "/api/projects/proj-knowledge01/knowledge/doc-binary01", 200},
	{"document-invalid-utf8", "/api/projects/proj-knowledge01/knowledge/doc-invalid01", 200},
	{"document-large", "/api/projects/proj-knowledge01/knowledge/doc-large001", 200},
	{"document-symlink", "/api/projects/proj-knowledge01/knowledge/doc-symlink01", 200},
	{"document-parent", "/api/projects/proj-knowledge01/knowledge/doc-parent01", 200},
	{"document-directory", "/api/projects/proj-knowledge01/knowledge/doc-dir0001", 200},
	{"document-missing", "/api/projects/proj-knowledge01/knowledge/doc-absent01", 404},
	{"document-cross-project", "/api/projects/proj-knowledge02/knowledge/doc-text001", 404},
	{"document-project-missing", "/api/projects/proj-missing01/knowledge/doc-text001", 404},
	{"knowledge-drafts", "/api/projects/proj-knowledge01/knowledge/drafts", 200},
	{"knowledge-drafts-empty", "/api/projects/proj-knowledge02/knowledge/drafts", 200},
	{"knowledge-drafts-missing", "/api/projects/proj-missing01/knowledge/drafts", 404},
	{"project-skill", "/api/projects/proj-knowledge01/skills/skill-project001", 200},
	{"project-skill-file-missing", "/api/projects/proj-knowledge01/skills/skill-nofile001", 200},
	{"project-skill-raw-body", "/api/projects/proj-knowledge01/skills/skill-rawbody01", 200},
	{"project-skill-cross-project", "/api/projects/proj-knowledge02/skills/skill-project001", 404},
	{"project-skill-missing", "/api/projects/proj-knowledge01/skills/skill-absent01", 404},
	{"project-skill-other-origin", "/api/projects/proj-knowledge01/skills/skill-otherorigin01", 404},
	{"project-skill-project-missing", "/api/projects/proj-missing01/skills/skill-project001", 404},
}

// TestKnowledgeGoldenParity owns its project files and schema. It runs separately
// so fixture projects cannot change the pre-existing project/overview goldens.
func TestKnowledgeGoldenParity(t *testing.T) {
	dsn := os.Getenv("LOOPGO_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("LOOPGO_TEST_DATABASE_URL is not set")
	}
	if !acceptableDatabase(dsn) {
		t.Fatal("refusing database URL")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	if err := db.ExecScript(ctx, dsn, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"); err != nil {
		t.Fatal(err)
	}
	if err := db.EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"project", "project/subdir", "empty"} {
		if err := os.MkdirAll(filepath.Join(root, name), 0700); err != nil {
			t.Fatal(err)
		}
	}
	files := map[string][]byte{
		"project/live.md":     []byte("# Live\n最新内容 🌍\n"),
		"project/binary.dat":  []byte("text\x00binary"),
		"project/invalid.dat": []byte{0xff, 'i', 'n', 'v', 'a', 'l', 'i', 'd'},
		"project/large.txt":   bytes.Repeat([]byte("x"), 2*1024*1024+1),
		"outside.md":          []byte("OUTSIDE_SENTINEL"),
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(root, name), content, 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(root, "outside.md"), filepath.Join(root, "project", "escaped.md")); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"seed.sql", "knowledge_fixture.sql"} {
		sql, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", name))
		if err != nil {
			t.Fatal(err)
		}
		if err := db.ExecScript(ctx, dsn, strings.ReplaceAll(string(sql), "__KNOWLEDGE_ROOT__", strings.ReplaceAll(root, "'", "''"))); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
	}
	sqlDB, err := db.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	api := httpapi.New("parity-test", db.Probe{DB: sqlDB}, "")
	api.RuntimeRoot = root
	api.Reads = db.NewStore(sqlDB)
	token := useOperatorToken(t, api)
	// Disposable runtime checks use an explicit port above the operator range.
	var listener net.Listener
	for port := 18001; port < 18101; port++ {
		listener, err = net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(port))
		if err == nil {
			break
		}
	}
	if err != nil {
		t.Fatalf("listen on a disposable port: %v", err)
	}
	srv := &httptest.Server{Listener: listener, Config: &http.Server{Handler: api.Handler()}}
	srv.Start()
	t.Cleanup(srv.Close)
	client := srv.Client()
	client.Timeout = 30 * time.Second
	if u, err := url.Parse(srv.URL); err == nil {
		if port, conv := strconv.Atoi(u.Port()); conv == nil {
			api.Port = port
		}
	}
	useBearer(client, token)

	for _, tc := range knowledgeCases {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := client.Get(srv.URL + tc.path)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			body, err := io.ReadAll(resp.Body)
			if err != nil {
				t.Fatal(err)
			}
			if resp.StatusCode != tc.status {
				t.Fatalf("status %d body %s, want %d", resp.StatusCode, body, tc.status)
			}
			if bytes.Contains(body, []byte("OUTSIDE_SENTINEL")) {
				t.Fatal("response leaked a file outside the project")
			}
			var value map[string]any
			if err := json.Unmarshal(body, &value); err != nil {
				t.Fatal(err)
			}
			if index, ok := value["index"].(string); ok {
				if value["index_chars"] != float64(utf8.RuneCountInString(index)) {
					t.Fatalf("index_chars %v does not count real index", value["index_chars"])
				}
			}
			// Compare the index count after replacing the unique fixture path too.
			rewritten := bytes.ReplaceAll(body, []byte(root), []byte("<knowledge-root>"))
			if err := json.Unmarshal(rewritten, &value); err != nil {
				t.Fatal(err)
			}
			if index, ok := value["index"].(string); ok {
				value["index_chars"] = utf8.RuneCountInString(index)
			}
			rewritten, err = json.Marshal(value)
			if err != nil {
				t.Fatal(err)
			}
			golden, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", tc.name+".json"))
			if err != nil {
				t.Fatal(err)
			}
			got, want := canonical(t, rewritten), canonical(t, golden)
			if !bytes.Equal(got, want) {
				t.Fatalf("canonical JSON differs\n got %s\nwant %s", got, want)
			}
			compareCaseRaw(t, tc.name, resp, body, false)
		})
	}
}

func compareCaseRaw(t *testing.T, name string, resp *http.Response, body []byte, required bool) {
	t.Helper()
	dir := filepath.Join("..", "..", "testdata", "parity", "raw")
	golden, err := os.ReadFile(filepath.Join(dir, name+".http"))
	if err != nil {
		if os.IsNotExist(err) && !required {
			return
		}
		t.Fatal(err)
	}
	paths, err := readPointerFile(filepath.Join(dir, name+".paths"))
	if err != nil {
		t.Fatal(err)
	}
	if err := CompareRaw(resp, body, golden, paths); err != nil {
		t.Fatalf("raw: %v", err)
	}
}

func TestNormalizeKeepsSeededTimes(t *testing.T) {
	got := normalize(map[string]any{
		"created_at":   "2020-01-15T00:00:00+00:00",
		"updated_at":   "2020-01-16T00:00:00Z",
		"next_fire_at": "2020-04-01T09:00:00+00:00",
		"completed_at": "2020-03-04T12:00:00+00:00",
		"note":         "not a time",
		"uptime":       12,
		"version":      "build-id",
		"requirement_versions": []any{
			map[string]any{"version": float64(2), "text": "a"},
		},
		"spend_today": map[string]any{
			"since":          "2026-06-15T00:00:00+08:00",
			"equivalent_usd": 1.25,
		},
	}).(map[string]any)
	for _, key := range []string{"created_at", "updated_at", "next_fire_at", "completed_at"} {
		if _, ok := got[key].(string); !ok || got[key] == "<time>" {
			t.Fatalf("%s became %v", key, got[key])
		}
	}
	if got["created_at"] != "2020-01-15T00:00:00+00:00" || got["note"] != "not a time" || got["uptime"] != "<volatile>" || got["version"] != "<volatile>" {
		t.Fatalf("normalize changed a seeded value: %#v", got)
	}
	versions := got["requirement_versions"].([]any)
	row := versions[0].(map[string]any)
	if row["version"] != float64(2) || row["text"] != "a" {
		t.Fatalf("numeric version %#v", row)
	}
	spend := got["spend_today"].(map[string]any)
	if spend["since"] != "<time>" || spend["equivalent_usd"] != 1.25 {
		t.Fatalf("spend_today %#v", spend)
	}
}

func canonical(t *testing.T, raw []byte) []byte {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var value any
	if err := dec.Decode(&value); err != nil {
		t.Fatalf("json: %v body %s", err, raw)
	}
	out, err := json.Marshal(normalize(value))
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func normalize(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(typed))
		for key, item := range typed {
			if volatileKeys[key] {
				out[key] = "<volatile>"
				continue
			}
			// A build id is a string. Requirement and role versions are numbers.
			if key == "version" {
				if _, ok := item.(string); ok {
					out[key] = "<volatile>"
					continue
				}
			}
			if volatileTimes[key] {
				out[key] = "<time>"
				continue
			}
			out[key] = normalize(item)
		}
		return out
	case []any:
		out := make([]any, len(typed))
		for i, item := range typed {
			out[i] = normalize(item)
		}
		return out
	case json.Number:
		return canonicalNumber(typed)
	default:
		return value
	}
}

// canonicalNumber is the exact decimal of a JSON number. 0 and 0.0 and 1e2
// share one encoding. Integers above 2^53 do not collapse to the same float.
func canonicalNumber(n json.Number) json.Number {
	r, ok := parseJSONRat(string(n))
	if !ok {
		return n
	}
	return json.Number(formatRat(r))
}

func parseJSONRat(s string) (*big.Rat, bool) {
	if s == "" {
		return nil, false
	}
	base := s
	exp := 0
	if i := strings.IndexAny(s, "eE"); i >= 0 {
		base = s[:i]
		n, err := strconv.Atoi(s[i+1:])
		if err != nil {
			return nil, false
		}
		exp = n
	}
	r := new(big.Rat)
	if _, ok := r.SetString(base); !ok {
		return nil, false
	}
	if exp == 0 {
		return r, true
	}
	pow := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(absInt(exp))), nil)
	scale := new(big.Rat).SetInt(pow)
	if exp > 0 {
		r.Mul(r, scale)
	} else {
		r.Quo(r, scale)
	}
	return r, true
}

func absInt(n int) int {
	if n < 0 {
		return -n
	}
	return n
}

func formatRat(r *big.Rat) string {
	if r.Sign() == 0 {
		return "0"
	}
	neg := r.Sign() < 0
	abs := new(big.Rat).Abs(r)
	if abs.IsInt() {
		s := abs.Num().String()
		if neg {
			return "-" + s
		}
		return s
	}
	num := new(big.Int).Set(abs.Num())
	den := new(big.Int).Set(abs.Denom())
	intPart := new(big.Int).Quo(num, den)
	rem := new(big.Int).Rem(num, den)
	frac := make([]byte, 0, 8)
	for rem.Sign() != 0 && len(frac) < 10000 {
		rem.Mul(rem, big.NewInt(10))
		digit := new(big.Int).Quo(rem, den)
		rem.Rem(rem, den)
		frac = append(frac, byte('0'+digit.Int64()))
	}
	s := intPart.String() + "." + string(frac)
	if neg {
		return "-" + s
	}
	return s
}

func TestCanonicalNumbers(t *testing.T) {
	tests := []struct {
		name string
		a, b string
		same bool
	}{
		{name: "zero", a: `{"n":0}`, b: `{"n":0.0}`, same: true},
		{name: "exponent", a: `{"n":1e2}`, b: `{"n":100}`, same: true},
		{name: "fraction", a: `{"n":1.50}`, b: `{"n":1.5}`, same: true},
		{name: "above 2^53", a: `{"n":9007199254740993}`, b: `{"n":9007199254740992}`, same: false},
		{name: "forty digits", a: `{"n":` + strings.Repeat("9", 40) + `}`, b: `{"n":` + strings.Repeat("9", 39) + `}`, same: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := string(canonical(t, []byte(tt.a))) == string(canonical(t, []byte(tt.b)))
			if got != tt.same {
				t.Fatalf("same %v, want %v\n a %s\n b %s", got, tt.same, canonical(t, []byte(tt.a)), canonical(t, []byte(tt.b)))
			}
		})
	}
}

// acceptableDatabase reports whether every host pgx would dial, including TLS
// and multi-host fallbacks, is 127.0.0.1 or localhost on port 54329, and the
// database name starts with loopgo_. A name that merely contains loopgo_ is refused.
func acceptableDatabase(dsn string) bool {
	cfg, err := pgconn.ParseConfig(dsn)
	if err != nil || cfg == nil || !strings.HasPrefix(cfg.Database, "loopgo_") {
		return false
	}
	if !loopbackPort(cfg.Host, cfg.Port) {
		return false
	}
	for _, fallback := range cfg.Fallbacks {
		if fallback == nil || !loopbackPort(fallback.Host, fallback.Port) {
			return false
		}
	}
	return true
}

func loopbackPort(host string, port uint16) bool {
	return port == 54329 && (host == "127.0.0.1" || host == "localhost")
}

func TestAcceptableDatabase(t *testing.T) {
	const good = "postgresql://postgres:postgres@127.0.0.1:54329/loopgo_g1a"
	tests := []struct {
		name string
		dsn  string
		ok   bool
	}{
		{name: "loopback", dsn: good, ok: true},
		{name: "localhost", dsn: "postgresql://postgres:postgres@localhost:54329/loopgo_g1a", ok: true},
		{name: "keyword", dsn: "host=127.0.0.1 port=54329 dbname=loopgo_g1a user=postgres password=postgres", ok: true},
		{name: "both loopback hosts", dsn: "postgresql://postgres:postgres@127.0.0.1:54329,localhost:54329/loopgo_g1a", ok: true},
		{name: "sslmode disable", dsn: good + "?sslmode=disable", ok: true},
		{name: "substring name", dsn: "postgresql://postgres:postgres@127.0.0.1:54329/notloopgo_db", ok: false},
		{name: "prefix missing", dsn: "postgresql://postgres:postgres@127.0.0.1:54329/xloopgo_db", ok: false},
		{name: "other host", dsn: "postgresql://postgres:postgres@10.0.0.1:54329/loopgo_g1a", ok: false},
		{name: "other port", dsn: "postgresql://postgres:postgres@127.0.0.1:5432/loopgo_g1a", ok: false},
		{name: "fallback host", dsn: "postgresql://postgres:postgres@127.0.0.1:54329,evil.example:5432/loopgo_g1a", ok: false},
		{name: "fallback port", dsn: "postgresql://postgres:postgres@127.0.0.1:54329,127.0.0.1:5432/loopgo_g1a", ok: false},
		{name: "keyword fallback", dsn: "host=127.0.0.1,evil.example port=54329,5432 dbname=loopgo_g1a", ok: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := acceptableDatabase(tt.dsn); got != tt.ok {
				t.Fatalf("acceptable %v, want %v", got, tt.ok)
			}
		})
	}
}
