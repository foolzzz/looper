package httpapi

import (
	"bufio"
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/pyjson"
)

// pyActivity is one long-running reference server: the Python
// AgentHTTPServer, its Broadcaster, and _handle_task_activity_stream.
// Frames come from that handler over a real socket, not from a Go copy of
// the format string.
type pyActivity struct {
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	out    *bufio.Reader
	stderr *lockedBuf
	mu     sync.Mutex
	done   chan struct{}
	failed bool
}

type pyPlay struct {
	Op        string   `json:"op"`
	Task      string   `json:"task"`
	Query     string   `json:"query,omitempty"`
	Rows      any      `json:"rows,omitempty"`
	Once      bool     `json:"once,omitempty"`
	FailAfter *int     `json:"fail_after,omitempty"`
	Hold      bool     `json:"hold,omitempty"`
	Clients   int      `json:"clients,omitempty"`
	Steps     []pyStep `json:"steps"`
}

type pyStep struct {
	Do     string `json:"do"`
	N      int    `json:"n"`
	Client int    `json:"client"`
	Event  any    `json:"event,omitempty"`
}

type pyCall struct {
	Task  string `json:"task"`
	Since string `json:"since"`
	Limit int    `json:"limit"`
}

type pyResult struct {
	OK       bool      `json:"ok"`
	Error    string    `json:"error"`
	PartsB64 []string  `json:"parts"`
	Calls    []pyCall  `json:"calls"`
	Timeouts []float64 `json:"timeouts"`
	Subs     int       `json:"subs"`
	Marks    []int     `json:"marks"`
	Parts    [][]byte  `json:"-"`
}

var (
	pyOnce sync.Once
	pyAct  *pyActivity
	pyErr  error
)

func activityOracle(t *testing.T) *pyActivity {
	t.Helper()
	pyOnce.Do(func() { pyAct, pyErr = startActivityOracle() })
	if pyErr != nil {
		t.Fatalf("python activity oracle: %v", pyErr)
	}
	return pyAct
}

func stopActivityOracle() {
	if pyAct == nil {
		return
	}
	_ = pyAct.stdin.Close()
	select {
	case <-pyAct.done:
	case <-time.After(2 * time.Second):
		if pyAct.cmd.Process != nil {
			_ = pyAct.cmd.Process.Kill()
		}
		<-pyAct.done
	}
}

func startActivityOracle() (*pyActivity, error) {
	root := loopgoRoot()
	python := filepath.Join(root, ".venv", "bin", "python")
	if st, err := os.Stat(python); err != nil || st.IsDir() {
		found, err := exec.LookPath("python3")
		if err != nil {
			return nil, fmt.Errorf("python3: %w", err)
		}
		python = found
	}
	script := filepath.Join(root, "go", "internal", "httpapi", "testdata", "activity_stream_oracle.py")
	cmd := exec.Command(python, script)
	cmd.Dir = root
	cmd.Env = append(os.Environ(), "PYTHONPATH="+root, "PYTHONUNBUFFERED=1")
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr := &lockedBuf{}
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	ora := &pyActivity{
		cmd:    cmd,
		stdin:  stdin,
		out:    bufio.NewReader(stdout),
		stderr: stderr,
		done:   make(chan struct{}),
	}
	go func() {
		_ = cmd.Wait()
		close(ora.done)
	}()
	line, err := readOracleLine(ora, 30*time.Second)
	if err != nil {
		_ = cmd.Process.Kill()
		<-ora.done
		return nil, fmt.Errorf("%w\n%s", err, stderr.String())
	}
	var ready struct {
		Ready bool `json:"ready"`
		Port  int  `json:"port"`
	}
	if err := json.Unmarshal(line, &ready); err != nil || !ready.Ready || ready.Port < 18000 {
		_ = cmd.Process.Kill()
		<-ora.done
		return nil, fmt.Errorf("oracle ready %s\n%s", line, stderr.String())
	}
	return ora, nil
}

func (o *pyActivity) play(t *testing.T, req pyPlay) pyResult {
	t.Helper()
	req.Op = "play"
	if req.Clients == 0 {
		req.Clients = 1
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.failed {
		t.Fatal("python oracle is dead")
	}
	body, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := o.stdin.Write(append(body, '\n')); err != nil {
		o.failed = true
		t.Fatalf("python oracle write: %v\n%s", err, o.stderr.String())
	}
	line, err := readOracleLine(o, 20*time.Second)
	if err != nil {
		o.failed = true
		if o.cmd.Process != nil {
			_ = o.cmd.Process.Kill()
		}
		t.Fatalf("python oracle: %v\n%s", err, o.stderr.String())
	}
	var res pyResult
	if err := json.Unmarshal(line, &res); err != nil {
		t.Fatalf("python oracle json: %v\n%s", err, line)
	}
	if !res.OK {
		t.Fatalf("python oracle\n%s", res.Error)
	}
	res.Parts = make([][]byte, len(res.PartsB64))
	for i, part := range res.PartsB64 {
		decoded, err := base64.StdEncoding.DecodeString(part)
		if err != nil {
			t.Fatal(err)
		}
		res.Parts[i] = decoded
	}
	return res
}

func readOracleLine(o *pyActivity, limit time.Duration) ([]byte, error) {
	type got struct {
		line []byte
		err  error
	}
	ch := make(chan got, 1)
	go func() {
		line, err := o.out.ReadBytes('\n')
		ch <- got{line, err}
	}()
	select {
	case g := <-ch:
		return g.line, g.err
	case <-time.After(limit):
		return nil, fmt.Errorf("timed out after %s", limit)
	}
}

func loopgoRoot() string {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		return ""
	}
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", "..", ".."))
}

type lockedBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuf) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

func (l *lockedBuf) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}

func splitHTTPHead(raw []byte) (header string, rest []byte) {
	i := bytes.Index(raw, []byte("\r\n\r\n"))
	if i < 0 {
		return "", raw
	}
	return string(raw[:i+4]), raw[i+4:]
}

func splitHTTP(t *testing.T, raw []byte) (status, ctype string, body []byte) {
	t.Helper()
	head, body := splitHTTPHead(raw)
	if head == "" {
		t.Fatalf("response %q", raw)
	}
	lines := strings.Split(strings.TrimSuffix(head, "\r\n\r\n"), "\r\n")
	fields := strings.SplitN(lines[0], " ", 3)
	if len(fields) < 2 {
		t.Fatalf("status line %q", lines[0])
	}
	status = fields[1]
	for _, line := range lines[1:] {
		key, value, ok := strings.Cut(line, ": ")
		if ok && strings.EqualFold(key, "Content-Type") {
			ctype = value
		}
	}
	return status, ctype, body
}

func readEqual(t *testing.T, conn net.Conn, want []byte) {
	t.Helper()
	if len(want) == 0 {
		t.Fatal("empty python part")
	}
	got := readN(t, conn, len(want))
	if !bytes.Equal(got, want) {
		failBytes(t, got, want)
	}
}

func sameStreamCalls(t *testing.T, py []pyCall, sinces, limits []int64, calls int, task, wantTask string) {
	t.Helper()
	if calls != len(py) || len(sinces) != len(py) || len(limits) != len(py) || task != wantTask {
		t.Fatalf("go calls %d task %s sinces %v limits %v py %+v", calls, task, sinces, limits, py)
	}
	for i, call := range py {
		if call.Task != wantTask || call.Limit != 500 || limits[i] != 500 || call.Since != strconv.FormatInt(sinces[i], 10) {
			t.Fatalf("call %d go since %d limit %d py %+v", i, sinces[i], limits[i], call)
		}
	}
}

func failAt(n int) *int { return &n }

func TestActivityStreamSocketMatchesPython(t *testing.T) {
	ora := activityOracle(t)
	live := activityLiveObject()
	liveEvent := objectPairs(live)

	t.Run("over 500 then done", func(t *testing.T) {
		rows := activityHistory(501, activityStreamTask)
		py := ora.play(t, pyPlay{
			Task: activityStreamTask, Query: "since=0&limit=1", Rows: historyPairs(rows),
			Steps: []pyStep{
				{Do: "arm"}, {Do: "take"},
				{Do: "publish", Event: liveEvent},
				{Do: "arm"}, {Do: "take"},
				{Do: "close"}, {Do: "eof"},
			},
		})
		if len(py.Parts) != 3 || len(py.Calls) != 3 {
			t.Fatalf("parts %d calls %+v", len(py.Parts), py.Calls)
		}
		reads := &streamReads{rows: rows}
		srv, _, addr := startActivity(t, reads, nil)
		conn := dialActivity(t, addr, activityStreamTask, "since=0&limit=1")
		readEqual(t, conn, py.Parts[0])
		waitCount(t, srv.Activity, activityStreamTask, 1)
		sinces, limits, calls, task := reads.snapshot()
		sameStreamCalls(t, py.Calls, sinces, limits, calls, task, activityStreamTask)
		srv.Activity.Publish(activityStreamTask, live)
		readEqual(t, conn, py.Parts[1])
		srv.Activity.Close(activityStreamTask)
		if got := readUntilClose(t, conn); !bytes.Equal(got, py.Parts[2]) {
			failBytes(t, got, py.Parts[2])
		}
		waitCount(t, srv.Activity, activityStreamTask, 0)
	})

	t.Run("since cursors", func(t *testing.T) {
		rows := activityHistory(3, activityStreamTask)
		cases := []struct {
			query  string
			sinces []int64
		}{
			{query: "", sinces: []int64{0, 3}},
			{query: "since=", sinces: []int64{0, 3}},
			{query: "since=0", sinces: []int64{0, 3}},
			{query: "since=2", sinces: []int64{2, 3}},
		}
		for _, tc := range cases {
			t.Run(tc.query, func(t *testing.T) {
				py := ora.play(t, pyPlay{
					Task: activityStreamTask, Query: tc.query, Rows: historyPairs(rows),
					Steps: []pyStep{{Do: "arm"}, {Do: "take"}, {Do: "drop"}},
				})
				reads := &streamReads{rows: rows}
				srv, _, addr := startActivity(t, reads, nil)
				conn := dialActivity(t, addr, activityStreamTask, tc.query)
				readEqual(t, conn, py.Parts[0])
				// The follow-up page is issued after those history frames are
				// flushed. Python records calls only once the live subscription
				// has armed. Wait for that subscriber, then compare.
				waitCount(t, srv.Activity, activityStreamTask, 1)
				sinces, limits, calls, task := reads.snapshot()
				sameStreamCalls(t, py.Calls, sinces, limits, calls, task, activityStreamTask)
				if len(sinces) != len(tc.sinces) || !sameInts(sinces, tc.sinces) {
					t.Fatalf("sinces %v", sinces)
				}
			})
		}
	})

	t.Run("illegal since", func(t *testing.T) {
		_, _, addr := startActivity(t, &streamReads{}, nil)
		queries := []string{
			"since=nope",
			"since=1.5",
			"since=" + strings.Repeat("9", pythonIntMaxStrDigits+1),
		}
		for _, query := range queries {
			t.Run(query[:min(12, len(query))], func(t *testing.T) {
				py := ora.play(t, pyPlay{
					Task: activityStreamTask, Query: query,
					Steps: []pyStep{{Do: "eof"}},
				})
				got := getActivity(t, addr, activityStreamTask, query, true)
				pStatus, pType, pBody := splitHTTP(t, py.Parts[0])
				gStatus, gType, gBody := splitHTTP(t, got)
				if pStatus != "500" || gStatus != pStatus || gType != pType || !bytes.Equal(gBody, pBody) {
					t.Fatalf("python %s %s %q\ngo %s %s %q", pStatus, pType, pBody, gStatus, gType, gBody)
				}
				if bytes.Contains(got, []byte("text/event-stream")) || bytes.Contains(py.Parts[0], []byte("text/event-stream")) {
					t.Fatal("illegal since opened an event stream")
				}
				if len(py.Calls) != 0 {
					t.Fatalf("python queried %#v", py.Calls)
				}
			})
		}
	})

	t.Run("since past int64", func(t *testing.T) {
		digits := strings.Repeat("9", 40)
		rows := activityHistory(2, activityStreamTask)
		py := ora.play(t, pyPlay{
			Task: activityStreamTask, Query: "since=" + digits, Rows: historyPairs(rows),
			Steps: []pyStep{
				{Do: "arm"}, {Do: "take"},
				{Do: "publish", Event: liveEvent},
				{Do: "arm"}, {Do: "take"},
				{Do: "drop"},
			},
		})
		if len(py.Calls) != 1 || py.Calls[0].Since != digits {
			t.Fatalf("python calls %+v", py.Calls)
		}
		if _, rest := splitHTTPHead(py.Parts[0]); len(rest) != 0 {
			t.Fatal("python replayed a row for an overflowing since")
		}
		reads := &streamReads{rows: rows}
		srv, _, addr := startActivity(t, reads, nil)
		conn := dialActivity(t, addr, activityStreamTask, "since="+digits)
		readEqual(t, conn, py.Parts[0])
		waitCount(t, srv.Activity, activityStreamTask, 1)
		if _, _, calls, _ := reads.snapshot(); calls != 0 {
			t.Fatalf("go queries %d", calls)
		}
		srv.Activity.Publish(activityStreamTask, live)
		readEqual(t, conn, py.Parts[1])
	})

	t.Run("replay error", func(t *testing.T) {
		secret := errors.New("dial SECRET")
		t.Run("first page", func(t *testing.T) {
			py := ora.play(t, pyPlay{
				Task: activityStreamTask, Query: "since=0", Rows: historyPairs(activityHistory(3, activityStreamTask)),
				FailAfter: failAt(0),
				Steps: []pyStep{
					{Do: "arm"}, {Do: "take"},
					{Do: "publish", Event: liveEvent},
					{Do: "arm"}, {Do: "take"},
					{Do: "drop"},
				},
			})
			if _, rest := splitHTTPHead(py.Parts[0]); len(rest) != 0 || len(py.Calls) != 1 {
				t.Fatalf("python replay %q calls %+v", py.Parts[0], py.Calls)
			}
			reads := &streamReads{rows: activityHistory(3, activityStreamTask), err: secret, errAfter: 0}
			srv, _, addr := startActivity(t, reads, nil)
			conn := dialActivity(t, addr, activityStreamTask, "since=0")
			readEqual(t, conn, py.Parts[0])
			waitCount(t, srv.Activity, activityStreamTask, 1)
			srv.Activity.Publish(activityStreamTask, live)
			readEqual(t, conn, py.Parts[1])
		})
		t.Run("second page", func(t *testing.T) {
			rows := activityHistory(501, activityStreamTask)
			py := ora.play(t, pyPlay{
				Task: activityStreamTask, Query: "since=0", Rows: historyPairs(rows),
				FailAfter: failAt(1),
				Steps: []pyStep{
					{Do: "arm"}, {Do: "take"},
					{Do: "publish", Event: liveEvent},
					{Do: "arm"}, {Do: "take"},
					{Do: "drop"},
				},
			})
			if len(py.Calls) != 2 {
				t.Fatalf("python calls %+v", py.Calls)
			}
			reads := &streamReads{rows: rows, err: secret, errAfter: 1}
			srv, _, addr := startActivity(t, reads, nil)
			conn := dialActivity(t, addr, activityStreamTask, "since=0")
			readEqual(t, conn, py.Parts[0])
			waitCount(t, srv.Activity, activityStreamTask, 1)
			srv.Activity.Publish(activityStreamTask, live)
			readEqual(t, conn, py.Parts[1])
			sinces, limits, calls, task := reads.snapshot()
			sameStreamCalls(t, py.Calls, sinces, limits, calls, task, activityStreamTask)
		})
	})

	t.Run("ping", func(t *testing.T) {
		py := ora.play(t, pyPlay{
			Task: activityStreamTask, Query: "since=0",
			Steps: []pyStep{
				{Do: "arm"}, {Do: "take"},
				{Do: "fire"},
				{Do: "arm"}, {Do: "take"},
				{Do: "drop"},
			},
		})
		if len(py.Timeouts) != 2 || py.Timeouts[0] != 20 || py.Timeouts[1] != 20 {
			t.Fatalf("python timeouts %v", py.Timeouts)
		}
		if string(py.Parts[1]) != ": ping\n\n" {
			t.Fatalf("python ping %q", py.Parts[1])
		}
		clock := &manualClock{}
		srv, _, addr := startActivity(t, &streamReads{}, clock)
		conn := dialActivity(t, addr, activityStreamTask, "since=0")
		waitCount(t, srv.Activity, activityStreamTask, 1)
		readEqual(t, conn, py.Parts[0])
		clock.fire(t)
		readEqual(t, conn, py.Parts[1])
		for _, d := range clock.durations() {
			if d != 20*time.Second {
				t.Fatalf("timeout %s, want 20s", d)
			}
		}
	})

	t.Run("event beats a ready timeout", func(t *testing.T) {
		py := ora.play(t, pyPlay{
			Task: activityStreamTask, Query: "since=0", Hold: true,
			Steps: []pyStep{
				{Do: "entered"}, {Do: "take"},
				{Do: "publish", Event: liveEvent},
				{Do: "fire"}, {Do: "release"},
				{Do: "arm"}, {Do: "take"},
				{Do: "drop"},
			},
		})
		if string(py.Parts[1]) == ": ping\n\n" || !bytes.Equal(py.Parts[1], pythonActivityFrames(t, true, [][]any{liveEvent})) {
			t.Fatalf("python chose %q", py.Parts[1])
		}
		entered := make(chan struct{})
		release := make(chan struct{})
		clock := &manualClock{hold: release, arm: true, entered: entered}
		srv, _, addr := startActivity(t, &streamReads{}, clock)
		conn := dialActivity(t, addr, activityStreamTask, "since=0")
		select {
		case <-entered:
		case <-time.After(2 * time.Second):
			t.Fatal("clock did not arm")
		}
		readEqual(t, conn, py.Parts[0])
		srv.Activity.Publish(activityStreamTask, live)
		close(release)
		readEqual(t, conn, py.Parts[1])
		expectQuiet(t, conn)
	})

	t.Run("two subscribers and a disconnect", func(t *testing.T) {
		one := objectPairs(pyjson.Object{{Key: "kind", Value: "phase"}, {Key: "summary", Value: "one"}, {Key: "worker", Value: "planner"}})
		two := objectPairs(pyjson.Object{{Key: "kind", Value: "phase"}, {Key: "summary", Value: "two"}, {Key: "worker", Value: "planner"}})
		py := ora.play(t, pyPlay{
			Task: activityStreamTask, Query: "since=0", Clients: 2,
			Steps: []pyStep{
				{Do: "arm", N: 2},
				{Do: "take", Client: 0}, {Do: "take", Client: 1},
				{Do: "publish", Event: one},
				{Do: "arm", N: 2},
				{Do: "take", Client: 0}, {Do: "take", Client: 1},
				{Do: "drop", Client: 0},
				{Do: "subs", N: 2},
				{Do: "publish", Event: two},
				{Do: "arm", N: 1},
				{Do: "take", Client: 1},
				{Do: "subs", N: 1},
			},
		})
		if len(py.Marks) != 2 || py.Marks[0] != 2 || py.Marks[1] != 1 {
			t.Fatalf("python subs %v", py.Marks)
		}
		srv, _, addr := startActivity(t, &streamReads{}, nil)
		first := dialActivity(t, addr, activityStreamTask, "since=0")
		second := dialActivity(t, addr, activityStreamTask, "since=0")
		readEqual(t, first, py.Parts[0])
		readEqual(t, second, py.Parts[1])
		waitCount(t, srv.Activity, activityStreamTask, 2)
		srv.Activity.Publish(activityStreamTask, pyjson.Object{{Key: "kind", Value: "phase"}, {Key: "summary", Value: "one"}, {Key: "worker", Value: "planner"}})
		readEqual(t, first, py.Parts[2])
		readEqual(t, second, py.Parts[3])
		closeNow(first)
		waitCount(t, srv.Activity, activityStreamTask, 1)
		srv.Activity.Publish(activityStreamTask, pyjson.Object{{Key: "kind", Value: "phase"}, {Key: "summary", Value: "two"}, {Key: "worker", Value: "planner"}})
		readEqual(t, second, py.Parts[4])
	})

	t.Run("disconnect unsubscribes", func(t *testing.T) {
		py := ora.play(t, pyPlay{
			Task: activityStreamTask, Query: "since=0",
			Steps: []pyStep{
				{Do: "arm"},
				{Do: "subs", N: 1},
				{Do: "drop"},
				{Do: "subs", N: 1},
				{Do: "publish", Event: liveEvent},
				{Do: "subs", N: 0},
			},
		})
		// Python drops the subscriber in finally after the next write fails.
		// Go's peer watch cancels the stream as soon as the socket closes.
		if len(py.Marks) != 3 || py.Marks[0] != 1 || py.Marks[1] != 1 || py.Marks[2] != 0 {
			t.Fatalf("python marks %v", py.Marks)
		}
		srv, _, addr := startActivity(t, &streamReads{}, nil)
		conn := dialActivity(t, addr, activityStreamTask, "since=0")
		_ = readSSEHeaders(t, conn)
		waitCount(t, srv.Activity, activityStreamTask, 1)
		closeNow(conn)
		waitCount(t, srv.Activity, activityStreamTask, 0)
	})
}

func mustSinces(t *testing.T, py pyResult) []int64 {
	t.Helper()
	out := make([]int64, len(py.Calls))
	for i, call := range py.Calls {
		n, err := strconv.ParseInt(call.Since, 10, 64)
		if err != nil {
			t.Fatal(err)
		}
		out[i] = n
	}
	return out
}

func mustLimits(py pyResult) []int64 {
	out := make([]int64, len(py.Calls))
	for i, call := range py.Calls {
		out[i] = int64(call.Limit)
	}
	return out
}
