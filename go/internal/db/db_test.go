package db

import (
	"bytes"
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
)

func TestProbeNilDB(t *testing.T) {
	err := (Probe{}).Ping(context.Background())
	if err == nil {
		t.Fatal("expected error")
	}
}

func TestProbeSelectOne(t *testing.T) {
	tests := []struct {
		name    string
		value   int64
		retErr  error
		cancel  bool
		wantErr string
		absent  string
		wantSQL string
	}{
		{name: "ok", value: 1, wantSQL: "SELECT 1"},
		{name: "wrong value", value: 2, wantErr: "error: unreachable", wantSQL: "SELECT 1"},
		{name: "query error", value: 1, retErr: errors.New("connection refused"), wantErr: "error: unreachable", absent: "connection refused", wantSQL: "SELECT 1"},
		{name: "canceled", value: 1, cancel: true, wantErr: "error: context canceled"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			drv := &probeDrv{value: tt.value, retErr: tt.retErr}
			sqlDB := openProbeDB(t, drv)
			ctx := context.Background()
			if tt.cancel {
				var cancel context.CancelFunc
				ctx, cancel = context.WithCancel(ctx)
				cancel()
			}
			err := (Probe{DB: sqlDB}).Ping(ctx)
			if tt.wantErr == "" {
				if err != nil {
					t.Fatalf("ping: %v", err)
				}
			} else if err == nil || err.Error() != tt.wantErr {
				t.Fatalf("error %v, want %q", err, tt.wantErr)
			}
			if tt.absent != "" && err != nil && strings.Contains(err.Error(), tt.absent) {
				t.Fatalf("driver text leaked: %v", err)
			}
			if tt.wantSQL != "" && drv.query != tt.wantSQL {
				t.Fatalf("query %q, want %q", drv.query, tt.wantSQL)
			}
		})
	}
}

func TestOpen(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := Open(ctx, "postgres://localhost/db")
	if err == nil || err.Error() != "error: context canceled" {
		t.Fatalf("canceled: %v", err)
	}
	_, err = Open(context.Background(), "  ")
	if err == nil || err.Error() != "error: invalid database URL" {
		t.Fatalf("empty url: %v", err)
	}
	sqlDB, err := Open(context.Background(), "postgres://postgres:postgres@127.0.0.1:1/none")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
}

func TestErrorText(t *testing.T) {
	parseErr := func(dsn string) error {
		t.Helper()
		_, err := pgconn.ParseConfig(dsn)
		if err == nil {
			t.Fatal("expected a configuration error")
		}
		return err
	}
	tests := []struct {
		name    string
		err     error
		want    string
		absent  string
		debug   string
		noDebug bool
	}{
		{name: "nil", want: ""},
		{name: "deadline", err: context.DeadlineExceeded, want: "error: timeout"},
		{name: "wrapped deadline", err: fmt.Errorf("select 1: %w", context.DeadlineExceeded), want: "error: timeout"},
		{name: "canceled", err: context.Canceled, want: "error: context canceled"},
		{name: "dial", err: errors.New("dial tcp: connection refused"), want: "error: unreachable", absent: "connection refused"},
		{
			name:   "encoded query",
			err:    parseErr("postgresql://u@127.0.0.1:1/%zz?%70assword=SECRET"),
			want:   "error: invalid database URL",
			absent: "SECRET",
		},
		{
			name:   "keyword quote",
			err:    parseErr(`password='alpha\'SECRET_ONE SECRET_TWO' host=127.0.0.1 port=1 sslmode=invalid`),
			want:   "error: invalid database URL",
			absent: "SECRET",
		},
		{
			name:   "userinfo",
			err:    parseErr("postgresql://u:SECRET@127.0.0.1:1/db?sslmode=invalid"),
			want:   "error: invalid database URL",
			absent: "SECRET",
		},
		{
			name:    "keyword text is not a url",
			err:     errors.New(`password='alpha\'SECRET_ONE SECRET_TWO' sslmode=invalid`),
			want:    "error: unreachable",
			absent:  "SECRET",
			noDebug: true,
		},
		{
			name:   "url password is redacted in debug",
			err:    errors.New("postgresql://u:SECRET@127.0.0.1:1/db"),
			want:   "error: unreachable",
			absent: "SECRET",
			debug:  "xxxxx",
		},
		{
			name:    "query password is left out of debug",
			err:     errors.New("postgresql://u@127.0.0.1:1/db?%70assword=SECRET"),
			want:    "error: unreachable",
			absent:  "SECRET",
			noDebug: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var logs bytes.Buffer
			prev := slog.Default()
			slog.SetDefault(slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug})))
			t.Cleanup(func() { slog.SetDefault(prev) })

			got := ErrorText(tt.err)
			if got != tt.want {
				t.Fatalf("got %q, want %q", got, tt.want)
			}
			if tt.absent != "" && (strings.Contains(got, tt.absent) || strings.Contains(logs.String(), tt.absent)) {
				t.Fatalf("leaked %q in status %q log %q", tt.absent, got, logs.String())
			}
			if tt.debug != "" && !strings.Contains(logs.String(), tt.debug) {
				t.Fatalf("debug log %q, want substring %q", logs.String(), tt.debug)
			}
			if tt.noDebug && logs.Len() != 0 {
				t.Fatalf("debug log %q, want empty", logs.String())
			}
		})
	}
}

var probeSeq atomic.Int64

func openProbeDB(t *testing.T, drv *probeDrv) *sql.DB {
	t.Helper()
	name := fmt.Sprintf("loopgo-probe-%d", probeSeq.Add(1))
	sql.Register(name, drv)
	sqlDB, err := sql.Open(name, "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	sqlDB.SetMaxOpenConns(1)
	return sqlDB
}

type probeDrv struct {
	query  string
	value  int64
	retErr error
}

func (d *probeDrv) Open(string) (driver.Conn, error) { return &probeConn{d: d}, nil }

type probeConn struct{ d *probeDrv }

func (c *probeConn) Prepare(query string) (driver.Stmt, error) {
	c.d.query = query
	if c.d.retErr != nil {
		return nil, c.d.retErr
	}
	return &probeStmt{value: c.d.value}, nil
}

func (c *probeConn) Close() error { return nil }

func (c *probeConn) Begin() (driver.Tx, error) { return nil, errors.New("no transaction") }

type probeStmt struct{ value int64 }

func (s *probeStmt) Close() error { return nil }

func (s *probeStmt) NumInput() int { return 0 }

func (s *probeStmt) Exec([]driver.Value) (driver.Result, error) {
	return nil, errors.New("exec not supported")
}

func (s *probeStmt) Query([]driver.Value) (driver.Rows, error) {
	return &probeRows{value: s.value}, nil
}

type probeRows struct {
	value int64
	done  bool
}

func (r *probeRows) Columns() []string { return []string{"one"} }

func (r *probeRows) Close() error { return nil }

func (r *probeRows) Next(dest []driver.Value) error {
	if r.done {
		return io.EOF
	}
	r.done = true
	dest[0] = r.value
	return nil
}
