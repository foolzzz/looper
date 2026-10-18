package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestRun(t *testing.T) {
	tests := []struct {
		name     string
		args     []string
		wantCode int
		wantOut  string
		wantErr  string
	}{
		{name: "version", args: []string{"version"}, wantCode: 0, wantOut: "looper-go dev\n"},
		{name: "version extra", args: []string{"version", "now"}, wantCode: 2, wantErr: "unexpected argument"},
		{name: "no args", args: nil, wantCode: 2, wantErr: "usage:"},
		{name: "unknown", args: []string{"doctor"}, wantCode: 2, wantErr: "unknown command"},
		{name: "help", args: []string{"--help"}, wantCode: 0, wantOut: "usage:"},
		{name: "serve help", args: []string{"serve", "--help"}, wantCode: 0, wantOut: "--port"},
		{name: "missing port", args: []string{"serve", "--db-url", "postgres://localhost/db"}, wantCode: 2, wantErr: "--port is required"},
		{name: "missing db", args: []string{"serve", "--port", "80"}, wantCode: 2, wantErr: "--db-url is required"},
		{name: "bad port", args: []string{"serve", "--port", "nope", "--db-url", "postgres://localhost/db"}, wantCode: 2, wantErr: "not a TCP port"},
		{name: "missing web dir", args: []string{"serve", "--port", "9", "--db-url", "postgres://localhost/db", "--web-dir", "/no/such/loopgo-web-dir"}, wantCode: 2, wantErr: "--web-dir"},
		{name: "web dir is file", args: []string{"serve", "--port", "9", "--db-url", "postgres://localhost/db", "--web-dir", "/dev/null"}, wantCode: 2, wantErr: "not a directory"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			code := run(tt.args, &stdout, &stderr)
			if code != tt.wantCode {
				t.Fatalf("code %d, want %d\nstdout %s\nstderr %s", code, tt.wantCode, stdout.String(), stderr.String())
			}
			if tt.wantOut != "" && !strings.Contains(stdout.String(), tt.wantOut) {
				t.Fatalf("stdout %q, want substring %q", stdout.String(), tt.wantOut)
			}
			if tt.wantErr != "" && !strings.Contains(stderr.String(), tt.wantErr) {
				t.Fatalf("stderr %q, want substring %q", stderr.String(), tt.wantErr)
			}
		})
	}
}

func TestVersionLdflags(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "looper")
	cmd := exec.Command("go", "build", "-o", bin, "-ldflags=-X=main.version=0.0.1", ".")
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0", "GOTOOLCHAIN=local")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	got, err := exec.Command(bin, "version").Output()
	if err != nil {
		t.Fatalf("version: %v", err)
	}
	if string(got) != "looper-go 0.0.1\n" {
		t.Fatalf("stdout %q", got)
	}
}

func TestVersionFromBuild(t *testing.T) {
	old := version
	version = "1.2.3"
	t.Cleanup(func() { version = old })
	var stdout, stderr bytes.Buffer
	if code := run([]string{"version"}, &stdout, &stderr); code != 0 {
		t.Fatalf("code %d stderr %s", code, stderr.String())
	}
	if stdout.String() != "looper-go 1.2.3\n" {
		t.Fatalf("stdout %q", stdout.String())
	}
}

func TestRunPortInUse(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	port := ln.Addr().(*net.TCPAddr).Port
	var stdout, stderr bytes.Buffer
	code := run([]string{
		"serve",
		"--port", strconv.Itoa(port),
		"--db-url", "postgres://postgres:s3cr3t-not-in-logs@127.0.0.1:1/none",
	}, &stdout, &stderr)
	if code != 1 {
		t.Fatalf("code %d stderr %s", code, stderr.String())
	}
	if !strings.Contains(stderr.String(), "serve failed") {
		t.Fatalf("stderr %s", stderr.String())
	}
	if strings.Contains(stderr.String(), "s3cr3t-not-in-logs") {
		t.Fatalf("stderr leaked the database password: %s", stderr.String())
	}
}

func TestServeShutsDown(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	if err := ln.Close(); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- serveContext(ctx, []string{
			"--port", strconv.Itoa(port),
			"--db-url", "postgres://postgres:s3cr3t-not-in-logs@127.0.0.1:1/none?connect_timeout=1",
		}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	}()

	client := &http.Client{Timeout: 5 * time.Second}
	url := fmt.Sprintf("http://127.0.0.1:%d/api/status", port)
	var resp *http.Response
	deadline := time.Now().Add(3 * time.Second)
	for {
		resp, err = client.Get(url)
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("server did not start: %v", err)
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
	if strings.Contains(string(body), "s3cr3t-not-in-logs") {
		t.Fatal("body leaked the database password")
	}
	dbUnreachable := strings.Contains(string(body), `"db":"error: unreachable"`)
	dbTimeout := strings.Contains(string(body), `"db":"error: timeout"`)
	if !strings.Contains(string(body), `"implementation":"go"`) || (!dbUnreachable && !dbTimeout) {
		t.Fatalf("body %s", body)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("shutdown exceeded the limit")
	}
}

// closeListener reports when Serve's listener is closed, which is the first
// step of http.Server.Shutdown.
type closeListener struct {
	net.Listener
	closed chan struct{}
	once   sync.Once
}

func (l *closeListener) Close() error {
	err := l.Listener.Close()
	l.once.Do(func() { close(l.closed) })
	return err
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestServeDrainsInFlightRequest(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	closed := make(chan struct{})
	wln := &closeListener{Listener: ln, closed: closed}

	entered := make(chan struct{})
	release := make(chan struct{})
	var releaseOnce sync.Once
	t.Cleanup(func() { releaseOnce.Do(func() { close(release) }) })
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(entered)
		<-release
		if _, err := io.WriteString(w, "done"); err != nil {
			return
		}
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- serveListener(ctx, wln, handler, discardLogger(), 3*time.Second)
	}()

	resCh := make(chan string, 1)
	errCh := make(chan error, 1)
	go func() {
		url := "http://" + ln.Addr().String() + "/"
		deadline := time.Now().Add(3 * time.Second)
		var resp *http.Response
		var err error
		for {
			resp, err = http.Get(url)
			if err == nil || time.Now().After(deadline) {
				break
			}
			time.Sleep(10 * time.Millisecond)
		}
		if err != nil {
			errCh <- err
			return
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(resp.Body)
		if err != nil {
			errCh <- err
			return
		}
		if resp.StatusCode != http.StatusOK {
			errCh <- fmt.Errorf("status %d body %s", resp.StatusCode, body)
			return
		}
		resCh <- string(body)
	}()

	select {
	case <-entered:
	case err := <-errCh:
		t.Fatal(err)
	case <-time.After(3 * time.Second):
		t.Fatal("request did not reach the handler")
	}

	cancel()
	select {
	case <-closed:
	case err := <-done:
		t.Fatalf("shutdown returned before the listener closed: %v", err)
	case <-time.After(2 * time.Second):
		t.Fatal("listener was not closed")
	}
	select {
	case err := <-done:
		t.Fatalf("shutdown finished while the request was in flight: %v", err)
	default:
	}

	releaseOnce.Do(func() { close(release) })
	select {
	case body := <-resCh:
		if body != "done" {
			t.Fatalf("body %q", body)
		}
	case err := <-errCh:
		t.Fatal(err)
	case <-time.After(3 * time.Second):
		t.Fatal("in-flight request did not finish")
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown did not return after the request finished")
	}
}

func TestServeShutdownIsBounded(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	entered := make(chan struct{})
	block := make(chan struct{})
	var once sync.Once
	t.Cleanup(func() { once.Do(func() { close(block) }) })
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(entered)
		<-block
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	const limit = 200 * time.Millisecond
	go func() {
		done <- serveListener(ctx, ln, handler, discardLogger(), limit)
	}()
	go func() {
		url := "http://" + ln.Addr().String() + "/"
		deadline := time.Now().Add(3 * time.Second)
		for {
			resp, err := http.Get(url)
			if err == nil {
				resp.Body.Close()
				return
			}
			if time.Now().After(deadline) {
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	}()

	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("request did not reach the handler")
	}
	cancel()
	started := time.Now()
	select {
	case err := <-done:
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("error %v, want deadline exceeded", err)
		}
		if elapsed := time.Since(started); elapsed > 2*time.Second {
			t.Fatalf("shutdown took %s", elapsed)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("shutdown was not bounded")
	}
}
