package db

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

const disposableAdminURL = "postgresql://postgres:postgres@127.0.0.1:54329/postgres"

// withDisposableDB creates a loopgo_* database on the local test server and
// drops that database when the test ends. prefix is not a database name: two
// tests, including two processes, can pass the same prefix and must not drop
// each other's database. A name that already exists is left alone and another
// name is tried.
func withDisposableDB(t *testing.T, prefix string) string {
	t.Helper()
	if !disposableDBName(prefix) {
		t.Fatalf("refusing database name %q", prefix)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	admin, err := pgconn.Connect(ctx, disposableAdminURL)
	if err != nil {
		t.Fatalf("admin database: %v", err)
	}
	defer admin.Close(ctx)

	var created string
	for attempt := 0; attempt < 5; attempt++ {
		name, err := uniqueDisposableName(prefix)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := admin.Exec(ctx, "CREATE DATABASE "+name).ReadAll(); err != nil {
			var pgErr *pgconn.PgError
			if errors.As(err, &pgErr) && pgErr.Code == "42P04" {
				continue
			}
			t.Fatalf("create database %s: %v", name, err)
		}
		created = name
		break
	}
	if created == "" {
		t.Fatalf("create database %s: name already exists", prefix)
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
		if err := dropDisposable(cctx, again, created); err != nil {
			t.Error(err)
		}
	})
	return "postgresql://postgres:postgres@127.0.0.1:54329/" + created
}

func uniqueDisposableName(prefix string) (string, error) {
	var buf [4]byte
	if _, err := rand.Read(buf[:]); err != nil {
		return "", fmt.Errorf("database name: %w", err)
	}
	suffix := "_" + strconv.Itoa(os.Getpid()) + "_" + hex.EncodeToString(buf[:])
	name := prefix + suffix
	if len(name) > 63 {
		keep := 63 - len(suffix)
		if keep < len("loopgo_") {
			return "", fmt.Errorf("refusing database name %q", prefix)
		}
		name = prefix[:keep] + suffix
	}
	if !disposableDBName(name) {
		return "", fmt.Errorf("refusing database name %q", name)
	}
	return name, nil
}

func dropDisposable(ctx context.Context, admin *pgconn.PgConn, name string) error {
	if !disposableDBName(name) {
		return fmt.Errorf("refusing database name %q", name)
	}
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
	if !strings.HasPrefix(name, "loopgo_") || len(name) > 63 || len(name) <= len("loopgo_") {
		return false
	}
	for _, r := range name {
		if (r < 'a' || r > 'z') && (r < '0' || r > '9') && r != '_' {
			return false
		}
	}
	return true
}

// TestWithDisposableDBLeavesForeignDatabase holds a connection to a database
// whose name is the helper's prefix. The helper used to terminate and drop
// that name before creating it, which killed the other connection.
func TestWithDisposableDBLeavesForeignDatabase(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	admin, err := pgconn.Connect(ctx, disposableAdminURL)
	if err != nil {
		t.Fatalf("admin database: %v", err)
	}
	defer admin.Close(ctx)

	var foreign string
	for attempt := 0; attempt < 5; attempt++ {
		name, err := uniqueDisposableName("loopgo_hold")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := admin.Exec(ctx, "CREATE DATABASE "+name).ReadAll(); err != nil {
			var pgErr *pgconn.PgError
			if errors.As(err, &pgErr) && pgErr.Code == "42P04" {
				continue
			}
			t.Fatal(err)
		}
		foreign = name
		break
	}
	if foreign == "" {
		t.Fatal("create foreign database")
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
		if err := dropDisposable(cctx, again, foreign); err != nil {
			t.Error(err)
		}
	})

	held, err := pgconn.Connect(ctx, "postgresql://postgres:postgres@127.0.0.1:54329/"+foreign)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { held.Close(context.Background()) })

	dsn := withDisposableDB(t, foreign)
	if strings.HasSuffix(dsn, "/"+foreign) {
		t.Fatalf("helper reused %s", dsn)
	}
	if _, err := held.Exec(ctx, "SELECT 1").ReadAll(); err != nil {
		t.Fatalf("foreign database %s was interrupted: %v", foreign, err)
	}
}
