package httpapi

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/agents/broadcast"
	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/pyjson"
)

const activityStreamTask = "task-rich00001"

func TestActivityStreamConstantsMatchPython(t *testing.T) {
	if strings.Join(activityColumns, ",") != "seq,task_id,worker,kind,summary,metadata,input_tokens,output_tokens,created_at" {
		t.Fatalf("columns %q", activityColumns)
	}
	ora := activityOracle(t)
	res := ora.play(t, pyPlay{
		Task: activityStreamTask, Query: "since=0", Clients: 1,
		Steps: []pyStep{
			{Do: "arm"},
			{Do: "take"},
			{Do: "fire"},
			{Do: "arm"},
			{Do: "take"},
			{Do: "close"},
			{Do: "eof"},
		},
	})
	if len(res.Parts) != 3 {
		t.Fatalf("parts %d", len(res.Parts))
	}
	header, rest := splitHTTPHead(res.Parts[0])
	if header != activitySSEHeader || len(rest) != 0 {
		t.Fatalf("header %q rest %q", header, rest)
	}
	if string(res.Parts[1]) != activityPingFrame {
		t.Fatalf("ping %q", res.Parts[1])
	}
	if string(res.Parts[2]) != activityDoneFrame {
		t.Fatalf("done %q", res.Parts[2])
	}
	if len(res.Timeouts) != 2 || res.Timeouts[0] != 20 || res.Timeouts[1] != 20 {
		t.Fatalf("timeouts %v", res.Timeouts)
	}
	bad := ora.play(t, pyPlay{
		Task: activityStreamTask, Query: "since=nope", Clients: 1,
		Steps: []pyStep{{Do: "eof"}},
	})
	status, ctype, body := splitHTTP(t, bad.Parts[0])
	if status != "500" || ctype != pyjson.ContentType || string(body) != `{"error": "internal server error"}` {
		t.Fatalf("status %s type %s body %q", status, ctype, body)
	}
}

func TestActivityStreamReplayMatchesPython(t *testing.T) {
	rows := activityHistory(501, activityStreamTask)
	live := activityLiveObject()
	liveFrame := pythonActivityFrames(t, true, [][]any{objectPairs(live)})
	cases := []struct {
		name   string
		query  string
		minSeq int64
		sinces []int64
	}{
		{name: "missing", query: "", minSeq: 0, sinces: []int64{0, 500, 501}},
		{name: "blank", query: "since=", minSeq: 0, sinces: []int64{0, 500, 501}},
		{name: "zero", query: "since=0", minSeq: 0, sinces: []int64{0, 500, 501}},
		{name: "limit ignored", query: "since=0&limit=1", minSeq: 0, sinces: []int64{0, 500, 501}},
		{name: "tail", query: "since=500", minSeq: 500, sinces: []int64{500, 501}},
		{name: "caught up", query: "since=501", minSeq: 501, sinces: []int64{501}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reads := &streamReads{rows: rows}
			srv, _, addr := startActivity(t, reads, nil)
			conn := dialActivity(t, addr, activityStreamTask, tc.query)
			if header := readSSEHeaders(t, conn); header != activitySSEHeader {
				t.Fatalf("header %q", header)
			}
			want := pythonActivityFrames(t, false, historyPairs(rowsAfter(rows, tc.minSeq)))
			if got := readN(t, conn, len(want)); !bytes.Equal(got, want) {
				failBytes(t, got, want)
			}
			waitCount(t, srv.Activity, activityStreamTask, 1)
			sinces, limits, calls, task := reads.snapshot()
			if task != activityStreamTask || calls != len(tc.sinces) || !sameInts(sinces, tc.sinces) {
				t.Fatalf("task %s calls %d sinces %v", task, calls, sinces)
			}
			for _, limit := range limits {
				if limit != activityStreamPage {
					t.Fatalf("limit %d", limit)
				}
			}
			srv.Activity.Publish(activityStreamTask, live)
			if got := readN(t, conn, len(liveFrame)); !bytes.Equal(got, liveFrame) {
				failBytes(t, got, liveFrame)
			}
		})
	}
}

func TestActivityStreamIllegalSince(t *testing.T) {
	logs := captureLogs(t)
	reads := &streamReads{rows: activityHistory(1, activityStreamTask)}
	srv, _, addr := startActivity(t, reads, nil)
	want := `{"error": "internal server error"}`
	cases := []string{
		"since=nope",
		"since=1.5",
		"since=" + strings.Repeat("9", pythonIntMaxStrDigits+1),
	}
	for _, query := range cases {
		t.Run(query[:min(12, len(query))], func(t *testing.T) {
			raw := getActivity(t, addr, activityStreamTask, query, true)
			if !bytes.Contains(raw, []byte("HTTP/1.1 500 Internal Server Error\r\n")) {
				t.Fatalf("status %q", raw)
			}
			if bytes.Contains(raw, []byte("text/event-stream")) || bytes.Contains(raw, []byte("X-Accel-Buffering")) {
				t.Fatalf("sse header in %q", raw)
			}
			if !bytes.Contains(raw, []byte("\r\n\r\n"+want)) {
				t.Fatalf("body %q", raw)
			}
			if strings.Contains(query, "999") && bytes.Contains(raw, []byte(strings.Repeat("9", 40))) {
				t.Fatal("response contains the rejected digits")
			}
		})
	}
	if srv.Activity.Count(activityStreamTask) != 0 {
		t.Fatalf("subscribers %d", srv.Activity.Count(activityStreamTask))
	}
	if _, _, calls, _ := reads.snapshot(); calls != 0 {
		t.Fatalf("queries %d", calls)
	}
	if strings.Contains(logs.String(), strings.Repeat("9", 40)) {
		t.Fatalf("log %s", logs.String())
	}
}

func TestActivityStreamSinceOverflowStillLive(t *testing.T) {
	logs := captureLogs(t)
	digits := strings.Repeat("9", 40)
	reads := &streamReads{rows: activityHistory(2, activityStreamTask)}
	srv, _, addr := startActivity(t, reads, nil)
	conn := dialActivity(t, addr, activityStreamTask, "since="+digits)
	if header := readSSEHeaders(t, conn); header != activitySSEHeader {
		t.Fatalf("header %q", header)
	}
	waitCount(t, srv.Activity, activityStreamTask, 1)
	if _, _, calls, _ := reads.snapshot(); calls != 0 {
		t.Fatalf("queries %d", calls)
	}
	logged := logs.String()
	if !strings.Contains(logged, "since does not fit") || strings.Contains(logged, digits) {
		t.Fatalf("log %s", logged)
	}
	live := activityLiveObject()
	want := pythonActivityFrames(t, true, [][]any{objectPairs(live)})
	srv.Activity.Publish(activityStreamTask, live)
	if got := readN(t, conn, len(want)); !bytes.Equal(got, want) {
		failBytes(t, got, want)
	}
}

func TestActivityStreamDatabaseReplayError(t *testing.T) {
	logs := captureLogs(t)
	secret := errors.New("dial SECRET")
	live := activityLiveObject()
	wantLive := pythonActivityFrames(t, true, [][]any{objectPairs(live)})
	t.Run("first page", func(t *testing.T) {
		reads := &streamReads{rows: activityHistory(3, activityStreamTask), err: secret, errAfter: 0}
		srv, _, addr := startActivity(t, reads, nil)
		conn := dialActivity(t, addr, activityStreamTask, "since=0")
		if header := readSSEHeaders(t, conn); header != activitySSEHeader {
			t.Fatalf("header %q", header)
		}
		waitCount(t, srv.Activity, activityStreamTask, 1)
		srv.Activity.Publish(activityStreamTask, live)
		if got := readN(t, conn, len(wantLive)); !bytes.Equal(got, wantLive) {
			failBytes(t, got, wantLive)
		}
		expectQuiet(t, conn)
	})
	t.Run("second page", func(t *testing.T) {
		rows := activityHistory(501, activityStreamTask)
		reads := &streamReads{rows: rows, err: secret, errAfter: 1}
		srv, _, addr := startActivity(t, reads, nil)
		conn := dialActivity(t, addr, activityStreamTask, "since=0")
		_ = readSSEHeaders(t, conn)
		first := pythonActivityFrames(t, false, historyPairs(rowsAfter(rows, 0)[:500]))
		if got := readN(t, conn, len(first)); !bytes.Equal(got, first) {
			failBytes(t, got, first)
		}
		waitCount(t, srv.Activity, activityStreamTask, 1)
		srv.Activity.Publish(activityStreamTask, live)
		if got := readN(t, conn, len(wantLive)); !bytes.Equal(got, wantLive) {
			failBytes(t, got, wantLive)
		}
		expectQuiet(t, conn)
		_, _, calls, _ := reads.snapshot()
		if calls != 2 {
			t.Fatalf("queries %d", calls)
		}
	})
	logged := logs.String()
	if !strings.Contains(logged, "error: unreachable") || strings.Contains(logged, "SECRET") {
		t.Fatalf("log %s", logged)
	}
}

func TestActivityStreamReplayDoesNotSpin(t *testing.T) {
	logs := captureLogs(t)
	task := activityStreamTask
	t.Run("cursor does not advance", func(t *testing.T) {
		row := activityHistory(1, task)[0]
		row["seq"] = int64(3)
		reads := &streamReads{rows: []map[string]any{row}, unfiltered: true}
		srv, _, addr := startActivity(t, reads, nil)
		conn := dialActivity(t, addr, task, "since=10")
		_ = readSSEHeaders(t, conn)
		want := pythonActivityFrames(t, false, historyPairs([]map[string]any{row}))
		if got := readN(t, conn, len(want)); !bytes.Equal(got, want) {
			failBytes(t, got, want)
		}
		waitCount(t, srv.Activity, task, 1)
		_, _, calls, _ := reads.snapshot()
		if calls != 1 {
			t.Fatalf("queries %d", calls)
		}
		if !strings.Contains(logs.String(), "activity page did not advance") {
			t.Fatalf("log %s", logs.String())
		}
	})
	t.Run("row is not json", func(t *testing.T) {
		row := activityHistory(1, task)[0]
		row["metadata"] = map[string]any{"k": "v"}
		reads := &streamReads{rows: []map[string]any{row}}
		srv, _, addr := startActivity(t, reads, nil)
		conn := dialActivity(t, addr, task, "since=0")
		_ = readSSEHeaders(t, conn)
		waitCount(t, srv.Activity, task, 1)
		live := activityLiveObject()
		want := pythonActivityFrames(t, true, [][]any{objectPairs(live)})
		srv.Activity.Publish(task, live)
		if got := readN(t, conn, len(want)); !bytes.Equal(got, want) {
			failBytes(t, got, want)
		}
		if !strings.Contains(logs.String(), "activity row is not JSON") {
			t.Fatalf("log %s", logs.String())
		}
	})
}

func TestActivityStreamSkipsUnorderedLiveEvent(t *testing.T) {
	logs := captureLogs(t)
	reads := &streamReads{}
	srv, _, addr := startActivity(t, reads, nil)
	conn := dialActivity(t, addr, activityStreamTask, "since=0")
	_ = readSSEHeaders(t, conn)
	waitCount(t, srv.Activity, activityStreamTask, 1)
	srv.Activity.Publish(activityStreamTask, map[string]any{"kind": "note"})
	live := activityLiveObject()
	want := pythonActivityFrames(t, true, [][]any{objectPairs(live)})
	srv.Activity.Publish(activityStreamTask, live)
	if got := readN(t, conn, len(want)); !bytes.Equal(got, want) {
		failBytes(t, got, want)
	}
	expectQuiet(t, conn)
	if !strings.Contains(logs.String(), "activity event map[string]interface {}") {
		t.Fatalf("log %s", logs.String())
	}
}

func TestActivityStreamPingAndDone(t *testing.T) {
	live := activityLiveObject()
	want := pythonActivityFrames(t, true, [][]any{objectPairs(live)})
	t.Run("ping", func(t *testing.T) {
		clock := &manualClock{}
		srv, _, addr := startActivity(t, &streamReads{}, clock)
		conn := dialActivity(t, addr, activityStreamTask, "since=0")
		_ = readSSEHeaders(t, conn)
		waitCount(t, srv.Activity, activityStreamTask, 1)
		clock.fire(t)
		if got := readN(t, conn, len(activityPingFrame)); string(got) != activityPingFrame {
			t.Fatalf("ping %q", got)
		}
		expectQuiet(t, conn)
		for _, d := range clock.durations() {
			if d != 20*time.Second {
				t.Fatalf("timeout %s, want 20s", d)
			}
		}
	})
	t.Run("event beats a ready timeout", func(t *testing.T) {
		entered := make(chan struct{})
		release := make(chan struct{})
		clock := &manualClock{hold: release, arm: true, entered: entered}
		srv, _, addr := startActivity(t, &streamReads{}, clock)
		conn := dialActivity(t, addr, activityStreamTask, "since=0")
		_ = readSSEHeaders(t, conn)
		select {
		case <-entered:
		case <-time.After(2 * time.Second):
			t.Fatal("clock did not arm")
		}
		srv.Activity.Publish(activityStreamTask, live)
		close(release)
		if got := readN(t, conn, len(want)); !bytes.Equal(got, want) {
			failBytes(t, got, want)
		}
		expectQuiet(t, conn)
	})
	t.Run("done", func(t *testing.T) {
		srv, _, addr := startActivity(t, &streamReads{}, nil)
		conn := dialActivity(t, addr, activityStreamTask, "since=0")
		_ = readSSEHeaders(t, conn)
		waitCount(t, srv.Activity, activityStreamTask, 1)
		srv.Activity.Close(activityStreamTask)
		if got := readN(t, conn, len(activityDoneFrame)); string(got) != activityDoneFrame {
			t.Fatalf("done %q", got)
		}
		if rest := readUntilClose(t, conn); len(rest) != 0 {
			t.Fatalf("tail %q", rest)
		}
		waitCount(t, srv.Activity, activityStreamTask, 0)
	})
}

func TestActivityStreamDisconnectDropsSubscriber(t *testing.T) {
	srv, _, addr := startActivity(t, &streamReads{}, nil)
	conn := dialActivity(t, addr, activityStreamTask, "since=0")
	_ = readSSEHeaders(t, conn)
	waitCount(t, srv.Activity, activityStreamTask, 1)
	closeNow(conn)
	waitCount(t, srv.Activity, activityStreamTask, 0)
}

func TestActivityStreamTwoSubscribers(t *testing.T) {
	srv, _, addr := startActivity(t, &streamReads{}, nil)
	first := dialActivity(t, addr, activityStreamTask, "since=0")
	second := dialActivity(t, addr, activityStreamTask, "since=0")
	_ = readSSEHeaders(t, first)
	_ = readSSEHeaders(t, second)
	waitCount(t, srv.Activity, activityStreamTask, 2)
	one := pyjson.Object{{Key: "kind", Value: "phase"}, {Key: "summary", Value: "one"}, {Key: "worker", Value: "planner"}}
	two := pyjson.Object{{Key: "kind", Value: "phase"}, {Key: "summary", Value: "two"}, {Key: "worker", Value: "planner"}}
	wantOne := pythonActivityFrames(t, true, [][]any{objectPairs(one)})
	wantTwo := pythonActivityFrames(t, true, [][]any{objectPairs(two)})
	srv.Activity.Publish(activityStreamTask, one)
	if got := readN(t, first, len(wantOne)); !bytes.Equal(got, wantOne) {
		failBytes(t, got, wantOne)
	}
	if got := readN(t, second, len(wantOne)); !bytes.Equal(got, wantOne) {
		failBytes(t, got, wantOne)
	}
	closeNow(first)
	waitCount(t, srv.Activity, activityStreamTask, 1)
	srv.Activity.Publish(activityStreamTask, two)
	if got := readN(t, second, len(wantTwo)); !bytes.Equal(got, wantTwo) {
		failBytes(t, got, wantTwo)
	}
}

func TestActivityStreamShutdownDropsSubscriber(t *testing.T) {
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
			srv, server, addr := startActivity(t, &streamReads{}, nil)
			conn := dialActivity(t, addr, activityStreamTask, "since=0")
			_ = readSSEHeaders(t, conn)
			waitCount(t, srv.Activity, activityStreamTask, 1)
			if err := tc.stop(server); err != nil {
				t.Fatal(err)
			}
			if rest := readUntilClose(t, conn); len(rest) != 0 {
				t.Fatalf("tail %q", rest)
			}
			waitCount(t, srv.Activity, activityStreamTask, 0)
		})
	}
}

func TestActivityStreamReplayQueryHasDeadline(t *testing.T) {
	reads := &streamReads{}
	srv, _, addr := startActivity(t, reads, nil)
	conn := dialActivity(t, addr, activityStreamTask, "since=0")
	_ = readSSEHeaders(t, conn)
	waitCount(t, srv.Activity, activityStreamTask, 1)
	reads.mu.Lock()
	deadlines, remain, bare := reads.deadlines, reads.remain, reads.noDeadline
	reads.mu.Unlock()
	if bare || deadlines != 1 {
		t.Fatalf("deadlines %d bare %v", deadlines, bare)
	}
	if remain <= 0 || remain > 30*time.Second || remain < 30*time.Second-2*time.Second {
		t.Fatalf("query deadline remaining %s, want just under 30s", remain)
	}
}

func TestActivityStreamReplayTimeoutRecovers(t *testing.T) {
	logs := captureLogs(t)
	const streams = 8
	reads := &streamReads{waitCtx: true, rows: activityHistory(2, activityStreamTask)}
	srv, _, addr := startActivity(t, reads, nil)
	srv.activityQueryLimit = 500 * time.Millisecond
	conns := make([]net.Conn, streams)
	for i := range conns {
		conns[i] = dialActivity(t, addr, activityStreamTask, "since=0")
	}
	waitUntil(t, func() bool { return atomic.LoadInt32(&reads.maxFlight) == streams })
	for _, conn := range conns {
		if header := readSSEHeaders(t, conn); header != activitySSEHeader {
			t.Fatalf("header %q", header)
		}
	}
	waitCount(t, srv.Activity, activityStreamTask, streams)
	waitUntil(t, func() bool { return atomic.LoadInt32(&reads.flight) == 0 })
	if atomic.LoadInt32(&reads.maxFlight) != streams {
		t.Fatalf("max in flight %d", reads.maxFlight)
	}
	live := activityLiveObject()
	want := pythonActivityFrames(t, true, [][]any{objectPairs(live)})
	srv.Activity.Publish(activityStreamTask, live)
	for _, conn := range conns {
		if got := readN(t, conn, len(want)); !bytes.Equal(got, want) {
			failBytes(t, got, want)
		}
	}
	if !strings.Contains(logs.String(), "error: timeout") {
		t.Fatalf("log %s", logs.String())
	}
	reads.mu.Lock()
	defer reads.mu.Unlock()
	if reads.calls != streams || reads.noDeadline {
		t.Fatalf("calls %d bare %v", reads.calls, reads.noDeadline)
	}
}

func TestActivityStreamReplayCancelReleasesQuery(t *testing.T) {
	logs := captureLogs(t)
	reads := &streamReads{waitCtx: true}
	srv, _, addr := startActivity(t, reads, nil)
	srv.activityQueryLimit = 10 * time.Second
	started := time.Now()
	conn := dialActivity(t, addr, activityStreamTask, "since=0")
	waitUntil(t, func() bool { return atomic.LoadInt32(&reads.flight) == 1 })
	closeNow(conn)
	waitCount(t, srv.Activity, activityStreamTask, 0)
	waitUntil(t, func() bool { return atomic.LoadInt32(&reads.flight) == 0 })
	if time.Since(started) > 2*time.Second {
		t.Fatal("canceled query waited out its own deadline")
	}
	if strings.Contains(logs.String(), "error: timeout") {
		t.Fatalf("log %s", logs.String())
	}
}

func TestActivityStreamNilDatabaseStillLive(t *testing.T) {
	logs := captureLogs(t)
	srv := New("dev", nil, "")
	_, addr := serveSSE(t, srv.Handler())
	conn := dialActivity(t, addr, activityStreamTask, "since=0")
	if header := readSSEHeaders(t, conn); header != activitySSEHeader {
		t.Fatalf("header %q", header)
	}
	waitCount(t, srv.TaskActivity(), activityStreamTask, 1)
	live := activityLiveObject()
	want := pythonActivityFrames(t, true, [][]any{objectPairs(live)})
	srv.TaskActivity().Publish(activityStreamTask, live)
	if got := readN(t, conn, len(want)); !bytes.Equal(got, want) {
		failBytes(t, got, want)
	}
	if !strings.Contains(logs.String(), "no database") {
		t.Fatalf("log %s", logs.String())
	}
}

func TestActivityStreamRejectsOtherMethods(t *testing.T) {
	reads := &streamReads{rows: activityHistory(1, activityStreamTask)}
	srv := New("dev", nil, "")
	srv.Reads = reads
	srv.Activity = broadcast.New()
	h := srv.Handler()
	cases := []struct {
		method string
		path   string
	}{
		{http.MethodHead, "/api/tasks/" + activityStreamTask + "/activity/stream"},
		{http.MethodPost, "/api/tasks/" + activityStreamTask + "/activity/stream"},
		{http.MethodPut, "/api/tasks/" + activityStreamTask + "/activity/stream"},
		{http.MethodGet, "/api/tasks/nope/activity/stream"},
		{http.MethodGet, "/api/tasks/task-bad_id/activity/stream"},
		{http.MethodGet, "/api/tasks/" + activityStreamTask + "/activity/stream/extra"},
	}
	for _, tc := range cases {
		t.Run(tc.method+" "+tc.path, func(t *testing.T) {
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(tc.method, tc.path))
			if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if rr.Header().Get("Content-Type") != pyjson.ContentType {
				t.Fatalf("content-type %s", rr.Header().Get("Content-Type"))
			}
		})
	}
	if _, _, calls, _ := reads.snapshot(); calls != 0 || srv.Activity.Count(activityStreamTask) != 0 {
		t.Fatal("rejected request touched activity")
	}
}

func TestDeferredChannelGetsStayNotFound(t *testing.T) {
	h := New("dev", nil, "").Handler()
	paths := []string{
		"/api/channels/lark/status",
		"/api/channels/lark/people",
		"/api/channels/lark_bot/status",
		"/api/channels/lark_bot/people",
		"/api/agent/v1/tools",
	}
	for _, path := range paths {
		t.Run(path, func(t *testing.T) {
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(http.MethodGet, path))
			if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error": "not found"}` {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
		})
	}
}

func TestActivityStreamDatabaseMatchesPython(t *testing.T) {
	const task = "task-stream0001"
	dsn := withHTTPDisposableDB(t, "loopgo_g1q_sse")
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
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO projects (id, name) VALUES ('proj-stream', 'Stream')`); err != nil {
		t.Fatal(err)
	}
	if _, err := sqlDB.ExecContext(ctx, `INSERT INTO tasks (id, requirement, project_id) VALUES ($1, 'ship it', 'proj-stream')`, task); err != nil {
		t.Fatal(err)
	}
	if _, err := sqlDB.ExecContext(ctx, `
		INSERT INTO activity (task_id, worker, kind, summary, metadata, input_tokens, output_tokens, created_at)
		SELECT $1,
		       CASE WHEN g = 1 THEN '工人' ELSE 'w' END,
		       'note',
		       CASE WHEN g = 2 THEN NULL WHEN g = 1 THEN E'say "hi" & <tag>\nnext' ELSE 's' END,
		       CASE WHEN g = 1 THEN '{"k": "值"}'::jsonb ELSE '{}'::jsonb END,
		       CASE WHEN g = 1 THEN 3 ELSE 0 END,
		       CASE WHEN g = 1 THEN 4 ELSE 0 END,
		       TIMESTAMPTZ '2018-01-01 00:00:00+00' + g * INTERVAL '1 second'
		FROM generate_series(1, 501) AS g`, task); err != nil {
		t.Fatal(err)
	}
	store := db.NewStore(sqlDB)
	all, err := store.ListActivity(ctx, task, 0, 501)
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != 501 {
		t.Fatalf("rows %d types %s", len(all), rowTypes(all))
	}
	if all[0]["created_at"] != "2018-01-01T00:00:01+00:00" {
		t.Fatalf("created_at %#v (%T)", all[0]["created_at"], all[0]["created_at"])
	}
	meta, _ := all[0]["metadata"].(string)
	if meta != `{"k": "值"}` {
		t.Fatalf("metadata %q (%T)", all[0]["metadata"], all[0]["metadata"])
	}
	tail, err := store.ListActivity(ctx, task, 500, 500)
	if err != nil {
		t.Fatal(err)
	}
	if len(tail) != 1 || tail[0]["seq"] != int64(501) {
		t.Fatalf("tail %#v", tail)
	}

	srv := New("dev", db.Probe{DB: sqlDB}, "")
	srv.Reads = store
	srv.Activity = broadcast.New()
	_, addr := serveSSE(t, srv.Handler())
	conn := dialActivity(t, addr, task, "since=0")
	if header := readSSEHeaders(t, conn); header != activitySSEHeader {
		t.Fatalf("header %q", header)
	}
	want := pythonActivityFrames(t, false, historyPairs(all))
	if got := readN(t, conn, len(want)); !bytes.Equal(got, want) {
		t.Fatalf("replay mismatch (%s)", rowTypes(all))
	}
	waitCount(t, srv.Activity, task, 1)

	tailConn := dialActivity(t, addr, task, "since=500")
	_ = readSSEHeaders(t, tailConn)
	wantTail := pythonActivityFrames(t, false, historyPairs(tail))
	if got := readN(t, tailConn, len(wantTail)); !bytes.Equal(got, wantTail) {
		failBytes(t, got, wantTail)
	}

	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/"+task+"/activity/stream?since=nope"))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error": "internal server error"}` {
		t.Fatalf("bad since %d %s", rr.Code, rr.Body.String())
	}

	live := activityLiveObject()
	wantLive := pythonActivityFrames(t, true, [][]any{objectPairs(live)})
	srv.Activity.Publish(task, live)
	if got := readN(t, conn, len(wantLive)); !bytes.Equal(got, wantLive) {
		failBytes(t, got, wantLive)
	}
	srv.Activity.Close(task)
	if got := readN(t, conn, len(activityDoneFrame)); string(got) != activityDoneFrame {
		t.Fatalf("done %q", got)
	}
	if rest := readUntilClose(t, conn); len(rest) != 0 {
		t.Fatalf("tail %q", rest)
	}
	waitCount(t, srv.Activity, task, 0)
}

func activityLiveObject() pyjson.Object {
	return pyjson.Object{
		{Key: "kind", Value: "phase"},
		{Key: "summary", Value: "live 值"},
		{Key: "worker", Value: "planner"},
	}
}

func activityHistory(n int, task string) []map[string]any {
	rows := make([]map[string]any, n)
	for i := 1; i <= n; i++ {
		row := map[string]any{
			"seq":           int64(i),
			"task_id":       task,
			"worker":        "w",
			"kind":          "note",
			"summary":       "s",
			"metadata":      "{}",
			"input_tokens":  int32(0),
			"output_tokens": int32(0),
			"created_at":    "2018-01-01T00:00:01+00:00",
		}
		if i == 1 {
			row["worker"] = "工人"
			row["summary"] = "say \"hi\" & <tag>\nnext"
			row["metadata"] = `{"k": "值"}`
			row["input_tokens"] = int32(3)
			row["output_tokens"] = int32(4)
		}
		if i == 2 {
			row["summary"] = nil
		}
		rows[i-1] = row
	}
	return rows
}

func rowsAfter(rows []map[string]any, since int64) []map[string]any {
	var out []map[string]any
	for _, row := range rows {
		seq, ok := activityInt(row["seq"])
		if ok && seq > since {
			out = append(out, row)
		}
	}
	return out
}

func historyPairs(rows []map[string]any) [][]any {
	out := make([][]any, len(rows))
	for i, row := range rows {
		pairs := make([]any, 0, len(activityColumns))
		for _, key := range activityColumns {
			value, ok := row[key]
			if !ok {
				continue
			}
			pairs = append(pairs, []any{key, jsonSafe(value)})
		}
		out[i] = pairs
	}
	return out
}

func objectPairs(obj pyjson.Object) []any {
	pairs := make([]any, len(obj))
	for i, field := range obj {
		pairs[i] = []any{field.Key, jsonSafe(field.Value)}
	}
	return pairs
}

func jsonSafe(v any) any {
	if b, ok := v.([]byte); ok {
		return string(b)
	}
	return v
}

// pythonActivityFrames is the real Python handler's frames for these rows.
// Replay rows are one list_activity page. A live row is one published event.
func pythonActivityFrames(t *testing.T, live bool, rows [][]any) []byte {
	t.Helper()
	ora := activityOracle(t)
	if live {
		if len(rows) != 1 {
			t.Fatalf("live rows %d", len(rows))
		}
		res := ora.play(t, pyPlay{
			Task: activityStreamTask, Query: "since=0", Clients: 1,
			Steps: []pyStep{
				{Do: "arm"},
				{Do: "take"},
				{Do: "publish", Event: rows[0]},
				{Do: "arm"},
				{Do: "take"},
				{Do: "drop"},
			},
		})
		if len(res.Parts) != 2 {
			t.Fatalf("live parts %d", len(res.Parts))
		}
		if _, rest := splitHTTPHead(res.Parts[0]); len(rest) != 0 {
			t.Fatalf("live header carried %q", rest)
		}
		return res.Parts[1]
	}
	res := ora.play(t, pyPlay{
		Task: activityStreamTask, Query: "since=0", Rows: rows, Once: true, Clients: 1,
		Steps: []pyStep{{Do: "arm"}, {Do: "take"}, {Do: "drop"}},
	})
	if len(res.Parts) != 1 {
		t.Fatalf("replay parts %d", len(res.Parts))
	}
	_, frames := splitHTTPHead(res.Parts[0])
	return frames
}

type streamReads struct {
	recordingReads
	mu         sync.Mutex
	rows       []map[string]any
	err        error
	errAfter   int
	unfiltered bool
	calls      int
	sinces     []int64
	limits     []int64
	task       string
	// waitCtx blocks until ctx is done, the way a stuck query holds a connection.
	waitCtx    bool
	remain     time.Duration
	deadlines  int
	noDeadline bool
	flight     int32
	maxFlight  int32
}

func (r *streamReads) ListActivity(ctx context.Context, taskID string, since, limit int64) ([]map[string]any, error) {
	r.mu.Lock()
	r.calls++
	call := r.calls
	r.task = taskID
	r.sinces = append(r.sinces, since)
	r.limits = append(r.limits, limit)
	if dl, ok := ctx.Deadline(); ok {
		r.remain = time.Until(dl)
		r.deadlines++
	} else {
		r.noDeadline = true
	}
	wait := r.waitCtx
	fail := r.err
	failAfter := r.errAfter
	rows := r.rows
	unfiltered := r.unfiltered
	r.mu.Unlock()

	if wait {
		n := int(atomic.AddInt32(&r.flight, 1))
		for {
			old := atomic.LoadInt32(&r.maxFlight)
			if int32(n) <= old || atomic.CompareAndSwapInt32(&r.maxFlight, old, int32(n)) {
				break
			}
		}
		<-ctx.Done()
		atomic.AddInt32(&r.flight, -1)
		return nil, ctx.Err()
	}
	if fail != nil && call-1 == failAfter {
		return nil, fail
	}
	var out []map[string]any
	for _, row := range rows {
		seq, ok := activityInt(row["seq"])
		if unfiltered || (ok && seq > since) {
			out = append(out, row)
		}
	}
	if limit >= 0 && int64(len(out)) > limit {
		out = out[:limit]
	}
	if out == nil {
		out = []map[string]any{}
	}
	return out, nil
}

func (r *streamReads) snapshot() (sinces, limits []int64, calls int, task string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]int64(nil), r.sinces...), append([]int64(nil), r.limits...), r.calls, r.task
}

type manualClock struct {
	mu      sync.Mutex
	durs    []time.Duration
	ch      chan time.Time
	hold    <-chan struct{}
	entered chan struct{}
	arm     bool
}

func (c *manualClock) After(d time.Duration) <-chan time.Time {
	c.mu.Lock()
	c.durs = append(c.durs, d)
	hold := c.hold
	arm := c.arm
	entered := c.entered
	c.hold = nil
	c.arm = false
	c.entered = nil
	c.mu.Unlock()
	if entered != nil {
		close(entered)
	}
	if hold != nil {
		<-hold
	}
	ch := make(chan time.Time, 1)
	if arm {
		ch <- time.Now()
	}
	c.mu.Lock()
	c.ch = ch
	c.mu.Unlock()
	return ch
}

func (c *manualClock) fire(t *testing.T) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		c.mu.Lock()
		ch := c.ch
		c.mu.Unlock()
		if ch != nil {
			select {
			case ch <- time.Now():
				return
			default:
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("clock was not waiting")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func (c *manualClock) durations() []time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]time.Duration(nil), c.durs...)
}

func startActivity(t *testing.T, reads Reader, clock *manualClock) (*Server, *http.Server, string) {
	t.Helper()
	srv := New("dev", nil, "")
	srv.Reads = reads
	srv.Activity = broadcast.New()
	if clock != nil {
		srv.activityAfter = clock.After
	}
	server, addr := serveSSE(t, srv.Handler())
	return srv, server, addr
}

func dialActivity(t *testing.T, addr, task, rawQuery string) net.Conn {
	t.Helper()
	return dialActivityRequest(t, addr, activityRequest(task, rawQuery, false))
}

func getActivity(t *testing.T, addr, task, rawQuery string, closeConn bool) []byte {
	t.Helper()
	conn := dialActivityRequest(t, addr, activityRequest(task, rawQuery, closeConn))
	return readUntilClose(t, conn)
}

func activityRequest(task, rawQuery string, closeConn bool) string {
	target := "/api/tasks/" + task + "/activity/stream"
	if rawQuery != "" {
		target += "?" + rawQuery
	}
	extra := ""
	if closeConn {
		extra = "Connection: close\r\n"
	}
	return "GET " + target + " HTTP/1.1\r\nHost: 127.0.0.1\r\n" + extra + "\r\n"
}

func dialActivityRequest(t *testing.T, addr, request string) net.Conn {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	if _, err := io.WriteString(conn, request); err != nil {
		t.Fatal(err)
	}
	return conn
}

func waitCount(t *testing.T, bus *broadcast.Broadcaster, id string, n int) {
	t.Helper()
	waitUntil(t, func() bool { return bus.Count(id) == n })
}

func expectQuiet(t *testing.T, conn net.Conn) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(40 * time.Millisecond))
	var buf [8]byte
	n, err := conn.Read(buf[:])
	if n != 0 || err == nil {
		t.Fatalf("extra %q", buf[:n])
	}
	var ne net.Error
	if !errors.As(err, &ne) || !ne.Timeout() {
		t.Fatalf("read %v", err)
	}
}

func sameInts(got, want []int64) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

func failBytes(t *testing.T, got, want []byte) {
	t.Helper()
	n := len(got)
	if len(want) < n {
		n = len(want)
	}
	i := 0
	for i < n && got[i] == want[i] {
		i++
	}
	t.Fatalf("mismatch at %d len %d/%d\ngot %q\nwant %q", i, len(got), len(want), snippet(got, i), snippet(want, i))
}

func snippet(b []byte, i int) string {
	start := i - 40
	if start < 0 {
		start = 0
	}
	end := i + 40
	if end > len(b) {
		end = len(b)
	}
	return string(b[start:end])
}

func rowTypes(rows []map[string]any) string {
	if len(rows) == 0 {
		return "none"
	}
	var b strings.Builder
	for _, key := range activityColumns {
		fmt.Fprintf(&b, "%s=%T ", key, rows[0][key])
	}
	return b.String()
}
