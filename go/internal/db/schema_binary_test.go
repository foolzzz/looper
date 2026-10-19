package db

import (
	"bytes"
	"context"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

// TestBuiltBinaryInitializesSchemaOutsideTree builds a trimpath binary and
// runs it with a working directory that cannot see looper/db/schema.sql.
// EnsureSchema has to apply the embedded copy. This process must not call
// EnsureSchema: go test is not built with -trimpath, so a Caller walk from
// here would find the source file and hide that regression.
func TestBuiltBinaryInitializesSchemaOutsideTree(t *testing.T) {
	mod := moduleRoot(t)
	bin := filepath.Join(t.TempDir(), "looper")
	buildCtx, buildCancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer buildCancel()
	build := exec.CommandContext(buildCtx, "go", "build", "-trimpath", "-o", bin, "./cmd/looper")
	build.Dir = mod
	build.Env = append(os.Environ(), "CGO_ENABLED=0", "GOTOOLCHAIN=local")
	out, err := build.CombinedOutput()
	if err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	blob, err := os.ReadFile(bin)
	if err != nil {
		t.Fatal(err)
	}
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller")
	}
	absSchema := filepath.Join(filepath.Dir(file), "schema.go")
	if !filepath.IsAbs(absSchema) || bytes.Contains(blob, []byte(absSchema)) {
		t.Fatalf("binary still contains %s; -trimpath did not remove it", absSchema)
	}

	work := t.TempDir()
	if sourceSchemaVisible(work) {
		t.Fatalf("looper/db/schema.sql is visible within 8 parents of %s", work)
	}
	dsn := withDisposableDB(t, "loopgo_g1efix_schema")

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	if err := ln.Close(); err != nil {
		t.Fatal(err)
	}

	serve := exec.Command(bin, "serve", "--port", strconv.Itoa(port), "--db-url", dsn)
	serve.Dir = work
	var logs lockedLog
	serve.Stdout = &logs
	serve.Stderr = &logs
	if err := serve.Start(); err != nil {
		t.Fatal(err)
	}
	died := make(chan struct{})
	var waitErr error
	go func() {
		waitErr = serve.Wait()
		close(died)
	}()
	t.Cleanup(func() {
		if serve.Process != nil {
			_ = serve.Process.Signal(os.Interrupt)
		}
		select {
		case <-died:
		case <-time.After(15 * time.Second):
			if serve.Process != nil {
				_ = serve.Process.Kill()
			}
			<-died
		}
	})

	deadline := time.Now().Add(45 * time.Second)
	var detail string
	for {
		var ready bool
		ready, detail = schemaTablesReady(dsn)
		if ready {
			break
		}
		select {
		case <-died:
			t.Fatalf("server exited before the schema was ready: %v\n%s", waitErr, redactTestDSN(logs.String()))
		default:
		}
		if time.Now().After(deadline) {
			t.Fatalf("schema tables missing (%s)\n%s", detail, redactTestDSN(logs.String()))
		}
		time.Sleep(200 * time.Millisecond)
	}
	logged := logs.String()
	if strings.Contains(logged, "ensure schema") || strings.Contains(logged, "postgres:postgres@") {
		t.Fatalf("server log:\n%s", redactTestDSN(logged))
	}
}

func moduleRoot(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller")
	}
	dir := filepath.Dir(file)
	for range 6 {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	t.Fatal("go.mod not found")
	return ""
}

// sourceSchemaVisible reports whether FindSchema could see looper/db/schema.sql
// by walking up from dir. Eight steps is the walk FindSchema uses.
func sourceSchemaVisible(dir string) bool {
	for range 8 {
		cand := filepath.Join(dir, "looper", "db", "schema.sql")
		info, err := os.Stat(cand)
		if err == nil && info.Mode().IsRegular() {
			return true
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return false
}

func schemaTablesReady(dsn string) (bool, string) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	conn, err := pgconn.Connect(ctx, dsn)
	if err != nil {
		return false, redactTestDSN(err.Error())
	}
	defer conn.Close(ctx)
	results, err := conn.Exec(ctx, `SELECT CASE WHEN to_regclass('public.tasks') IS NOT NULL AND to_regclass('public.channel_rejected_senders') IS NOT NULL THEN 'yes' ELSE 'no' END`).ReadAll()
	if err != nil {
		return false, redactTestDSN(err.Error())
	}
	if len(results) == 0 || len(results[0].Rows) == 0 || len(results[0].Rows[0]) == 0 {
		return false, "empty result"
	}
	got := string(results[0].Rows[0][0])
	return got == "yes", got
}

func redactTestDSN(s string) string {
	return strings.ReplaceAll(s, "postgres:postgres@", "postgres:***@")
}

// lockedLog is the server's stdout and stderr. The exec copier writes it
// while the test reads it.
type lockedLog struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (l *lockedLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.Write(p)
}

func (l *lockedLog) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.String()
}
