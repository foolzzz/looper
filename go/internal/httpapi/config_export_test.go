package httpapi

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/configtransfer"
	"github.com/foolzzz/looper/go/internal/db"
)

type exportReads struct {
	Reader
	options []configtransfer.Options
	export  func(context.Context, io.Writer, configtransfer.Options) error
}

func (r *exportReads) ExportConfig(ctx context.Context, out io.Writer, options configtransfer.Options) (configtransfer.Summary, error) {
	r.options = append(r.options, options)
	return configtransfer.Summary{}, r.export(ctx, out, options)
}

func TestConfigExportQueryAndHeaders(t *testing.T) {
	const secret = "synthetic-export-secret"
	now := time.Date(2026, 10, 19, 23, 30, 0, 0, time.FixedZone("west", -7*60*60))
	tests := []struct {
		query             string
		projects, secrets bool
	}{
		{"", false, false},
		{"?projects=1&secrets=1", true, true},
		{"?projects=0&secrets=true", false, false},
		{"?projects=1;&secrets=%", false, false},
		{"?projects=1&projects=0&secrets=1&secrets=", false, true},
		{"?secrets=1&secrets=0", false, false},
		{"?projects=%31&secrets=%31", true, true},
		{"?projects=+1&secrets=1+", false, false},
		{"?unknown=1&secrets=YES", false, false},
	}
	for _, tt := range tests {
		t.Run(tt.query, func(t *testing.T) {
			reads := &exportReads{export: func(ctx context.Context, out io.Writer, options configtransfer.Options) error {
				if deadline, ok := ctx.Deadline(); !ok || time.Until(deadline) > readTimeout {
					t.Fatal("export has no bounded read context")
				}
				for _, chunk := range []string{"{\n", `"value": "`, "公开", `"`, "\n}"} {
					if _, err := io.WriteString(out, chunk); err != nil {
						return err
					}
				}
				if options.IncludeSecrets {
					_, err := io.WriteString(out, secret)
					return err
				}
				return nil
			}}
			s := &Server{Reads: reads, Version: "go-development", Now: func() time.Time { return now }}
			rr := httptest.NewRecorder()
			s.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/config/export"+tt.query))
			if rr.Code != http.StatusOK || !rr.Flushed {
				t.Fatalf("status %d flushed %v body %s", rr.Code, rr.Flushed, rr.Body.String())
			}
			for key, want := range map[string]string{
				"Content-Type":        "application/json; charset=utf-8",
				"Content-Disposition": `attachment; filename="looper-config-2026-10-19.json"`,
				"Cache-Control":       "no-store",
				"Connection":          "close",
				"Content-Length":      "",
			} {
				if got := rr.Header().Get(key); got != want {
					t.Errorf("%s = %q, want %q", key, got, want)
				}
			}
			want := "{\n\"value\": \"公开\"\n}"
			if tt.secrets {
				want += secret
			}
			if rr.Body.String() != want {
				t.Fatalf("body %q, want %q", rr.Body.String(), want)
			}
			if len(reads.options) != 1 {
				t.Fatalf("export called %d times", len(reads.options))
			}
			options := reads.options[0]
			if options.IncludeProjects != tt.projects || options.IncludeSecrets != tt.secrets || options.Now != now || options.Version != db.PythonVersion {
				t.Fatalf("options %+v", options)
			}
		})
	}
}

func TestConfigExportRoutingAndGuard(t *testing.T) {
	tests := []struct {
		method, path, host, origin string
		status                     int
	}{
		{method: http.MethodHead, path: "/api/config/export", status: http.StatusNotFound},
		{method: http.MethodPost, path: "/api/config/export", status: http.StatusNotFound},
		{method: http.MethodPut, path: "/api/config/export", status: http.StatusNotFound},
		{method: http.MethodGet, path: "/api/config/export/", status: http.StatusNotFound},
		{method: http.MethodGet, path: "/api/config/%65xport", status: http.StatusNotFound},
		{method: http.MethodGet, path: "/api//config/export", status: http.StatusNotFound},
		{method: http.MethodGet, path: "/api/config/export", host: "attacker.invalid", status: http.StatusForbidden},
		{method: http.MethodGet, path: "/api/config/export", origin: "https://attacker.invalid", status: http.StatusForbidden},
	}
	for _, tt := range tests {
		t.Run(tt.method+tt.path+tt.host+tt.origin, func(t *testing.T) {
			reads := &exportReads{export: func(context.Context, io.Writer, configtransfer.Options) error {
				return errors.New("must not run")
			}}
			r := loopbackRequest(tt.method, tt.path)
			if tt.host != "" {
				r.Host = tt.host
			}
			if tt.origin != "" {
				r.Header.Set("Origin", tt.origin)
			}
			rr := httptest.NewRecorder()
			(&Server{Reads: reads}).Handler().ServeHTTP(rr, r)
			if rr.Code != tt.status || len(reads.options) != 0 {
				t.Fatalf("status %d export calls %d body %s", rr.Code, len(reads.options), rr.Body.String())
			}
		})
	}
}

func TestConfigExportErrors(t *testing.T) {
	logs := captureLogs(t)
	tests := []struct {
		name, prefix string
		reads        Reader
	}{
		{name: "nil store"},
		{name: "unsupported store", reads: &recordingReads{}},
		{name: "before first chunk", reads: &exportReads{export: func(context.Context, io.Writer, configtransfer.Options) error {
			return errors.New("synthetic-private-driver-error")
		}}},
		{name: "no chunks", reads: &exportReads{export: func(context.Context, io.Writer, configtransfer.Options) error { return nil }}},
		{name: "after first chunk", prefix: "{\n", reads: &exportReads{export: func(_ context.Context, out io.Writer, _ configtransfer.Options) error {
			if _, err := io.WriteString(out, "{\n"); err != nil {
				return err
			}
			return errors.New("synthetic-private-driver-error")
		}}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rr := httptest.NewRecorder()
			(&Server{Reads: tt.reads}).Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/config/export"))
			want, status := `{"error": "internal server error"}`, http.StatusInternalServerError
			if tt.prefix != "" {
				want, status = tt.prefix, http.StatusOK
			}
			if rr.Code != status || rr.Body.String() != want {
				t.Fatalf("status %d body %q, want %d %q", rr.Code, rr.Body.String(), status, want)
			}
			if tt.prefix == "" && rr.Header().Get("Content-Disposition") != "" {
				t.Fatal("failed export has download headers")
			}
		})
	}
	if strings.Contains(logs.String(), "synthetic-private-driver-error") {
		t.Fatal("driver error leaked to logs")
	}
}

func TestConfigExportCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	reads := &exportReads{export: func(exportCtx context.Context, out io.Writer, _ configtransfer.Options) error {
		if _, err := io.WriteString(out, "{\n"); err != nil {
			return err
		}
		cancel()
		if !errors.Is(exportCtx.Err(), context.Canceled) {
			t.Fatalf("request cancellation not propagated: %v", exportCtx.Err())
		}
		_, err := io.WriteString(out, `"late":true}`)
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("writer ignored cancellation: %v", err)
		}
		return err
	}}
	rr := httptest.NewRecorder()
	(&Server{Reads: reads}).Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/config/export").WithContext(ctx))
	if rr.Code != http.StatusOK || rr.Body.String() != "{\n" {
		t.Fatalf("status %d body %q", rr.Code, rr.Body.String())
	}
}

type failingExportResponse struct {
	*httptest.ResponseRecorder
	short, flushError bool
}

func (w failingExportResponse) Write(p []byte) (int, error) {
	if w.flushError {
		return w.ResponseRecorder.Write(p)
	}
	if w.short {
		return w.ResponseRecorder.Write(p[:1])
	}
	return 0, io.ErrClosedPipe
}

func (w failingExportResponse) FlushError() error {
	return io.ErrClosedPipe
}

func TestConfigExportWriteErrors(t *testing.T) {
	for _, mode := range []string{"disconnected", "short write", "flush error"} {
		t.Run(mode, func(t *testing.T) {
			var got error
			reads := &exportReads{export: func(_ context.Context, out io.Writer, _ configtransfer.Options) error {
				_, got = io.WriteString(out, "{\n")
				return got
			}}
			response := failingExportResponse{ResponseRecorder: httptest.NewRecorder(), short: mode == "short write", flushError: mode == "flush error"}
			(&Server{Reads: reads}).Handler().ServeHTTP(response, loopbackRequest(http.MethodGet, "/api/config/export"))
			want := io.ErrClosedPipe
			if mode == "short write" {
				want = io.ErrShortWrite
			}
			if !errors.Is(got, want) {
				t.Fatalf("writer error %v, want %v", got, want)
			}
			if strings.Contains(response.Body.String(), "error") {
				t.Fatalf("error appended to partial stream: %q", response.Body.String())
			}
		})
	}
}
