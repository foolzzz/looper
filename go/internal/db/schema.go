package db

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/jackc/pgx/v5/pgconn"
)

// schemaLock is the same advisory transaction lock Python takes in ensure_schema (0x6C6F6F70, "loop").
const schemaLock int64 = 0x6C6F6F70 // 1819242352, Python's _SCHEMA_LOCK

// FindSchema locates looper/db/schema.sql by walking up from the working
// directory and from this source file. The Go module does not copy the schema.
func FindSchema() (string, error) {
	var roots []string
	if wd, err := os.Getwd(); err == nil {
		roots = append(roots, wd)
	}
	if _, file, _, ok := runtime.Caller(0); ok {
		roots = append(roots, filepath.Dir(file))
	}
	for _, root := range roots {
		dir := root
		for range 8 {
			cand := filepath.Join(dir, "looper", "db", "schema.sql")
			info, err := os.Stat(cand)
			if err == nil && info.Mode().IsRegular() {
				return cand, nil
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	return "", errors.New("looper/db/schema.sql not found")
}

// EnsureSchema applies looper/db/schema.sql to an empty database.
// It does not run the Python data migrations or record a schema fingerprint.
// The script runs as one transaction, matching ensure_schema's lock.
// A connect failure is a safeError: driver text can contain the URL.
func EnsureSchema(ctx context.Context, databaseURL string) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("ensure schema: %w", hide(err))
	}
	path, err := FindSchema()
	if err != nil {
		return fmt.Errorf("ensure schema: %w", err)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("ensure schema: read schema: %w", err)
	}
	script := fmt.Sprintf("BEGIN;\nSELECT pg_advisory_xact_lock(%d);\n%s\nCOMMIT;\n", schemaLock, body)
	if err := execScript(ctx, databaseURL, script); err != nil {
		return fmt.Errorf("ensure schema: %w", err)
	}
	return nil
}

// ExecScript runs a multi-statement SQL script with the simple query protocol.
// database/sql Exec rejects multiple statements. Connect failures are safeError values.
func ExecScript(ctx context.Context, databaseURL, script string) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("exec script: %w", hide(err))
	}
	if err := execScript(ctx, databaseURL, script); err != nil {
		return fmt.Errorf("exec script: %w", err)
	}
	return nil
}

func execScript(ctx context.Context, databaseURL, script string) error {
	if strings.TrimSpace(databaseURL) == "" {
		return &safeError{msg: errInvalidDatabaseURL}
	}
	conn, err := pgconn.Connect(ctx, databaseURL)
	if err != nil {
		return hide(err)
	}
	defer conn.Close(ctx)
	if _, err := conn.Exec(ctx, script).ReadAll(); err != nil {
		return schemaSQLError(err)
	}
	return nil
}

// schemaSQLError keeps a server error's code and message.
// The connection string and the script text are left out.
func schemaSQLError(err error) error {
	if err == nil {
		return nil
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return &safeError{msg: "error: " + pgErr.Code + " " + pgErr.Message}
	}
	return hide(err)
}
