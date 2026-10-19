package db

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

const disposableAdminURL = "postgresql://postgres:postgres@127.0.0.1:54329/postgres"

// withDisposableDB creates loopgo_* on the local test server and drops it
// when the test ends. The name has to be a plain identifier.
func withDisposableDB(t *testing.T, name string) string {
	t.Helper()
	if !disposableDBName(name) {
		t.Fatalf("refusing database name %q", name)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	admin, err := pgconn.Connect(ctx, disposableAdminURL)
	if err != nil {
		t.Fatalf("admin database: %v", err)
	}
	defer admin.Close(ctx)
	if err := dropDisposable(ctx, admin, name); err != nil {
		t.Fatal(err)
	}
	if _, err := admin.Exec(ctx, "CREATE DATABASE "+name).ReadAll(); err != nil {
		t.Fatalf("create database %s: %v", name, err)
	}
	t.Cleanup(func() {
		cctx, ccancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer ccancel()
		again, err := pgconn.Connect(cctx, disposableAdminURL)
		if err != nil {
			t.Errorf("admin database during drop: %v", err)
			return
		}
		defer again.Close(cctx)
		if err := dropDisposable(cctx, again, name); err != nil {
			t.Error(err)
		}
	})
	return "postgresql://postgres:postgres@127.0.0.1:54329/" + name
}

func dropDisposable(ctx context.Context, admin *pgconn.PgConn, name string) error {
	q := "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '" + name + "' AND pid <> pg_backend_pid()"
	if _, err := admin.Exec(ctx, q).ReadAll(); err != nil {
		return fmt.Errorf("terminate %s: %w", name, err)
	}
	if _, err := admin.Exec(ctx, "DROP DATABASE IF EXISTS "+name).ReadAll(); err != nil {
		return fmt.Errorf("drop %s: %w", name, err)
	}
	return nil
}

func disposableDBName(name string) bool {
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
