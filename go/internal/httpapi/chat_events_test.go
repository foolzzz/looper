package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/foolzzz/looper/go/internal/agents/web"
	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/pyjson"
)

func TestChatSaveStatuses(t *testing.T) {
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = &recordingReads{}
	h := srv.Handler()
	logs := captureLogs(t)

	cases := []struct {
		name   string
		body   string
		status int
		want   string
	}{
		{name: "empty", body: "", status: http.StatusBadRequest, want: `{"error": "invalid JSON"}`},
		{name: "partial", body: "{", status: http.StatusBadRequest, want: `{"error": "invalid JSON"}`},
		{name: "array", body: "[]", status: http.StatusInternalServerError, want: `{"error": "internal server error"}`},
		{name: "null", body: "null", status: http.StatusInternalServerError, want: `{"error": "internal server error"}`},
		{name: "string", body: `"hi"`, status: http.StatusInternalServerError, want: `{"error": "internal server error"}`},
		{name: "number", body: "1", status: http.StatusInternalServerError, want: `{"error": "internal server error"}`},
		{name: "bool", body: "true", status: http.StatusInternalServerError, want: `{"error": "internal server error"}`},
		{name: "object", body: "{}", status: http.StatusBadRequest, want: `{"error": "session_id, message_id, role required"}`},
		{name: "empty string", body: `{"session_id":"","message_id":"m","role":"user"}`, status: http.StatusBadRequest, want: `{"error": "session_id, message_id, role required"}`},
		{name: "null field", body: `{"session_id":null,"message_id":"m","role":"user"}`, status: http.StatusBadRequest, want: `{"error": "session_id, message_id, role required"}`},
		{name: "zero", body: `{"session_id":0,"message_id":"m","role":"user"}`, status: http.StatusBadRequest, want: `{"error": "session_id, message_id, role required"}`},
		{name: "false", body: `{"session_id":false,"message_id":"m","role":"user"}`, status: http.StatusBadRequest, want: `{"error": "session_id, message_id, role required"}`},
		{name: "empty array", body: `{"session_id":[],"message_id":"m","role":"user"}`, status: http.StatusBadRequest, want: `{"error": "session_id, message_id, role required"}`},
		{name: "empty object", body: `{"session_id":{},"message_id":"m","role":"user"}`, status: http.StatusBadRequest, want: `{"error": "session_id, message_id, role required"}`},
		{name: "whitespace", body: `{"session_id":" ","message_id":"m","role":"user","content":"SECRET-CONTENT"}`, status: http.StatusOK, want: `{"ok": true}`},
		{name: "number field", body: `{"session_id":1,"message_id":"m","role":"user"}`, status: http.StatusOK, want: `{"ok": true}`},
		{name: "null content", body: `{"session_id":"s","message_id":"m","role":"user","content":null}`, status: http.StatusOK, want: `{"ok": true}`},
		{name: "ok", body: `{"session_id":"s","message_id":"m","role":"user","content":"SECRET-CONTENT"}`, status: http.StatusOK, want: `{"ok": true}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, chatJSONRequest(tc.body))
			if rr.Code != tc.status || rr.Body.String() != tc.want {
				t.Fatalf("%d %s", rr.Code, rr.Body.String())
			}
			if tc.status == http.StatusOK && rr.Header().Get("Content-Type") != "application/json; charset=utf-8" {
				t.Fatalf("type %s", rr.Header().Get("Content-Type"))
			}
		})
	}

	raw := httptest.NewRequest(http.MethodPost, "/api/chat/messages", bytes.NewReader([]byte{0xff}))
	raw.Host = "127.0.0.1"
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, raw)
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error": "invalid JSON"}` {
		t.Fatalf("utf8 %d %s", rr.Code, rr.Body.String())
	}

	// encoding/json accepts this and replaces the byte with U+FFFD.
	// Python's body.decode("utf-8") raises before json.loads.
	inside := []byte(`{"session_id":"s","message_id":"m","role":"user","content":"`)
	inside = append(inside, 0xff)
	inside = append(inside, []byte(`"}`)...)
	if !json.Valid(inside) || utf8.Valid(inside) {
		t.Fatalf("fixture valid json=%v utf8=%v", json.Valid(inside), utf8.Valid(inside))
	}
	insideReq := httptest.NewRequest(http.MethodPost, "/api/chat/messages", bytes.NewReader(inside))
	insideReq.Host = "127.0.0.1"
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, insideReq)
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error": "invalid JSON"}` {
		t.Fatalf("utf8 in string %d %s", rr.Code, rr.Body.String())
	}

	chunked := httptest.NewRequest(http.MethodPost, "/api/chat/messages", strings.NewReader(`{"session_id":"s","message_id":"m","role":"user"}`))
	chunked.Host = "127.0.0.1"
	chunked.ContentLength = -1
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, chunked)
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error": "invalid JSON"}` {
		t.Fatalf("chunked %d %s", rr.Code, rr.Body.String())
	}

	tooBig := httptest.NewRequest(http.MethodPost, "/api/chat/messages", nil)
	tooBig.Host = "127.0.0.1"
	tooBig.ContentLength = requestBodyMaxBytes + 1
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, tooBig)
	want413 := `{"error": "the request body has 8388609 bytes; the server takes at most 8 MiB (8388608 bytes)"}`
	if rr.Code != http.StatusRequestEntityTooLarge || rr.Body.String() != want413 {
		t.Fatalf("413 %d %s", rr.Code, rr.Body.String())
	}
	if strings.Contains(logs.String(), "SECRET-CONTENT") || strings.Contains(logs.String(), "postgres:postgres@") {
		t.Fatalf("log leaked %q", logs.String())
	}

	for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodHead} {
		rr = httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(method, "/api/chat/events?cid=abc"))
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` {
			t.Fatalf("%s events %d %s", method, rr.Code, rr.Body.String())
		}
	}
	queries := []string{"", "?cid=", "?session=", "?cid=&session="}
	for _, query := range queries {
		rr = httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/events"+query))
		if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error": "cid or session is required"}` {
			t.Fatalf("%s %d %s", query, rr.Code, rr.Body.String())
		}
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/events?cid=abc"))
	if rr.Code != http.StatusInternalServerError {
		t.Fatalf("recorder hijack %d %s", rr.Code, rr.Body.String())
	}
}

func TestChatSaveReadback(t *testing.T) {
	dsn := withHTTPDisposableDB(t, "loopgo_chat_http")
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	if err := db.EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	sqlDB, err := db.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	store := db.NewStore(sqlDB)
	adapter := web.New()
	adapter.SetSaver(store)
	srv := New("dev", db.Probe{DB: sqlDB}, "")
	srv.Reads = store
	srv.Web = adapter
	h := srv.Handler()
	logs := captureLogs(t)

	post := func(body string) {
		t.Helper()
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, chatJSONRequest(body))
		if rr.Code != http.StatusOK || rr.Body.String() != `{"ok": true}` {
			t.Fatalf("save %d %s", rr.Code, rr.Body.String())
		}
	}
	post(`{"session_id":"sess-1","message_id":"m1","role":"user","content":"hello 你好"}`)
	messages := getMessages(t, h, "sess-1")
	if len(messages) != 1 {
		t.Fatalf("messages %#v", messages)
	}
	row := messages[0]
	if row["message_id"] != "m1" || row["role"] != "user" || row["content"] != "hello 你好" || row["channel"] != "web" {
		t.Fatalf("row %#v", row)
	}
	if row["delivery_failure"] != nil || row["delivery_error"] != nil {
		t.Fatalf("delivery %#v", row)
	}
	created, _ := row["created_at"].(string)
	if !strings.Contains(created, "+00:00") {
		t.Fatalf("created %v", row["created_at"])
	}
	post(`{"session_id":"sess-1","message_id":"m1","role":"assistant","content":"updated"}`)
	messages = getMessages(t, h, "sess-1")
	if len(messages) != 1 || messages[0]["role"] != "user" || messages[0]["content"] != "updated" || messages[0]["channel"] != "web" {
		t.Fatalf("upsert %#v", messages)
	}
	if messages[0]["created_at"] != created {
		t.Fatalf("created %v want %s", messages[0]["created_at"], created)
	}

	post(`{"session_id":1,"message_id":"m","role":"user","content":"SECRET-CONTENT"}`)
	post(`{"session_id":"sess-1","message_id":"null-content","role":"user","content":null}`)
	messages = getMessages(t, h, "sess-1")
	if len(messages) != 1 || messages[0]["message_id"] != "m1" {
		t.Fatalf("non string stored %#v", messages)
	}

	adapter.Track("corr-1", "sess-1", "m-user")
	adapter.CloseSSE("corr-1")
	receipt := adapter.Deliver(ctx, web.Envelope{
		CorrelationID: "corr-1", MessageType: web.AgentReply,
		Payload: map[string]any{"text": "the answer"},
	})
	if !receipt.Success {
		t.Fatal(receipt.Error)
	}
	adapter.Deliver(ctx, web.Envelope{
		MessageID: "bg-1", MessageType: web.AgentReply, Timestamp: "2026-01-02T03:04:05+00:00",
		Payload: map[string]any{"text": "on its own", "chat_session_id": "sess-1"},
	})
	messages = getMessages(t, h, "sess-1")
	if len(messages) != 3 {
		t.Fatalf("deliver rows %d %#v", len(messages), messages)
	}
	if messages[1]["message_id"] != "m-user:reply" || messages[1]["content"] != "the answer" || messages[1]["role"] != "assistant" {
		t.Fatalf("reply %#v", messages[1])
	}
	if messages[2]["message_id"] != "bg-1" || messages[2]["content"] != "on its own" {
		t.Fatalf("autonomous %#v", messages[2])
	}

	gone, stop := context.WithCancel(context.Background())
	stop()
	req := chatJSONRequest(`{"session_id":"sess-1","message_id":"m-cancel","role":"user","content":"nope"}`)
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, req.WithContext(gone))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"ok": true}` {
		t.Fatalf("cancel %d %s", rr.Code, rr.Body.String())
	}
	messages = getMessages(t, h, "sess-1")
	if len(messages) != 3 {
		t.Fatalf("cancel stored %#v", messages)
	}
	if strings.Contains(logs.String(), "SECRET-CONTENT") || strings.Contains(logs.String(), "postgres:postgres@") {
		t.Fatalf("log %q", logs.String())
	}

	status := httptest.NewRecorder()
	h.ServeHTTP(status, loopbackRequest(http.MethodGet, "/api/status"))
	if status.Code != http.StatusOK || !strings.Contains(status.Body.String(), `"active_sse_streams": 0`) {
		t.Fatalf("status %s", status.Body.String())
	}
}

func TestChatEventSockets(t *testing.T) {
	unknown := readSSEGolden(t, "events-unknown.http")
	cidGolden := readSSEGolden(t, "events-cid.sse")
	sessionGolden := readSSEGolden(t, "events-session.sse")
	cidHeader, _ := splitSSE(t, cidGolden)
	sessionHeader, sessionBody := splitSSE(t, sessionGolden)

	adapter := web.New()
	saver := &socketSaver{}
	adapter.SetSaver(saver)
	srv := New("dev", nil, "")
	srv.Web = adapter
	_, addr := serveSSE(t, srv.Handler())
	text := "你好 \"quote\"\nnext"

	t.Run("unknown", func(t *testing.T) {
		conn := dialSSE(t, addr, "cid=missing")
		got := readUntilClose(t, conn)
		if !bytes.Equal(got, unknown) {
			t.Fatalf("unknown\n got %q\nwant %q", got, unknown)
		}
	})

	t.Run("cid order", func(t *testing.T) {
		adapter.Track("cid-1", "", "")
		adapter.Deliver(context.Background(), web.Envelope{
			CorrelationID: "cid-1", MessageType: web.StatusUpdate,
			Payload: map[string]any{"text": text, "delta": true},
		})
		adapter.Deliver(context.Background(), web.Envelope{
			CorrelationID: "cid-1", MessageType: web.AgentReply,
			Payload: map[string]any{"text": text},
		})
		conn := dialSSE(t, addr, "cid=cid-1")
		got := readUntilClose(t, conn)
		if !bytes.Equal(got, cidGolden) {
			t.Fatalf("cid\n got %q\nwant %q", got, cidGolden)
		}
		if adapter.HasStream("cid-1") {
			t.Fatal("stream left open")
		}
		again := dialSSE(t, addr, "cid=cid-1")
		if second := readUntilClose(t, again); !bytes.Equal(second, unknown) {
			t.Fatalf("second %q", second)
		}
	})

	t.Run("cid wins over session", func(t *testing.T) {
		adapter.Track("known", "", "")
		defer adapter.CloseSSE("known")
		conn := dialSSE(t, addr, "cid=known&session=other")
		header := readSSEHeaders(t, conn)
		if !bytes.Equal([]byte(header), cidHeader) {
			t.Fatalf("header %q", header)
		}
		if strings.Contains(header, "X-Accel-Buffering") {
			t.Fatal("session header on a cid stream")
		}
		closeNow(conn)
		waitUntil(t, func() bool { return !adapter.HasStream("known") })
	})

	t.Run("blank cid uses session", func(t *testing.T) {
		conn := dialSSE(t, addr, "cid=&session=chat-blank")
		header := readSSEHeaders(t, conn)
		if !bytes.Equal([]byte(header), sessionHeader) {
			t.Fatalf("header %q", header)
		}
		closeNow(conn)
		waitUntil(t, func() bool { return adapter.ChatSubscribers("chat-blank") == 0 })
	})

	t.Run("session frame", func(t *testing.T) {
		conn := dialSSE(t, addr, "session=chat-1")
		header := readSSEHeaders(t, conn)
		if !bytes.Equal([]byte(header), sessionHeader) {
			t.Fatalf("header %q", header)
		}
		waitUntil(t, func() bool { return adapter.ChatSubscribers("chat-1") == 1 })
		adapter.Deliver(context.Background(), web.Envelope{
			MessageID: "m-1", Timestamp: "2026-01-02T03:04:05.000123+00:00",
			MessageType: web.AgentReply,
			Payload:     map[string]any{"text": "背景完成", "chat_session_id": "chat-1"},
		})
		body := readN(t, conn, len(sessionBody))
		if !bytes.Equal(body, sessionBody) {
			t.Fatalf("body %q", body)
		}
		if len(saver.rows) != 1 || saver.rows[0].content != "背景完成" || saver.rows[0].message != "m-1" {
			t.Fatalf("saved %+v", saver.rows)
		}
		closeNow(conn)
		waitUntil(t, func() bool { return adapter.ChatSubscribers("chat-1") == 0 })
	})

	t.Run("two subscribers and close", func(t *testing.T) {
		first := dialSSE(t, addr, "session=chat-many")
		second := dialSSE(t, addr, "session=chat-many")
		if h := readSSEHeaders(t, first); !bytes.Equal([]byte(h), sessionHeader) {
			t.Fatalf("first %q", h)
		}
		if h := readSSEHeaders(t, second); !bytes.Equal([]byte(h), sessionHeader) {
			t.Fatalf("second %q", h)
		}
		waitUntil(t, func() bool { return adapter.ChatSubscribers("chat-many") == 2 })
		adapter.Deliver(context.Background(), web.Envelope{
			MessageID: "m-a", Timestamp: "ts-a", MessageType: web.AgentReply,
			Payload: map[string]any{"text": "same", "chat_session_id": "chat-many"},
		})
		want, err := web.Format(chatEvent("m-a", "same", "ts-a"))
		if err != nil {
			t.Fatal(err)
		}
		left := readN(t, first, len(want))
		right := readN(t, second, len(want))
		if !bytes.Equal(left, right) || !bytes.Equal(left, want) {
			t.Fatalf("left %q right %q want %q", left, right, want)
		}
		closeNow(first)
		waitUntil(t, func() bool { return adapter.ChatSubscribers("chat-many") == 1 })
		adapter.Deliver(context.Background(), web.Envelope{
			MessageID: "m-b", Timestamp: "ts-b", MessageType: web.AgentReply,
			Payload: map[string]any{"text": "only-open", "chat_session_id": "chat-many"},
		})
		next, err := web.Format(chatEvent("m-b", "only-open", "ts-b"))
		if err != nil {
			t.Fatal(err)
		}
		if got := readN(t, second, len(next)); !bytes.Equal(got, next) {
			t.Fatalf("remaining %q", got)
		}
		closeNow(second)
		waitUntil(t, func() bool { return adapter.ChatSubscribers("chat-many") == 0 })
	})

	t.Run("message stream close", func(t *testing.T) {
		adapter.Track("cid-close", "sess", "m")
		conn := dialSSE(t, addr, "cid=cid-close")
		if h := readSSEHeaders(t, conn); !bytes.Equal([]byte(h), cidHeader) {
			t.Fatalf("header %q", h)
		}
		if !adapter.HasStream("cid-close") {
			t.Fatal("stream missing before close")
		}
		closeNow(conn)
		waitUntil(t, func() bool { return !adapter.HasStream("cid-close") })
		if adapter.AwaitingCount() != 1 {
			t.Fatalf("awaiting %d", adapter.AwaitingCount())
		}
	})
}

func chatEvent(id, content, created string) web.Event {
	return web.Event{Kind: "chat.message", Payload: pyjson.Object{
		{Key: "message_id", Value: id},
		{Key: "role", Value: "assistant"},
		{Key: "content", Value: content},
		{Key: "created_at", Value: created},
	}}
}

func chatJSONRequest(body string) *http.Request {
	req := httptest.NewRequest(http.MethodPost, "/api/chat/messages", strings.NewReader(body))
	req.Host = "127.0.0.1"
	return req
}

func getMessages(t *testing.T, h http.Handler, sessionID string) []map[string]any {
	t.Helper()
	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/messages?session_id="+sessionID))
	if rr.Code != http.StatusOK {
		t.Fatalf("get %d %s", rr.Code, rr.Body.String())
	}
	var body struct {
		Messages []map[string]any `json:"messages"`
	}
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	return body.Messages
}

func readSSEGolden(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", "sse", name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func splitSSE(t *testing.T, raw []byte) (header, body []byte) {
	t.Helper()
	i := bytes.Index(raw, []byte("\r\n\r\n"))
	if i < 0 {
		t.Fatalf("no header end in %q", raw)
	}
	return raw[:i+4], raw[i+4:]
}

func serveSSE(t *testing.T, h http.Handler) (*http.Server, string) {
	t.Helper()
	var ln net.Listener
	var err error
	for port := 18340; port < 18440; port++ {
		ln, err = net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(port))
		if err == nil {
			break
		}
	}
	if ln == nil {
		t.Fatalf("listen: %v", err)
	}
	server := &http.Server{Handler: h, ReadHeaderTimeout: 5 * time.Second}
	binder, ok := h.(ShutdownBinder)
	if !ok {
		t.Fatal("chat handler is not tied to server shutdown")
	}
	ln = binder.BindShutdown(server, ln)
	go func() { _ = server.Serve(ln) }()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = server.Shutdown(ctx)
		_ = ln.Close()
	})
	return server, ln.Addr().String()
}

func TestChatSaveSlowBody(t *testing.T) {
	srv := New("dev", nil, "")
	srv.bodyReadTimeout = 200 * time.Millisecond
	_, addr := serveSSE(t, srv.Handler())
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	if _, err := io.WriteString(conn, "POST /api/chat/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 64\r\n\r\n{"); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf, err := io.ReadAll(conn)
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("read after %s: %v", elapsed, err)
	}
	if elapsed < 100*time.Millisecond || elapsed > time.Second {
		t.Fatalf("body read ended after %s, want the 200ms deadline", elapsed)
	}
	if len(buf) != 0 {
		t.Fatalf("response %q, want the socket closed with no status", buf)
	}
}

func TestChatEventShutdown(t *testing.T) {
	sessionGolden := readSSEGolden(t, "events-session.sse")
	cidGolden := readSSEGolden(t, "events-cid.sse")
	sessionHeader, _ := splitSSE(t, sessionGolden)
	cidHeader, _ := splitSSE(t, cidGolden)
	stops := []struct {
		name string
		stop func(*http.Server) error
	}{
		{name: "shutdown", stop: func(server *http.Server) error {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			return server.Shutdown(ctx)
		}},
		{name: "close", stop: func(server *http.Server) error {
			return server.Close()
		}},
	}
	for _, tc := range stops {
		t.Run(tc.name, func(t *testing.T) {
			adapter := web.New()
			srv := New("dev", nil, "")
			srv.Web = adapter
			server, addr := serveSSE(t, srv.Handler())
			adapter.Track("cid-shut", "sess", "m-shut")
			sessionConn := dialSSE(t, addr, "session=shut-session")
			cidConn := dialSSE(t, addr, "cid=cid-shut")
			if h := readSSEHeaders(t, sessionConn); !bytes.Equal([]byte(h), sessionHeader) {
				t.Fatalf("session header %q", h)
			}
			if h := readSSEHeaders(t, cidConn); !bytes.Equal([]byte(h), cidHeader) {
				t.Fatalf("cid header %q", h)
			}
			waitUntil(t, func() bool {
				return adapter.ChatSubscribers("shut-session") == 1 && adapter.HasStream("cid-shut")
			})
			if err := tc.stop(server); err != nil {
				t.Fatal(err)
			}
			if rest := readUntilClose(t, sessionConn); len(rest) != 0 {
				t.Fatalf("session tail %q", rest)
			}
			if rest := readUntilClose(t, cidConn); len(rest) != 0 {
				t.Fatalf("cid tail %q", rest)
			}
			waitUntil(t, func() bool {
				return adapter.ChatSubscribers("shut-session") == 0 && !adapter.HasStream("cid-shut")
			})
		})
	}
}

func dialSSE(t *testing.T, addr, rawQuery string) net.Conn {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	if _, err := io.WriteString(conn, "GET /api/chat/events?"+rawQuery+" HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	return conn
}

func readSSEHeaders(t *testing.T, conn net.Conn) string {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	var buf []byte
	tmp := make([]byte, 1)
	for !bytes.Contains(buf, []byte("\r\n\r\n")) {
		if _, err := conn.Read(tmp); err != nil {
			t.Fatalf("headers %q: %v", buf, err)
		}
		buf = append(buf, tmp[0])
		if len(buf) > 2048 {
			t.Fatalf("headers %q", buf)
		}
	}
	return string(buf)
}

func readN(t *testing.T, conn net.Conn, n int) []byte {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf := make([]byte, n)
	if _, err := io.ReadFull(conn, buf); err != nil {
		t.Fatalf("read %d: %v", n, err)
	}
	return buf
}

func readUntilClose(t *testing.T, conn net.Conn) []byte {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	buf, err := io.ReadAll(conn)
	if err != nil {
		t.Fatal(err)
	}
	return buf
}

func closeNow(conn net.Conn) {
	if tcp, ok := conn.(*net.TCPConn); ok {
		_ = tcp.SetLinger(0)
	}
	_ = conn.Close()
}

func waitUntil(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("timed out waiting")
}

type socketSaver struct {
	rows []savedChat
}

type savedChat struct {
	session string
	message string
	role    string
	content string
}

func (s *socketSaver) SaveChatMessage(_ context.Context, sessionID, messageID, role, content string) error {
	s.rows = append(s.rows, savedChat{sessionID, messageID, role, content})
	return nil
}
