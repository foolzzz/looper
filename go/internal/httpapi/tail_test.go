package httpapi

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"syscall"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
)

func TestChatAndActivityRoutes(t *testing.T) {
	reads := &recordingReads{
		chatTargets: []map[string]any{{"kind": "manager", "id": "manager", "can_send": true}},
		sessions: []map[string]any{{
			"session_id": "sess-web-1", "channel": "web", "message_count": int64(2),
		}},
		messages:     []map[string]any{{"message_id": "m1", "content": "hello"}},
		activity:     []map[string]any{{"seq": int64(1), "kind": "text"}},
		activityRow:  map[string]any{"seq": int64(4), "task_id": "task-rich00001", "detail": "full"},
		conversation: []map[string]any{{"seq": int64(1), "kind": "text"}},
	}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	h := srv.Handler()

	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/targets"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"targets":[{"can_send":true,"id":"manager","kind":"manager"}]}` {
		t.Fatalf("targets %d %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/sessions"))
	if rr.Code != http.StatusOK || !strings.Contains(rr.Body.String(), `"session_id":"sess-web-1"`) {
		t.Fatalf("sessions %d %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/messages?session_id=sess-lark-1&unused=1"))
	if rr.Code != http.StatusOK || reads.messageSession != "sess-lark-1" || !strings.Contains(rr.Body.String(), `"message_id":"m1"`) {
		t.Fatalf("messages %d session %q %s", rr.Code, reads.messageSession, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/messages?session_id=%20"))
	if rr.Code != http.StatusOK || reads.messageSession != " " {
		t.Fatalf("blank-looking session %d %q", rr.Code, reads.messageSession)
	}
	before := len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/messages"))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error": "session_id is required"}` || len(reads.calls) != before {
		t.Fatalf("missing session %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/messages?session_id="))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error": "session_id is required"}` {
		t.Fatalf("empty session %d %s", rr.Code, rr.Body.String())
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity?since=2&limit=1000"))
	if rr.Code != http.StatusOK || reads.activityTask != "task-rich00001" || reads.activitySince != 2 || reads.activityLimit != activityLimitMax {
		t.Fatalf("activity %d task %s since %d limit %d %s", rr.Code, reads.activityTask, reads.activitySince, reads.activityLimit, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity"))
	if reads.activitySince != 0 || reads.activityLimit != 100 {
		t.Fatalf("defaults since %d limit %d", reads.activitySince, reads.activityLimit)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity?since=nope"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "internal server error"}` || len(reads.calls) != before {
		t.Fatalf("bad since %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity?limit="+strings.Repeat("9", pythonIntMaxStrDigits+1)))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "internal server error"}` || len(reads.calls) != before {
		t.Fatalf("4301-digit limit %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity?since="+strings.Repeat("9", 40)))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"activity":[]}` || len(reads.calls) != before {
		t.Fatalf("overflow since %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity?limit="+strings.Repeat("9", pythonIntMaxStrDigits)))
	if rr.Code != http.StatusOK || reads.activityLimit != activityLimitMax {
		t.Fatalf("4300-digit limit %d passed %d", rr.Code, reads.activityLimit)
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-min000001/activity/4"))
	if rr.Code != http.StatusOK || reads.activitySeq != 4 || rr.Body.String() != `{"detail":"full","seq":4,"task_id":"task-rich00001"}` {
		t.Fatalf("detail %d seq %d %s", rr.Code, reads.activitySeq, rr.Body.String())
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity/"+strings.Repeat("9", 40)))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "activity not found"}` || len(reads.calls) != before {
		t.Fatalf("overflow seq %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity/"+strings.Repeat("9", pythonIntMaxStrDigits+1)))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "internal server error"}` || len(reads.calls) != before {
		t.Fatalf("4301-digit seq %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	for _, path := range []string{
		"/api/tasks/task-rich00001/activity/stream",
		"/api/tasks/nope/activity",
		"/api/tasks/task-rich00001/activity/1/extra",
	} {
		before = len(reads.calls)
		rr = httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` || len(reads.calls) != before {
			t.Fatalf("%s %d %s calls %d", path, rr.Code, rr.Body.String(), len(reads.calls)-before)
		}
	}

	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/conversation"))
	if rr.Code != http.StatusOK || reads.conversationTask != "task-rich00001" || !strings.Contains(rr.Body.String(), `"kind":"text"`) {
		t.Fatalf("conversation %d task %s %s", rr.Code, reads.conversationTask, rr.Body.String())
	}

	for _, path := range []string{
		"/api/chat/targets",
		"/api/chat/sessions",
		"/api/tasks/task-rich00001/activity",
		"/api/tasks/task-rich00001/activity/4",
		"/api/tasks/task-rich00001/conversation",
	} {
		for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodHead} {
			before = len(reads.calls)
			rr = httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(method, path))
			if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` || len(reads.calls) != before {
				t.Fatalf("%s %s %d %s calls %d", method, path, rr.Code, rr.Body.String(), len(reads.calls)-before)
			}
		}
	}
	for _, method := range []string{http.MethodPut, http.MethodHead} {
		before = len(reads.calls)
		rr = httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(method, "/api/chat/messages"))
		if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` || len(reads.calls) != before {
			t.Fatalf("%s messages %d %s calls %d", method, rr.Code, rr.Body.String(), len(reads.calls)-before)
		}
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodPost, "/api/chat/messages"))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error": "invalid JSON"}` || len(reads.calls) != before {
		t.Fatalf("POST messages %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
	before = len(reads.calls)
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/events"))
	if rr.Code != http.StatusBadRequest || rr.Body.String() != `{"error": "cid or session is required"}` || len(reads.calls) != before {
		t.Fatalf("events %d %s calls %d", rr.Code, rr.Body.String(), len(reads.calls)-before)
	}
}

func TestChatAndActivityErrors(t *testing.T) {
	secret := errors.New("dial SECRET")
	undefinedTable := fmt.Errorf("chat sessions: %w", &pgconn.PgError{
		Code:    "42P01",
		Message: `relation "chat_messages" does not exist`,
	})
	reads := &recordingReads{
		chatTargetsErr:  secret,
		sessionsErr:     undefinedTable,
		messagesErr:     secret,
		activityErr:     secret,
		activityRowErr:  secret,
		conversationErr: secret,
	}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	logs := captureLogs(t)
	h := srv.Handler()

	rr := httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/targets"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"targets":[{"can_send":true,"id":"manager","kind":"manager"}]}` {
		t.Fatalf("targets %d %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/sessions"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "the database could not be read (UndefinedTableError)"}` {
		t.Fatalf("sessions %d %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/chat/messages?session_id=sess-web-1"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"messages":[]}` {
		t.Fatalf("messages %d %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"activity":[]}` {
		t.Fatalf("activity %d %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/activity/1"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "activity not found"}` {
		t.Fatalf("detail %d %s", rr.Code, rr.Body.String())
	}
	rr = httptest.NewRecorder()
	h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/conversation"))
	if rr.Code != http.StatusOK || rr.Body.String() != `{"conversation":[]}` {
		t.Fatalf("conversation %d %s", rr.Code, rr.Body.String())
	}
	if strings.Contains(logs.String(), "SECRET") {
		t.Fatal("log leaked SECRET")
	}
}

func TestNilReadsForTailRoutes(t *testing.T) {
	h := New("dev", &fakeDB{}, "").Handler()
	checks := []struct {
		path string
		code int
		body string
	}{
		{"/api/chat/targets", http.StatusOK, `{"targets":[{"can_send":true,"id":"manager","kind":"manager"}]}`},
		{"/api/chat/sessions", http.StatusInternalServerError, `{"error": "the database could not be read (RuntimeError)"}`},
		{"/api/chat/messages?session_id=s", http.StatusOK, `{"messages":[]}`},
		{"/api/tasks/task-rich00001/activity", http.StatusOK, `{"activity":[]}`},
		{"/api/tasks/task-rich00001/activity/1", http.StatusNotFound, `{"error": "activity not found"}`},
		{"/api/tasks/task-rich00001/conversation", http.StatusOK, `{"conversation":[]}`},
	}
	for _, tt := range checks {
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, loopbackRequest(http.MethodGet, tt.path))
		if rr.Code != tt.code || rr.Body.String() != tt.body {
			t.Fatalf("%s %d %s", tt.path, rr.Code, rr.Body.String())
		}
	}
}

func TestSessionErrorName(t *testing.T) {
	undefined := fmt.Errorf("chat sessions: %w", &pgconn.PgError{Code: "42P01", Message: `relation "chat_messages" does not exist`})
	tests := []struct {
		name string
		err  error
		want string
	}{
		{name: "undefined table", err: undefined, want: "UndefinedTableError"},
		{name: "undefined column", err: fmt.Errorf("wrap: %w", &pgconn.PgError{Code: "42703"}), want: "UndefinedColumnError"},
		{name: "unknown sqlstate", err: &pgconn.PgError{Code: "99ZZZ"}, want: "UnknownPostgresError"},
		{name: "no database", err: errNoDatabase, want: "RuntimeError"},
		{name: "wrapped plain error", err: fmt.Errorf("chat sessions: %w", errors.New("dial")), want: "InterfaceError"},
		{name: "deadline", err: fmt.Errorf("chat sessions: %w", context.DeadlineExceeded), want: "TimeoutError"},
		{name: "canceled", err: context.Canceled, want: "CancelledError"},
		{name: "refused", err: &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}, want: "ConnectionRefusedError"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := sessionErrorName(tt.err)
			if got != tt.want || got == "wrapError" || got == "errorString" {
				t.Fatalf("sessionErrorName(%v) = %q, want %q", tt.err, got, tt.want)
			}
		})
	}
}

func TestEscapedTasksPathOnServer(t *testing.T) {
	reads := &recordingReads{tasksBody: map[string]any{"tasks": []any{}}}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	client := ts.Client()
	client.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	resp, err := client.Get(ts.URL + "/api/%74asks")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusNotFound || string(body) != `{"error": "not found"}` || resp.Header.Get("Location") != "" {
		t.Fatalf("status %d location %q body %s", resp.StatusCode, resp.Header.Get("Location"), body)
	}
	if ct := resp.Header.Get("Content-Type"); ct != "application/json; charset=utf-8" {
		t.Fatalf("content type %q", ct)
	}
	if len(reads.calls) != 0 {
		t.Fatalf("calls %v", reads.calls)
	}
}
