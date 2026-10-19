package parity

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/httpapi"
)

// Normalization, the same rule make_goldens.py applies before writing:
//
//   - Walk every JSON value.
//   - The value of a key in volatileTimes (since) becomes "<time>" and is not
//     walked. Those are generated when the request runs. Seeded timestamps
//     stay as written. volatileTimes is _VOLATILE_TIMES in make_goldens.py.
//   - The value of a key named uptime, pid, or pids becomes "<volatile>" and is
//     not walked. A string version (a build id) becomes "<volatile>" too. A
//     numeric version, such as a requirement or role version, is compared.
//     implementation and db are compared as written.
//   - Object key order is not significant. Numbers compare by value, so 0 and 0.0
//     match. Comparison remarshals both documents with encoding/json, which sorts
//     object keys and writes a whole number as itself.
//
// The overview clock is local noon on 2026-06-15, the same day make_goldens.py
// freezes. Seed rows at 2026-06-15T12:00:00Z count as today. Rows in 2020 do not.
//
// goldenCases is the same list make_goldens.py writes.
var (
	volatileTimes = map[string]bool{"since": true}
	volatileKeys  = map[string]bool{"uptime": true, "pid": true, "pids": true}
	goldenCases   = []struct {
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

	api := httpapi.New("parity-test", db.Probe{DB: sqlDB}, "")
	api.Reads = db.NewStore(sqlDB)
	api.BindHost = "127.0.0.1"
	api.Now = func() time.Time {
		return time.Date(2026, 6, 15, 12, 0, 0, 0, time.Local)
	}
	srv := httptest.NewServer(api.Handler())
	t.Cleanup(srv.Close)

	client := srv.Client()
	client.Timeout = 30 * time.Second
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
			got := canonical(t, body)
			golden, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", tc.name+".json"))
			if err != nil {
				t.Fatal(err)
			}
			want := canonical(t, golden)
			if string(got) != string(want) {
				t.Fatalf("canonical JSON differs\n got %s\nwant %s", got, want)
			}
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
				if string(body) != `{"error":"not found"}` {
					t.Fatalf("body %s", body)
				}
				return
			}
			got := canonical(t, body)
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
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
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
	default:
		return value
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
