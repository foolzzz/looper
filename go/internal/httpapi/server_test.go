package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/foolzzz/looper/go/internal/db"
)

func TestMain(m *testing.M) {
	slog.SetDefault(slog.New(slog.NewTextHandler(io.Discard, nil)))
	os.Exit(m.Run())
}

type fakeDB struct {
	err   error
	hasDL bool
	limit time.Duration
}

func (f *fakeDB) Ping(ctx context.Context) error {
	start := time.Now()
	deadline, ok := ctx.Deadline()
	f.hasDL = ok
	if ok {
		f.limit = deadline.Sub(start)
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return f.err
}

// loopbackRequest is httptest.NewRequest with a loopback Host.
// NewRequest uses example.com, which the Host guard refuses.
func loopbackRequest(method, target string) *http.Request {
	req := httptest.NewRequest(method, target, nil)
	req.Host = "127.0.0.1"
	return req
}

func assertProbeWindow(t *testing.T, limit time.Duration) {
	t.Helper()
	if limit < db.ProbeTimeout-200*time.Millisecond || limit > db.ProbeTimeout+50*time.Millisecond {
		t.Fatalf("database check deadline %s, want %s", limit, db.ProbeTimeout)
	}
}

func TestStatus(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "index.html"), []byte("INDEX"), 0o644); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name       string
		pinger     DBPinger
		webDir     string
		method     string
		path       string
		wantStatus int
		wantLog    string
		wantBody   string
		wantAllow  string
		leak       string
		deadline   bool
	}{
		{
			name:       "ok",
			pinger:     &fakeDB{},
			method:     http.MethodGet,
			path:       "/api/status",
			wantStatus: http.StatusOK,
			wantBody:   pythonStatusZero,
			deadline:   true,
		},
		{
			name:       "db error",
			pinger:     &fakeDB{err: errors.New("dial tcp: connection refused")},
			method:     http.MethodGet,
			path:       "/api/status",
			wantStatus: http.StatusOK,
			wantBody:   pythonStatusZero,
			wantLog:    "error: unreachable",
			leak:       "connection refused",
			deadline:   true,
		},
		{
			name:       "timeout",
			pinger:     &fakeDB{err: context.DeadlineExceeded},
			method:     http.MethodGet,
			path:       "/api/status",
			wantStatus: http.StatusOK,
			wantBody:   pythonStatusZero,
			wantLog:    "error: timeout",
			deadline:   true,
		},
		{
			name:       "nil database",
			pinger:     nil,
			method:     http.MethodGet,
			path:       "/api/status",
			wantStatus: http.StatusOK,
			wantBody:   pythonStatusZero,
			wantLog:    "error: no database",
		},
		{
			name:       "post is not the spa shell",
			pinger:     &fakeDB{},
			webDir:     root,
			method:     http.MethodPost,
			path:       "/api/status",
			wantStatus: http.StatusMethodNotAllowed,
			wantBody:   `{"error":"method not allowed"}`,
			wantAllow:  "GET, HEAD",
		},
		{
			name:       "put is not allowed",
			pinger:     &fakeDB{},
			webDir:     root,
			method:     http.MethodPut,
			path:       "/api/status",
			wantStatus: http.StatusMethodNotAllowed,
			wantBody:   `{"error":"method not allowed"}`,
			wantAllow:  "GET, HEAD",
		},
		{
			name:       "unknown api",
			pinger:     &fakeDB{},
			webDir:     root,
			method:     http.MethodGet,
			path:       "/api/nope",
			wantStatus: http.StatusNotFound,
			wantBody:   `{"error":"not found"}`,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var pinger *fakeDB
			if f, ok := tt.pinger.(*fakeDB); ok {
				pinger = f
			}
			logs := captureLogs(t)
			h := New("1.2.3", tt.pinger, tt.webDir).Handler()
			req := loopbackRequest(tt.method, tt.path)
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, req)
			if rr.Code != tt.wantStatus {
				t.Fatalf("status %d, want %d, body %s", rr.Code, tt.wantStatus, rr.Body.String())
			}
			if ct := rr.Header().Get("Content-Type"); ct != "application/json" {
				t.Fatalf("content type %q", ct)
			}
			if tt.wantAllow != "" && rr.Header().Get("Allow") != tt.wantAllow {
				t.Fatalf("Allow %q, want %q", rr.Header().Get("Allow"), tt.wantAllow)
			}
			if tt.wantBody != "" && rr.Body.String() != tt.wantBody {
				t.Fatalf("body %s, want %s", rr.Body.String(), tt.wantBody)
			}
			if tt.wantLog != "" && !strings.Contains(logs.String(), tt.wantLog) {
				t.Fatalf("log %q, want substring %q", logs.String(), tt.wantLog)
			}
			if rr.Code == http.StatusOK && tt.path == "/api/status" {
				var raw map[string]any
				if err := json.Unmarshal(rr.Body.Bytes(), &raw); err != nil {
					t.Fatal(err)
				}
				for _, key := range []string{"implementation", "version", "db"} {
					if _, ok := raw[key]; ok {
						t.Fatalf("status includes %s, which Python does not: %s", key, rr.Body.String())
					}
				}
				if len(raw) != 5 {
					t.Fatalf("keys %v", raw)
				}
			}
			if tt.deadline {
				if pinger == nil || !pinger.hasDL {
					t.Fatal("status check had no deadline")
				}
				assertProbeWindow(t, pinger.limit)
			}
			if tt.leak != "" && (strings.Contains(rr.Body.String(), tt.leak) || strings.Contains(logs.String(), tt.leak)) {
				t.Fatalf("leaked %q in body %s log %s", tt.leak, rr.Body.String(), logs.String())
			}
		})
	}
}

// pythonStatusZero is GET /api/status with no reader and no live agents.
const pythonStatusZero = `{"status":"running","agents":{},"tasks":{"total":0,"running":0,"completed":0},"channels":{"web":{"channel":"web","healthy":false,"active_sse_streams":0}},"checks":[]}`

func TestStatusHidesDSNSecrets(t *testing.T) {
	tests := []struct {
		name string
		dsn  string
	}{
		{name: "encoded query", dsn: "postgresql://u@127.0.0.1:1/%zz?%70assword=SECRET"},
		{name: "keyword quote", dsn: `password='alpha\'SECRET_ONE SECRET_TWO' host=127.0.0.1 port=1 sslmode=invalid`},
		{name: "userinfo", dsn: "postgresql://u:SECRET@127.0.0.1:1/db?sslmode=invalid"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if _, err := pgconn.ParseConfig(tt.dsn); err == nil {
				t.Fatal("dsn parsed; refusing to connect")
			}
			logs := captureLogs(t)
			sqlDB, err := db.Open(context.Background(), tt.dsn)
			if err != nil {
				if strings.Contains(err.Error(), "SECRET") || strings.Contains(logs.String(), "SECRET") {
					t.Fatal("open leaked SECRET")
				}
				t.Fatal("open failed")
			}
			t.Cleanup(func() { sqlDB.Close() })

			h := New("dev", db.Probe{DB: sqlDB}, "").Handler()
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/status"))

			body := rr.Body.String()
			if rr.Code != http.StatusOK {
				t.Fatalf("status %d", rr.Code)
			}
			if body != pythonStatusZero {
				t.Fatalf("body %s", body)
			}
			if strings.Contains(body, "SECRET") || strings.Contains(logs.String(), "SECRET") {
				t.Fatal("status or log leaked SECRET")
			}
			if !strings.Contains(logs.String(), "error: invalid database URL") {
				t.Fatal("log missing the fixed database message")
			}
		})
	}
}

func captureLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

func TestStatusPropagatesRequestCancel(t *testing.T) {
	logs := captureLogs(t)
	started := make(chan struct{})
	errCh := make(chan error, 1)
	pinger := &blockingDB{started: started, errCh: errCh}
	h := New("1.2.3", pinger, "").Handler()
	ctx, cancel := context.WithCancel(context.Background())
	req := loopbackRequest(http.MethodGet, "/api/status").WithContext(ctx)

	done := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, req)
		done <- rr
	}()

	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("database check did not start")
	}
	assertProbeWindow(t, pinger.limit)
	cancel()

	select {
	case err := <-errCh:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("ping error %v, want context canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("request cancellation did not reach the database check")
	}

	select {
	case rr := <-done:
		if rr.Code != http.StatusOK {
			t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
		}
		if rr.Body.String() != pythonStatusZero {
			t.Fatalf("body %s", rr.Body.String())
		}
		if !strings.Contains(logs.String(), "error: context canceled") {
			t.Fatalf("log %s", logs.String())
		}
	case <-time.After(time.Second):
		t.Fatal("handler did not return after cancellation")
	}
}

type blockingDB struct {
	started chan struct{}
	errCh   chan error
	limit   time.Duration
	once    sync.Once
}

func (b *blockingDB) Ping(ctx context.Context) error {
	start := time.Now()
	if deadline, ok := ctx.Deadline(); ok {
		b.limit = deadline.Sub(start)
	}
	b.once.Do(func() { close(b.started) })
	<-ctx.Done()
	err := ctx.Err()
	b.errCh <- err
	return err
}

func TestStaticAndSPA(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	secretPath := filepath.Join(outside, "secret.txt")
	if err := os.WriteFile(secretPath, []byte("SECRET"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "index.html"), []byte("INDEX"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(root, "assets"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "assets", "app.js"), []byte("JS"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(secretPath, filepath.Join(root, "leak.txt")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join("assets", "app.js"), filepath.Join(root, "alias.js")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	relOutside, err := filepath.Rel(root, outside)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(relOutside, filepath.Join(root, "rellink")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("assets", filepath.Join(root, "inside")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "plain"), []byte("PLAIN"), 0o644); err != nil {
		t.Fatal(err)
	}

	empty := t.TempDir() // no index.html

	h := New("dev", &fakeDB{}, root).Handler()
	emptyH := New("dev", &fakeDB{}, empty).Handler()
	noWeb := New("dev", &fakeDB{}, "").Handler()

	tests := []struct {
		name       string
		h          http.Handler
		method     string
		path       string
		wantStatus int
		wantBody   string
		emptyBody  bool
		wantType   string
		jsonErr    string
		wantAllow  string
	}{
		{name: "root", h: h, method: http.MethodGet, path: "/", wantStatus: http.StatusOK, wantBody: "INDEX", wantType: "text/html"},
		{name: "index file", h: h, method: http.MethodGet, path: "/index.html", wantStatus: http.StatusOK, wantBody: "INDEX", wantType: "text/html"},
		{name: "asset", h: h, method: http.MethodGet, path: "/assets/app.js", wantStatus: http.StatusOK, wantBody: "JS", wantType: "javascript"},
		{name: "alias symlink inside", h: h, method: http.MethodGet, path: "/alias.js", wantStatus: http.StatusOK, wantBody: "JS", wantType: "javascript"},
		{name: "spa fallback", h: h, method: http.MethodGet, path: "/tasks/123", wantStatus: http.StatusOK, wantBody: "INDEX", wantType: "text/html"},
		{name: "directory is not a listing", h: h, method: http.MethodGet, path: "/assets", wantStatus: http.StatusOK, wantBody: "INDEX", wantType: "text/html"},
		{name: "head root", h: h, method: http.MethodHead, path: "/", wantStatus: http.StatusOK, emptyBody: true, wantType: "text/html"},
		{name: "symlink escape", h: h, method: http.MethodGet, path: "/leak.txt", wantStatus: http.StatusNotFound, jsonErr: `{"error":"not found"}`},
		{name: "directory symlink escape", h: h, method: http.MethodGet, path: "/link/secret.txt", wantStatus: http.StatusNotFound, jsonErr: `{"error":"not found"}`},
		{name: "directory symlink missing child", h: h, method: http.MethodGet, path: "/link/no-such.txt", wantStatus: http.StatusNotFound, jsonErr: `{"error":"not found"}`},
		{name: "relative directory symlink escape", h: h, method: http.MethodGet, path: "/rellink/secret.txt", wantStatus: http.StatusNotFound, jsonErr: `{"error":"not found"}`},
		{name: "symlink directory itself", h: h, method: http.MethodGet, path: "/link", wantStatus: http.StatusNotFound, jsonErr: `{"error":"not found"}`},
		{name: "directory symlink inside", h: h, method: http.MethodGet, path: "/inside/app.js", wantStatus: http.StatusOK, wantBody: "JS", wantType: "javascript"},
		{name: "dotdot is the cleaned static file", h: h, method: http.MethodGet, path: "/../secret.txt", wantStatus: http.StatusOK, wantBody: "INDEX", wantType: "text/html"},
		{name: "dot segment is the same file", h: h, method: http.MethodGet, path: "/./index.html", wantStatus: http.StatusOK, wantBody: "INDEX", wantType: "text/html"},
		{name: "dot segment asset", h: h, method: http.MethodGet, path: "/assets/./app.js", wantStatus: http.StatusOK, wantBody: "JS", wantType: "javascript"},
		{name: "collapse into api is still static", h: h, method: http.MethodGet, path: "/foo/../api/status", wantStatus: http.StatusOK, wantBody: "INDEX", wantType: "text/html"},
		{name: "cleaned missing path falls back", h: h, method: http.MethodGet, path: "/secret.txt", wantStatus: http.StatusOK, wantBody: "INDEX", wantType: "text/html"},
		{name: "no index", h: emptyH, method: http.MethodGet, path: "/missing", wantStatus: http.StatusNotFound, jsonErr: `{"error":"not found"}`},
		{name: "existing file without index", h: emptyH, method: http.MethodGet, path: "/nope", wantStatus: http.StatusNotFound, jsonErr: `{"error":"not found"}`},
		{name: "post static", h: h, method: http.MethodPost, path: "/tasks/1", wantStatus: http.StatusMethodNotAllowed, jsonErr: `{"error":"method not allowed"}`, wantAllow: "GET, HEAD"},
		{name: "put static", h: h, method: http.MethodPut, path: "/tasks/1", wantStatus: http.StatusMethodNotAllowed, jsonErr: `{"error":"method not allowed"}`, wantAllow: "GET, HEAD"},
		{name: "no web dir", h: noWeb, method: http.MethodGet, path: "/", wantStatus: http.StatusNotFound, jsonErr: `{"error":"not found"}`},
		{name: "plain file", h: h, method: http.MethodGet, path: "/plain", wantStatus: http.StatusOK, wantBody: "PLAIN"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			req := loopbackRequest(tt.method, tt.path)
			rr := httptest.NewRecorder()
			tt.h.ServeHTTP(rr, req)
			if rr.Code != tt.wantStatus {
				t.Fatalf("status %d, want %d, body %s", rr.Code, tt.wantStatus, rr.Body.String())
			}
			if tt.wantBody != "" && rr.Body.String() != tt.wantBody {
				t.Fatalf("body %q, want %q", rr.Body.String(), tt.wantBody)
			}
			if tt.emptyBody && rr.Body.Len() != 0 {
				t.Fatalf("body %q, want empty", rr.Body.String())
			}
			if strings.Contains(rr.Body.String(), "SECRET") {
				t.Fatal("response contained a file from outside the web dir")
			}
			if tt.wantType != "" && !strings.Contains(rr.Header().Get("Content-Type"), tt.wantType) {
				t.Fatalf("content type %q, want substring %q", rr.Header().Get("Content-Type"), tt.wantType)
			}
			if tt.jsonErr != "" {
				if rr.Body.String() != tt.jsonErr {
					t.Fatalf("body %s, want %s", rr.Body.String(), tt.jsonErr)
				}
				if ct := rr.Header().Get("Content-Type"); ct != "application/json" {
					t.Fatalf("content type %q", ct)
				}
			}
			if tt.wantAllow != "" && rr.Header().Get("Allow") != tt.wantAllow {
				t.Fatalf("Allow %q, want %q", rr.Header().Get("Allow"), tt.wantAllow)
			}
		})
	}

	// A file that exists in a directory with no index.html is still served.
	if err := os.WriteFile(filepath.Join(empty, "app.js"), []byte("ONLY"), 0o644); err != nil {
		t.Fatal(err)
	}
	req := loopbackRequest(http.MethodGet, "/app.js")
	rr := httptest.NewRecorder()
	emptyH.ServeHTTP(rr, req)
	if rr.Code != http.StatusOK || rr.Body.String() != "ONLY" {
		t.Fatalf("status %d body %q", rr.Code, rr.Body.String())
	}
}
