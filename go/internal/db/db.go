// Package db opens the Looper PostgreSQL database.
// Schema changes live in looper/db/schema.sql. This package does not copy that file.
package db

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"net/url"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib" // registers the "pgx" driver
)

// ProbeTimeout is the deadline for one SELECT 1 health check.
const ProbeTimeout = 2 * time.Second

// probeQuery is the health check. It stays a literal SELECT 1.
const probeQuery = "SELECT 1"

// Open returns a pool. It does not dial: the first query does.
// A failure is a safeError. Driver text is not wrapped: it can contain the URL.
func Open(ctx context.Context, url string) (*sql.DB, error) {
	if err := ctx.Err(); err != nil {
		return nil, hide(err)
	}
	if strings.TrimSpace(url) == "" {
		return nil, &safeError{msg: errInvalidDatabaseURL}
	}
	sqlDB, err := sql.Open("pgx", url)
	if err != nil {
		return nil, hide(err)
	}
	sqlDB.SetMaxOpenConns(8)
	sqlDB.SetMaxIdleConns(2)
	sqlDB.SetConnMaxLifetime(30 * time.Minute)
	return sqlDB, nil
}

// Probe runs SELECT 1. The caller sets the deadline on ctx.
type Probe struct {
	DB *sql.DB
}

// Ping implements the HTTP health check.
// Failures are safeError values. Driver text is not returned or logged.
func (p Probe) Ping(ctx context.Context) error {
	if p.DB == nil {
		return &safeError{msg: errUnreachable}
	}
	var one int
	if err := p.DB.QueryRowContext(ctx, probeQuery).Scan(&one); err != nil {
		return hide(err)
	}
	if one != 1 {
		return &safeError{msg: errUnreachable}
	}
	return nil
}

const (
	errInvalidDatabaseURL = "error: invalid database URL"
	errUnreachable        = "error: unreachable"
	errTimeout            = "error: timeout"
	errCanceled           = "error: context canceled"
)

// safeError is a database failure safe to return and log.
// It does not unwrap. Driver text can contain the connection string.
type safeError struct{ msg string }

func (e *safeError) Error() string { return e.msg }

// ErrorText returns the fixed status for a database failure.
// Parse failures and an empty URL are "error: invalid database URL".
// context.DeadlineExceeded is "error: timeout". Other open, connect, and
// ping failures are "error: unreachable". context.Canceled stays distinct.
// The driver error is not part of the result. It is written at debug level
// only when the whole error parses as a URL and url.Redacted changes it.
// A query string is left out: url.Redacted does not hide those passwords.
func ErrorText(err error) string {
	if err == nil {
		return ""
	}
	var safe *safeError
	if errors.As(err, &safe) {
		return safe.msg
	}
	logDebugURL(err)
	return classify(err)
}

func hide(err error) error {
	if err == nil {
		return nil
	}
	return &safeError{msg: ErrorText(err)}
}

func classify(err error) string {
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return errTimeout
	case errors.Is(err, context.Canceled):
		return errCanceled
	default:
		var parseErr *pgconn.ParseConfigError
		if errors.As(err, &parseErr) {
			return errInvalidDatabaseURL
		}
		return errUnreachable
	}
}

// logDebugURL logs err at debug when err.Error parses as a URL.
// The logged value is url.Redacted. Anything else is left out, including a
// URL whose query might still hold a password after redaction.
func logDebugURL(err error) {
	raw := err.Error()
	u, perr := url.Parse(raw)
	if perr != nil || u == nil || u.Scheme == "" || u.Host == "" || u.RawQuery != "" {
		return
	}
	if u.Scheme != "postgres" && u.Scheme != "postgresql" {
		return
	}
	redacted := u.Redacted()
	if redacted == raw {
		return
	}
	slog.Debug("database error", "url", redacted)
}
