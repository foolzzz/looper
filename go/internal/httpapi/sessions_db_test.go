package httpapi

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/foolzzz/looper/go/internal/db"
)

// TestChatSessionsReportsUndefinedTable is the real store path behind
// sessionErrorName. Renaming chat_messages makes PostgreSQL raise 42P01.
// Python's list_sessions puts asyncpg's class name in the JSON error.
func TestChatSessionsReportsUndefinedTable(t *testing.T) {
	dsn := withHTTPDisposableDB(t, "loopgo_g1efix_sessions")
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
	if _, err := sqlDB.ExecContext(ctx, `ALTER TABLE chat_messages RENAME TO chat_messages_hidden`); err != nil {
		t.Fatal(err)
	}

	srv := New("dev", db.Probe{DB: sqlDB}, "")
	srv.Reads = db.NewStore(sqlDB)
	logs := captureLogs(t)
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/sessions"))
	const want = `{"error":"the database could not be read (UndefinedTableError)"}`
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != want {
		t.Fatalf("sessions %d %s", rr.Code, rr.Body.String())
	}
	logged := logs.String()
	if !strings.Contains(logged, "42P01") || strings.Contains(logged, "postgres:postgres@") ||
		strings.Contains(logged, "errorString") || strings.Contains(logged, "wrapError") {
		t.Fatalf("log %q", logged)
	}
}

const httpDisposableAdminURL = "postgresql://postgres:postgres@127.0.0.1:54329/postgres"

func withHTTPDisposableDB(t *testing.T, name string) string {
	t.Helper()
	if !httpDisposableName(name) {
		t.Fatalf("refusing database name %q", name)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	admin, err := pgconn.Connect(ctx, httpDisposableAdminURL)
	if err != nil {
		t.Fatalf("admin database: %v", err)
	}
	defer admin.Close(ctx)
	if err := dropHTTPDisposable(ctx, admin, name); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, "CREATE DATABASE "+name).ReadAll(); err != nil {
		t.Fatalf("create database %s: %v", name, err)
	}
	t.Cleanup(func() {
		cctx, ccancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer ccancel()
		again, err := pgconn.Connect(cctx, httpDisposableAdminURL)
		if err != nil {
			t.Errorf("admin database during drop: %v", err)
			return
		}
		defer again.Close(cctx)
		if err := dropHTTPDisposable(cctx, again, name); err != nil {
			t.Error(err)
		}
	})
	return "postgresql://postgres:postgres@127.0.0.1:54329/" + name
}

func dropHTTPDisposable(ctx context.Context, admin *pgconn.PgConn, name string) error {
	q := "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '" + name + "' AND pid <> pg_backend_pid()"
	if _, err := admin.Exec(ctx, q).ReadAll(); err != nil {
		return fmt.Errorf("terminate %s: %w", name, err)
	}
	if _, err := admin.Exec(ctx, "DROP DATABASE IF EXISTS "+name).ReadAll(); err != nil {
		return fmt.Errorf("drop %s: %w", name, err)
	}
	return nil
}

func httpDisposableName(name string) bool {
	if !strings.HasPrefix(name, "loopgo_") || len(name) > 63 {
		return false
	}
	for _, r := range name {
		if (r < 'a' || r > 'z') && (r < '0' || r > '9') && r != '_' {
			return false
		}
	}
	return true
}
