package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
)

func TestLargeRoleVersionStaysInHTTPBodies(t *testing.T) {
	version := strings.Repeat("9", 40)
	meta, err := json.Marshal(map[string]any{
		"prompt_layers": map[string]any{
			"task_role": map[string]any{"id": "backend", "kind": "work", "version": json.Number(version)},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	dsn := withHTTPDisposableDB(t, "loopgo_g1efix_rolehttp")
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	if err := db.EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	sqlDB, err := db.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO projects (id, name) VALUES ('proj-1', 'P')`); err != nil {
		t.Fatal(err)
	}
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO tasks (id, requirement, project_id) VALUES ('task-rolever1', 'ship it', 'proj-1')`); err != nil {
		t.Fatal(err)
	}
	var recordID int64
	if err := sqlDB.QueryRowContext(ctx, `INSERT INTO llm_records (agent_id, task_id, metadata) VALUES ('worker-1', 'task-rolever1', $1::jsonb) RETURNING id`, string(meta)).Scan(&recordID); err != nil {
		t.Fatal(err)
	}

	srv := New("dev", db.Probe{DB: sqlDB}, "")
	srv.Reads = db.NewStore(sqlDB)
	h := srv.Handler()

	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rolever1"))
	if rr.Code != http.StatusOK {
		t.Fatalf("task %d %s", rr.Code, rr.Body.String())
	}
	assertBareRoleVersion(t, rr.Body.String(), "run_roles", "worker-1", version)

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records?task_id=task-rolever1"))
	if rr.Code != http.StatusOK {
		t.Fatalf("list %d %s", rr.Code, rr.Body.String())
	}
	var listed struct {
		Records []map[string]any `json:"records"`
	}
	decodeUseNumber(t, rr.Body.String(), &listed)
	if len(listed.Records) != 1 {
		t.Fatalf("records %#v", listed.Records)
	}
	assertRoleVersionMap(t, listed.Records[0]["task_role"], version)

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/llm-records/"+strconv.FormatInt(recordID, 10)))
	if rr.Code != http.StatusOK {
		t.Fatalf("record %d %s", rr.Code, rr.Body.String())
	}
	var one struct {
		Record map[string]any `json:"record"`
	}
	decodeUseNumber(t, rr.Body.String(), &one)
	assertRoleVersionMap(t, one.Record["task_role"], version)
}

func TestGateSettingsTruthyConfigIs500(t *testing.T) {
	dsn := withHTTPDisposableDB(t, "loopgo_g1efix_gate")
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	if err := db.EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	sqlDB, err := db.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO channel_configs (channel_id, channel_type, config) VALUES ('tg', 'telegram', '"not-a-map"'::jsonb)`); err != nil {
		t.Fatal(err)
	}
	srv := New("dev", db.Probe{DB: sqlDB}, "")
	srv.Reads = db.NewStore(sqlDB)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/gate-settings"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"internal server error"}` {
		t.Fatalf("gate settings %d %s", rr.Code, rr.Body.String())
	}
}

func assertBareRoleVersion(t *testing.T, raw, field, agent, version string) {
	t.Helper()
	var body map[string]any
	decodeUseNumber(t, raw, &body)
	roles, _ := body[field].(map[string]any)
	assertRoleVersionMap(t, roles[agent], version)
	if strings.Contains(raw, `"version":"`+version+`"`) {
		t.Fatalf("version quoted in %s", raw)
	}
}

func assertRoleVersionMap(t *testing.T, raw any, version string) {
	t.Helper()
	role, _ := raw.(map[string]any)
	number, ok := role["version"].(json.Number)
	if role["id"] != "backend" || role["kind"] != "work" || !ok || string(number) != version {
		t.Fatalf("role %#v", raw)
	}
}

func decodeUseNumber(t *testing.T, raw string, dest any) {
	t.Helper()
	dec := json.NewDecoder(strings.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(dest); err != nil {
		t.Fatalf("decode %s: %v", raw, err)
	}
}
