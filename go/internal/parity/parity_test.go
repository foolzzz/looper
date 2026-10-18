package parity

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
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
//   - A string matching an ISO-8601 datetime, YYYY-MM-DDTHH:MM:SS with an optional
//     fraction and a Z or ±HH:MM suffix, becomes "<time>".
//   - The value of a key named uptime, pid, pids, or version becomes "<volatile>"
//     and is not walked. implementation and db are compared as written.
//   - Object key order is not significant. Numbers compare by value, so 0 and 0.0
//     match. Comparison remarshals both documents with encoding/json, which sorts
//     object keys and writes a whole number as itself.
//
// The overview clock is local noon on 2026-06-15, the same day make_goldens.py
// freezes. Seed rows at 2026-06-15T12:00:00Z count as today. Rows in 2020 do not.
var timeValue = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$`)

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
	for _, name := range []string{"status", "projects", "overview"} {
		t.Run(name, func(t *testing.T) {
			resp, err := client.Get(srv.URL + "/api/" + name)
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			body, err := io.ReadAll(resp.Body)
			if err != nil {
				t.Fatal(err)
			}
			if resp.StatusCode != http.StatusOK {
				t.Fatalf("status %d body %s", resp.StatusCode, body)
			}
			got := canonical(t, body)
			golden, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", name+".json"))
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
			if key == "uptime" || key == "pid" || key == "pids" || key == "version" {
				out[key] = "<volatile>"
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
	case string:
		if timeValue.MatchString(typed) {
			return "<time>"
		}
		return typed
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
