package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/operatorauth"
	"github.com/jackc/pgx/v5/pgconn"
)

// TestServeLoadsStoredScheduleMinInterval is scheduler_settings.load followed
// by the project schedules limits. A stored 30, with no environment override,
// is 30 seconds plain and 30 seconds gated. The parity suite builds a handler
// without this startup load, so it still sees the built-in 300/60.
func TestServeLoadsStoredScheduleMinInterval(t *testing.T) {
	looper, looperSet := os.LookupEnv("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS")
	loopx, loopxSet := os.LookupEnv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS")
	t.Cleanup(func() {
		restoreScheduleEnv("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", looper, looperSet)
		restoreScheduleEnv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", loopx, loopxSet)
		// The loader keeps the interval for the process. Put back the value
		// the restored environment selects so later tests are not left on 30.
		_ = db.ConfigureScheduleMinInterval()
	})
	restoreScheduleEnv("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", "", false)
	restoreScheduleEnv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", "", false)

	dsn := withServeDisposableDB(t, "loopgo_g1rfix_min")
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	if err := db.EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	sqlDB, err := db.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer sqlDB.Close()
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO projects (id, name) VALUES ('proj-minint01', 'Min')`); err != nil {
		t.Fatal(err)
	}
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO system_settings (key, value) VALUES ('scheduler.min_interval_seconds', '30')`); err != nil {
		t.Fatal(err)
	}
	sqlDB.Close()

	port := freeLoopbackPort(t)
	root := t.TempDir()
	serveCtx, stop := context.WithCancel(context.Background())
	defer stop()
	done := make(chan error, 1)
	go func() {
		done <- serveContext(serveCtx, []string{
			"--port", strconv.Itoa(port),
			"--db-url", dsn,
			"--runtime-root", root,
		}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	}()

	client := &http.Client{Timeout: 5 * time.Second}
	url := fmt.Sprintf("http://127.0.0.1:%d/api/projects/proj-minint01/schedules", port)
	var resp *http.Response
	deadline := time.Now().Add(8 * time.Second)
	for {
		token, readErr := operatorauth.ReadToken(context.Background(), root)
		if readErr == nil && token != "" {
			req, reqErr := http.NewRequest(http.MethodGet, url, nil)
			if reqErr != nil {
				t.Fatal(reqErr)
			}
			req.Header.Set("Authorization", "Bearer "+token)
			resp, err = client.Do(req)
			if err == nil && resp.StatusCode != http.StatusUnauthorized {
				break
			}
			if resp != nil {
				resp.Body.Close()
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("server did not answer schedules: %v", err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	body, err := io.ReadAll(resp.Body)
	resp.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d body %s", resp.StatusCode, body)
	}
	var decoded struct {
		Limits struct {
			Plain int `json:"min_interval_seconds"`
			Gated int `json:"min_interval_gated_seconds"`
		} `json:"limits"`
	}
	if err := json.Unmarshal(body, &decoded); err != nil {
		t.Fatalf("decode %s: %v", body, err)
	}
	if decoded.Limits.Plain != 30 || decoded.Limits.Gated != 30 {
		t.Fatalf("limits %d/%d, want 30/30; body %s", decoded.Limits.Plain, decoded.Limits.Gated, body)
	}

	stop()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("shutdown exceeded the limit")
	}
}

func restoreScheduleEnv(key, value string, set bool) {
	if set {
		_ = os.Setenv(key, value)
		return
	}
	_ = os.Unsetenv(key)
}

const serveDisposableAdminURL = "postgresql://postgres:postgres@127.0.0.1:54329/postgres"

func withServeDisposableDB(t *testing.T, prefix string) string {
	t.Helper()
	if !serveDisposableName(prefix) {
		t.Fatalf("refusing database name %q", prefix)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	admin, err := pgconn.Connect(ctx, serveDisposableAdminURL)
	if err != nil {
		t.Fatalf("admin database: %v", err)
	}
	defer admin.Close(ctx)

	var created string
	for attempt := 0; attempt < 5; attempt++ {
		name, err := uniqueServeDisposableName(prefix)
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
		again, err := pgconn.Connect(cctx, serveDisposableAdminURL)
		if err != nil {
			t.Errorf("admin database during drop: %v", err)
			return
		}
		defer again.Close(cctx)
		if !serveDisposableName(created) {
			t.Errorf("refusing database name %q", created)
			return
		}
		terminate := "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '" + created + "' AND pid <> pg_backend_pid()"
		if _, err := again.Exec(cctx, terminate).ReadAll(); err != nil {
			t.Errorf("terminate %s: %v", created, err)
			return
		}
		if _, err := again.Exec(cctx, "DROP DATABASE IF EXISTS "+created).ReadAll(); err != nil {
			t.Errorf("drop %s: %v", created, err)
		}
	})
	return "postgresql://postgres:postgres@127.0.0.1:54329/" + created
}

func uniqueServeDisposableName(prefix string) (string, error) {
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
	if !serveDisposableName(name) {
		return "", fmt.Errorf("refusing database name %q", name)
	}
	return name, nil
}

func serveDisposableName(name string) bool {
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
