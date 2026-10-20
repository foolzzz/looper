package httpapi

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

const (
	testOperatorToken = "operator_" + "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	testAgentToken    = "agent_" + "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	authPort          = 18834
)

func newAuthServer(t *testing.T, token string) (*Server, http.Handler) {
	t.Helper()
	web := t.TempDir()
	for _, dir := range []string{"assets", "docs"} {
		if err := os.Mkdir(filepath.Join(web, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	files := map[string]string{
		"index.html":      "<html>dashboard</html>",
		"assets/app.js":   "console.log(1)",
		"docs/index.html": "DOC",
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(web, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	api := New("test", nil, web)
	api.OperatorAuth = true
	api.OperatorToken = token
	api.ManagerToken = testAgentToken
	api.Port = authPort
	return api, api.Handler()
}

func authRequest(method, target string, body io.Reader) *http.Request {
	req := httptest.NewRequest(method, "http://127.0.0.1:"+strconv.Itoa(authPort)+target, body)
	req.Host = "127.0.0.1:" + strconv.Itoa(authPort)
	req.RemoteAddr = "127.0.0.1:12345"
	return req
}

type boomBody struct {
	read bool
}

func (b *boomBody) Read([]byte) (int, error) {
	b.read = true
	return 0, errors.New("body was read")
}

func (b *boomBody) Close() error { return nil }

func TestOperatorRefusalLogOmitsCredential(t *testing.T) {
	var buf bytes.Buffer
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(old) })
	_, h := newAuthServer(t, testOperatorToken)
	req := authRequest(http.MethodGet, "/api/nothing-here", nil)
	req.Header.Set("Authorization", "Bearer "+testOperatorToken+"extra")
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req)
	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("status %d", rr.Code)
	}
	text := buf.String()
	if strings.Contains(text, testOperatorToken) {
		t.Fatal("log contained the credential")
	}
	if !strings.Contains(text, "no operator credential matched (one was presented)") {
		t.Fatalf("log %s", text)
	}
}

func TestSignInPostDoesNotReadBody(t *testing.T) {
	_, h := newAuthServer(t, testOperatorToken)
	for _, path := range []string{"/login", "/logout"} {
		t.Run(path, func(t *testing.T) {
			body := &boomBody{}
			req := authRequest(http.MethodPost, path, body)
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, req)
			if body.read {
				t.Fatal("handler read the body")
			}
			if rr.Code != http.StatusMethodNotAllowed {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if path == "/login" && rr.Header().Get("Content-Security-Policy") != signInCSP {
				t.Fatalf("csp %q", rr.Header().Get("Content-Security-Policy"))
			}
		})
	}
}

func TestAuthOffDoesNotGateTheDashboard(t *testing.T) {
	plain := New("test", nil, t.TempDir())
	if err := os.WriteFile(filepath.Join(plain.WebDir, "index.html"), []byte("INDEX"), 0o644); err != nil {
		t.Fatal(err)
	}
	ph := plain.Handler()
	rr := httptest.NewRecorder()
	ph.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/"))
	if rr.Code != http.StatusOK || rr.Body.String() != "INDEX" {
		t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	ph.ServeHTTP(rr, loopbackRequest(http.MethodOptions, "/"))
	if rr.Code != http.StatusNoContent {
		t.Fatalf("options status %d", rr.Code)
	}
	rr = httptest.NewRecorder()
	ph.ServeHTTP(rr, loopbackRequest(http.MethodPost, "/tasks/1"))
	if rr.Code != http.StatusMethodNotAllowed {
		t.Fatalf("post status %d", rr.Code)
	}
	rr = httptest.NewRecorder()
	ph.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/agent/v1/tools"))
	if rr.Code != http.StatusUnauthorized || strings.Contains(rr.Body.String(), "not found") {
		t.Fatalf("agent status %d body %s", rr.Code, rr.Body.String())
	}
}

func TestRefusalDoesNotDrainBody(t *testing.T) {
	missing := readOperatorGolden(t, "post-import-missing.http")
	agent := readOperatorGolden(t, "agent-missing.http")
	tests := []struct {
		name    string
		request string
		golden  []byte
	}{
		{name: "content-length-1", request: "POST /api/config/imports HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 1\r\n\r\n", golden: missing},
		{name: "chunked", request: "POST /api/config/imports HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nTransfer-Encoding: chunked\r\n\r\n", golden: missing},
		{name: "content-length-1mb", request: "POST /api/config/imports HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 1000000\r\n\r\n", golden: missing},
		{name: "agent-content-length-1", request: "GET /api/agent/v1/tools HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 1\r\n\r\n", golden: agent},
		{name: "login-content-length-1", request: "POST /login HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 1\r\n\r\n"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			api, h := newAuthServer(t, testOperatorToken)
			done := make(chan struct{})
			srv := &http.Server{
				Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					defer close(done)
					h.ServeHTTP(w, r)
				}),
				ReadHeaderTimeout: 5 * time.Second,
			}
			ln, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			go func() { _ = srv.Serve(ln) }()
			t.Cleanup(func() { _ = srv.Close() })
			port := ln.Addr().(*net.TCPAddr).Port
			api.Port = port

			conn, err := net.Dial("tcp", ln.Addr().String())
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			if _, err := io.WriteString(conn, strings.Replace(tt.request, "%d", strconv.Itoa(port), 1)); err != nil {
				t.Fatal(err)
			}
			if err := conn.SetReadDeadline(time.Now().Add(2 * time.Second)); err != nil {
				t.Fatal(err)
			}
			raw, err := io.ReadAll(conn)
			if err != nil {
				t.Fatalf("read response: %v\n%s", err, raw)
			}
			if bytes.Contains(raw, []byte("operator-token")) {
				t.Fatal("response named the token file")
			}
			gotStatus, gotHeader, gotBody := parsePythonHTTP(t, raw)
			if tt.golden != nil {
				assertRawHTTP(t, gotStatus, gotHeader, gotBody, tt.golden)
			} else if gotStatus != "HTTP/1.1 405 Method Not Allowed" {
				t.Fatalf("status %s body %s", gotStatus, gotBody)
			}
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Fatal("handler did not return")
			}
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			if err := srv.Shutdown(ctx); err != nil {
				t.Fatal(err)
			}
		})
	}
}

// TestHijackedRefusalUnblocks is a client that never reads a large reflected
// Origin. net.Pipe does not buffer, so the refusal write blocks until a
// deadline or until shutdown closes the hijacked socket. The handler returns
// either way. A kernel socket can absorb this body and hide the stall.
func TestHijackedRefusalUnblocks(t *testing.T) {
	origin := "http://evil.invalid/" + strings.Repeat("a", 64*1024)
	request := "GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: " + origin + "\r\n\r\n"
	tests := []struct {
		name    string
		timeout time.Duration
		stop    string
	}{
		{name: "write deadline", timeout: 200 * time.Millisecond},
		{name: "shutdown", timeout: time.Hour, stop: "shutdown"},
		{name: "close", timeout: time.Hour, stop: "close"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			api := New("test", nil, "")
			api.refusalWriteTimeout = tt.timeout
			h := api.Handler()
			done := make(chan struct{})
			srv := &http.Server{
				Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					defer close(done)
					h.ServeHTTP(w, r)
				}),
				ReadHeaderTimeout: 5 * time.Second,
			}
			serverConn, client := net.Pipe()
			defer client.Close()
			defer serverConn.Close()
			ln := &pipeExportListener{conn: serverConn, closed: make(chan struct{})}
			binder, ok := h.(ShutdownBinder)
			if !ok {
				t.Fatal("handler is not tied to server shutdown")
			}
			bound := binder.BindShutdown(srv, ln)
			go func() { _ = srv.Serve(bound) }()
			t.Cleanup(func() { _ = srv.Close() })

			if err := client.SetWriteDeadline(time.Now().Add(2 * time.Second)); err != nil {
				t.Fatal(err)
			}
			if _, err := io.WriteString(client, request); err != nil {
				t.Fatal(err)
			}

			start := time.Now()
			if tt.stop != "" {
				select {
				case <-done:
					t.Fatal("write finished before shutdown; the peer is not stalled")
				case <-time.After(150 * time.Millisecond):
				}
				var stopErr error
				switch tt.stop {
				case "shutdown":
					ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
					stopErr = srv.Shutdown(ctx)
					cancel()
				case "close":
					stopErr = srv.Close()
				}
				if stopErr != nil {
					t.Fatal(stopErr)
				}
			}
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Fatal("handler still blocked")
			}
			elapsed := time.Since(start)
			if tt.stop == "" && (elapsed < 100*time.Millisecond || elapsed > 2*time.Second) {
				t.Fatalf("write returned after %s, want the 200ms deadline", elapsed)
			}
			if tt.stop != "" && elapsed > 2*time.Second {
				t.Fatalf("shutdown left the write blocked for %s", elapsed)
			}
		})
	}
}

func readOperatorGolden(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(operatorAuthFixtures, name))
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
