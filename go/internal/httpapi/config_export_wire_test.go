package httpapi

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/textproto"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/configtransfer"
)

// pipeExportListener runs the real net/http connection writer without binding
// a network port. Only Serve's Accept loop reads the pending connection.
type pipeExportListener struct {
	conn   net.Conn
	closed chan struct{}
	once   sync.Once
}

func (l *pipeExportListener) Accept() (net.Conn, error) {
	if l.conn != nil {
		conn := l.conn
		l.conn = nil
		return conn, nil
	}
	<-l.closed
	return nil, net.ErrClosed
}

func (l *pipeExportListener) Close() error {
	l.once.Do(func() { close(l.closed) })
	return nil
}

func (*pipeExportListener) Addr() net.Addr {
	return &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 18001}
}

func TestConfigExportStalledReader(t *testing.T) {
	for _, tt := range []struct {
		name string
		size int
	}{
		{"flush", 2},
		{"write", 64 * 1024},
	} {
		t.Run(tt.name, func(t *testing.T) {
			exportDone := make(chan error, 1)
			handlerDone := make(chan struct{})
			reads := &exportReads{export: func(_ context.Context, out io.Writer, _ configtransfer.Options) error {
				_, err := io.WriteString(out, strings.Repeat("x", tt.size))
				exportDone <- err
				return err
			}}
			handler := (&Server{Reads: reads}).Handler()
			serverConn, client := net.Pipe()
			defer client.Close()
			defer serverConn.Close()
			listener := &pipeExportListener{conn: serverConn, closed: make(chan struct{})}
			server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				defer close(handlerDone)
				// Keep the production deadline path, with a shorter request budget.
				ctx, cancel := context.WithTimeout(r.Context(), 100*time.Millisecond)
				defer cancel()
				handler.ServeHTTP(w, r.WithContext(ctx))
			})}
			defer server.Close()
			serveDone := make(chan error, 1)
			go func() { serveDone <- server.Serve(listener) }()
			if err := client.SetWriteDeadline(time.Now().Add(time.Second)); err != nil {
				t.Fatal(err)
			}
			if _, err := io.WriteString(client, "GET /api/config/export HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"); err != nil {
				t.Fatal(err)
			}
			// Never read or close the peer: only the response deadline can
			// release the blocked export and let its snapshot cleanup run.
			select {
			case err := <-exportDone:
				var timeout net.Error
				if !errors.As(err, &timeout) || !timeout.Timeout() {
					t.Fatalf("export error = %v, want socket timeout", err)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("stalled reader retained the export")
			}
			select {
			case <-handlerDone:
			case <-time.After(time.Second):
				t.Fatal("export handler did not return")
			}
			if err := server.Close(); err != nil {
				t.Fatal(err)
			}
			if err := <-serveDone; !errors.Is(err, http.ErrServerClosed) {
				t.Fatalf("serve: %v", err)
			}
		})
	}
}

func TestConfigExportWireFraming(t *testing.T) {
	for _, partial := range []bool{false, true} {
		t.Run(map[bool]string{false: "complete", true: "interrupted"}[partial], func(t *testing.T) {
			reads := &exportReads{export: func(_ context.Context, out io.Writer, _ configtransfer.Options) error {
				for _, piece := range []string{"{\n", `"value":"公开"`} {
					if _, err := io.WriteString(out, piece); err != nil {
						return err
					}
				}
				if partial {
					return errors.New("synthetic interrupted export")
				}
				_, err := io.WriteString(out, "}\n")
				return err
			}}
			now := time.Date(2026, 10, 19, 12, 0, 0, 0, time.UTC)
			handler := (&Server{Reads: reads, Now: func() time.Time { return now }}).Handler()
			serverConn, client := net.Pipe()
			defer client.Close()
			defer serverConn.Close()
			if err := client.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
				t.Fatal(err)
			}
			listener := &pipeExportListener{conn: serverConn, closed: make(chan struct{})}
			server := &http.Server{Handler: handler, ReadHeaderTimeout: time.Second}
			defer server.Close()
			finished := make(chan error, 1)
			go func() { finished <- server.Serve(listener) }()
			if _, err := io.WriteString(client, "GET /api/config/export HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"); err != nil {
				t.Fatal(err)
			}
			raw, err := io.ReadAll(client)
			if err != nil {
				t.Fatalf("read response through connection EOF: %v", err)
			}
			if err := server.Close(); err != nil {
				t.Fatal(err)
			}
			if err := <-finished; !errors.Is(err, http.ErrServerClosed) {
				t.Fatalf("serve: %v", err)
			}
			reader := bufio.NewReader(strings.NewReader(string(raw)))
			status, err := reader.ReadString('\n')
			if err != nil || status != "HTTP/1.1 200 OK\r\n" {
				t.Fatalf("status %q: %v", status, err)
			}
			headers, err := textproto.NewReader(reader).ReadMIMEHeader()
			if err != nil {
				t.Fatal(err)
			}
			for key, want := range map[string]string{
				"Content-Type":        "application/json; charset=utf-8",
				"Content-Disposition": `attachment; filename="looper-config-2026-10-19.json"`,
				"Cache-Control":       "no-store",
				"Connection":          "close",
				"Content-Length":      "",
				"Transfer-Encoding":   "",
				"Date":                "",
			} {
				if got := headers.Get(key); got != want {
					t.Errorf("wire %s = %q, want %q", key, got, want)
				}
			}
			if len(headers) != 4 {
				t.Errorf("wire headers %v, want exactly the four Python download headers", headers)
			}
			body, err := io.ReadAll(reader)
			want := "{\n\"value\":\"公开\""
			if !partial {
				want += "}\n"
			}
			if err != nil || string(body) != want {
				t.Fatalf("wire body %q: %v, want %q", body, err, want)
			}
		})
	}
}
