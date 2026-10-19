package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
)

func TestGateAndRoomRouting(t *testing.T) {
	reads := &recordingReads{}
	h := tailHandler(reads)
	tests := []struct {
		method, path string
	}{
		{http.MethodPost, "/api/tasks/task-rich00001/gates/g1/messages"},
		{http.MethodPut, "/api/tasks/task-rich00001/gates/g1/end"},
		{http.MethodHead, "/api/tasks/task-rich00001/room"},
		{http.MethodGet, "/api/tasks/task-%61bc/gates/g1/messages"},
		{http.MethodGet, "/api/tasks/task-rich00001/gates/nope!/messages"},
		{http.MethodGet, "/api/tasks/task-rich00001/gates/g%31/messages"},
		{http.MethodGet, "/api/tasks/task-rich00001/gates/" + strings.Repeat("a", 33) + "/messages"},
		{http.MethodGet, "/api/tasks/task-%61bc/room"},
		{http.MethodPost, "/api/tasks/task-rich00001/room"},
	}
	for _, tt := range tests {
		t.Run(tt.method+" "+tt.path, func(t *testing.T) {
			before := len(reads.calls)
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(tt.method, tt.path))
			if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"not found"}` {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if len(reads.calls) != before {
				t.Fatalf("calls %v", reads.calls)
			}
		})
	}
}

func TestGateAndRoomNilReads(t *testing.T) {
	h := tailHandler(nil)
	tests := []struct {
		path, body string
		status     int
	}{
		{path: "/api/tasks/task-rich00001/gates/g1/messages", status: http.StatusInternalServerError, body: `{"error":"internal server error"}`},
		{path: "/api/tasks/task-rich00001/room", status: http.StatusInternalServerError, body: `{"error":"internal server error"}`},
		{path: "/api/tasks/task-missing01/room?limit=nope", status: http.StatusBadRequest, body: `{"error":"limit must be a whole number"}`},
		{path: "/api/tasks/task-missing01/room?after=nope", status: http.StatusBadRequest, body: `{"error":"after must be a whole number"}`},
	}
	for _, tt := range tests {
		t.Run(tt.path, func(t *testing.T) {
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(http.MethodGet, tt.path))
			if rr.Code != tt.status || rr.Body.String() != tt.body {
				t.Fatalf("status %d body %s, want %d %s", rr.Code, rr.Body.String(), tt.status, tt.body)
			}
		})
	}
}

func TestGateAndRoomResults(t *testing.T) {
	secret := errors.New("SECRET")
	missing := &recordingReads{gateMessagesErr: db.ErrGateNotFound}
	rr := httptest.NewRecorder()
	tailHandler(missing).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-missing01/gates/g1/messages"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"task task-missing01 has no gate g1"}` {
		t.Fatalf("gate 404 %d %s", rr.Code, rr.Body.String())
	}
	if missing.gateTask != "task-missing01" || missing.gateLabel != "g1" {
		t.Fatalf("gate ids %s %s", missing.gateTask, missing.gateLabel)
	}

	logs := captureLogs(t)
	broken := &recordingReads{gateMessagesErr: secret, roomErr: secret}
	rr = httptest.NewRecorder()
	tailHandler(broken).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/gates/g1/messages"))
	if rr.Code != http.StatusInternalServerError || strings.Contains(rr.Body.String(), "SECRET") || strings.Contains(logs.String(), "SECRET") {
		t.Fatalf("gate leak status %d body %s log %s", rr.Code, rr.Body.String(), logs.String())
	}
	rr = httptest.NewRecorder()
	tailHandler(broken).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/room"))
	if rr.Code != http.StatusInternalServerError || strings.Contains(rr.Body.String(), "SECRET") || strings.Contains(logs.String(), "SECRET") {
		t.Fatalf("room leak status %d body %s log %s", rr.Code, rr.Body.String(), logs.String())
	}

	gone := &recordingReads{roomErr: db.ErrNotFound}
	rr = httptest.NewRecorder()
	tailHandler(gone).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-missing01/room"))
	if rr.Code != http.StatusNotFound || rr.Body.String() != `{"error":"task not found"}` {
		t.Fatalf("room 404 %d %s", rr.Code, rr.Body.String())
	}
}

func TestRoomQuery(t *testing.T) {
	reads := &recordingReads{roomBody: map[string]any{"enabled": true}}
	h := tailHandler(reads)
	tests := []struct {
		name    string
		path    string
		status  int
		body    string
		want    db.RoomQuery
		noStore bool
	}{
		{name: "defaults", path: "/api/tasks/task-rich00001/room", status: http.StatusOK, want: db.RoomQuery{Limit: 500}},
		{name: "blank limit", path: "/api/tasks/task-rich00001/room?limit=", status: http.StatusOK, want: db.RoomQuery{Limit: 500}},
		{name: "limit zero", path: "/api/tasks/task-rich00001/room?limit=0", status: http.StatusOK, want: db.RoomQuery{Limit: 1}},
		{name: "limit cap", path: "/api/tasks/task-rich00001/room?limit=501", status: http.StatusOK, want: db.RoomQuery{Limit: 500}},
		{name: "fullwidth digit", path: "/api/tasks/task-rich00001/room?limit=２", status: http.StatusOK, want: db.RoomQuery{Limit: 2}},
		{name: "filters", path: "/api/tasks/task-rich00001/room?kind=question&from=backend&unanswered=1&unanswered=no&after=1", status: http.StatusOK, want: db.RoomQuery{Kind: "question", Sender: "backend", After: 1, Limit: 500}},
		{name: "unanswered last", path: "/api/tasks/task-rich00001/room?unanswered=no&unanswered=true", status: http.StatusOK, want: db.RoomQuery{Unanswered: true, Limit: 500}},
		{name: "after past int64", path: "/api/tasks/task-rich00001/room?after=" + strings.Repeat("9", pythonIntMaxStrDigits), status: http.StatusOK, want: db.RoomQuery{AfterTooBig: true, Limit: 500}},
		{name: "limit past int64", path: "/api/tasks/task-rich00001/room?limit=" + strings.Repeat("9", pythonIntMaxStrDigits), status: http.StatusOK, want: db.RoomQuery{Limit: 500}},
		{name: "file separator", path: "/api/tasks/task-rich00001/room?limit=%1C2", status: http.StatusBadRequest, body: `{"error":"limit must be a whole number"}`, noStore: true},
		{name: "space", path: "/api/tasks/task-rich00001/room?after=%202", status: http.StatusBadRequest, body: `{"error":"after must be a whole number"}`, noStore: true},
		{name: "underscore", path: "/api/tasks/task-rich00001/room?limit=1_2", status: http.StatusBadRequest, body: `{"error":"limit must be a whole number"}`, noStore: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reads.calls = nil
			reads.roomQuery = db.RoomQuery{}
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(http.MethodGet, tt.path))
			if rr.Code != tt.status {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if tt.body != "" && rr.Body.String() != tt.body {
				t.Fatalf("body %s", rr.Body.String())
			}
			if tt.noStore {
				if len(reads.calls) != 0 {
					t.Fatalf("calls %v", reads.calls)
				}
				return
			}
			if len(reads.calls) != 1 || reads.roomTask != "task-rich00001" || reads.roomQuery != tt.want {
				t.Fatalf("task %s query %#v calls %v", reads.roomTask, reads.roomQuery, reads.calls)
			}
		})
	}
}

func TestRoomDigitLimit(t *testing.T) {
	reads := &recordingReads{}
	logs := captureLogs(t)
	raw := strings.Repeat("9", pythonIntMaxStrDigits+1)
	rr := httptest.NewRecorder()
	tailHandler(reads).ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/room?after="+raw))
	if rr.Code != http.StatusInternalServerError || rr.Body.String() != `{"error":"internal server error"}` {
		t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
	}
	if len(reads.calls) != 0 {
		t.Fatalf("calls %v", reads.calls)
	}
	if strings.Contains(logs.String(), raw) || strings.Contains(rr.Body.String(), raw) {
		t.Fatal("digit string was logged")
	}
	if !strings.Contains(logs.String(), "exceeds the digit limit") {
		t.Fatalf("log %s", logs.String())
	}
}

// roomDigitCase is one raw query value classified by CPython: str.isdigit,
// then int(). 400 is RequestError. 500 is the ValueError the request loop
// turns into an internal error. 200 carries the int() value before clamping.
type roomDigitCase struct {
	Name    string `json:"name"`
	Key     string `json:"key"`
	Raw     string `json:"raw"`
	Encoded string `json:"encoded"`
	Status  int    `json:"status"`
	Value   *int64 `json:"value"`
}

func TestRoomPythonDigits(t *testing.T) {
	cases := roomDigitCases(t)
	if len(cases) < 100 {
		t.Fatalf("oracle returned %d cases", len(cases))
	}
	var superscript, circled, nag, kawi bool
	for _, tc := range cases {
		if tc.Key == "limit" && tc.Raw == "²" {
			superscript = tc.Status == http.StatusInternalServerError && tc.Encoded == "%C2%B2"
		}
		if tc.Key == "limit" && tc.Raw == "②" {
			circled = tc.Status == http.StatusInternalServerError && tc.Encoded == "%E2%91%A1"
		}
		// U+1E4F2 and U+11F50 are Nd in Unicode 15 and unassigned in Unicode 14.
		if tc.Key == "limit" && tc.Raw == "\U0001E4F2" {
			nag = tc.Status == http.StatusBadRequest && tc.Encoded == "%F0%9E%93%B2"
		}
		if tc.Key == "limit" && tc.Raw == "\U00011F50" {
			kawi = tc.Status == http.StatusBadRequest && tc.Encoded == "%F0%91%BD%90"
		}
	}
	if !superscript || !circled || !nag || !kawi {
		t.Fatalf("oracle superscript %v circled %v nag %v kawi %v", superscript, circled, nag, kawi)
	}

	logs := captureLogs(t)
	reads := &recordingReads{roomBody: map[string]any{"enabled": true}}
	h := tailHandler(reads)
	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			reads.calls = nil
			reads.roomQuery = db.RoomQuery{}
			rr := httptest.NewRecorder()
			h.ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/tasks/task-rich00001/room?"+tc.Key+"="+tc.Encoded))
			if rr.Code != tc.Status {
				t.Fatalf("status %d body %s, python %d", rr.Code, rr.Body.String(), tc.Status)
			}
			switch tc.Status {
			case http.StatusBadRequest:
				want := `{"error":"` + tc.Key + ` must be a whole number"}`
				if rr.Body.String() != want || len(reads.calls) != 0 {
					t.Fatalf("body %s calls %v", rr.Body.String(), reads.calls)
				}
			case http.StatusInternalServerError:
				if rr.Body.String() != `{"error":"internal server error"}` || len(reads.calls) != 0 {
					t.Fatalf("body %s calls %v", rr.Body.String(), reads.calls)
				}
			case http.StatusOK:
				if tc.Value == nil {
					t.Fatal("python 200 without a value")
				}
				want := db.RoomQuery{Limit: db.RoomViewLimit}
				switch tc.Key {
				case "limit":
					n := *tc.Value
					switch {
					case n > int64(db.RoomViewLimit):
						want.Limit = db.RoomViewLimit
					case n < 1:
						want.Limit = 1
					default:
						want.Limit = int(n)
					}
				case "after":
					want.After = *tc.Value
				default:
					t.Fatalf("key %s", tc.Key)
				}
				if len(reads.calls) != 1 || reads.roomTask != "task-rich00001" || reads.roomQuery != want {
					t.Fatalf("task %s query %#v calls %v, python %d", reads.roomTask, reads.roomQuery, reads.calls, *tc.Value)
				}
			default:
				t.Fatalf("python status %d", tc.Status)
			}
		})
	}
	if strings.Contains(logs.String(), "²") || strings.Contains(logs.String(), "②") {
		t.Fatal("room digit text was logged")
	}
}

func roomDigitCases(t *testing.T) []roomDigitCase {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, roomPython(t), "-c", roomDigitOracle)
	out, err := cmd.Output()
	if err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			t.Fatalf("python oracle: %v\n%s", err, exit.Stderr)
		}
		t.Fatalf("python oracle: %v", err)
	}
	var cases []roomDigitCase
	if err := json.Unmarshal(out, &cases); err != nil {
		t.Fatalf("oracle json: %v", err)
	}
	return cases
}

// roomPython is the worktree venv, the interpreter the Python server uses.
// A checkout without that venv, such as CI, uses python3. Decimal digits
// added after Unicode 14 are pinned to CPython 3.11 below, so a newer
// interpreter does not reclassify them.
func roomPython(t *testing.T) string {
	t.Helper()
	venv := filepath.Join("..", "..", "..", ".venv", "bin", "python")
	if st, err := os.Stat(venv); err == nil && !st.IsDir() {
		return venv
	}
	bin, err := exec.LookPath("python3")
	if err != nil {
		t.Fatal("python3 is required for the room digit oracle")
	}
	return bin
}

// roomDigitOracle classifies query text the way _task_room.number does.
// str.isdigit then int(). ValueError is the request loop's 500. The encoded
// field is urllib.parse.quote of the raw value, which is the query Go parses.
const roomDigitOracle = `
import json, sys, unicodedata, urllib.parse

def outcome(raw):
    if not raw.isdigit():
        return 400, None
    try:
        return 200, int(raw)
    except ValueError:
        return 500, None

def add(cases, name, key, raw):
    status, value = outcome(raw)
    cases.append({
        "name": name,
        "key": key,
        "raw": raw,
        "encoded": urllib.parse.quote(raw, safe=""),
        "status": status,
        "value": value,
    })

cases = []
# Numeric_Type=Digit: isdigit is true and int() raises. The same 128
# characters in CPython 3.11 and 3.14.
for cp in range(0x110000):
    ch = chr(cp)
    if ch.isdigit() and unicodedata.category(ch) != "Nd":
        for key in ("limit", "after"):
            add(cases, "U+%04X %s" % (cp, key), key, ch)

# Nd added in Unicode 15.0: Kawi U+11F50–U+11F59 and Nag Mundari
# U+1E4F0–U+1E4F9. CPython 3.11 (Unicode 14) does not treat them as digits,
# so the room answers 400. A newer interpreter's str.isdigit is true and
# int() succeeds; the reference is still 3.11, including U+1E4F2
# (?limit=%F0%9E%93%B2).
later_nd = list(range(0x11F50, 0x11F5A)) + list(range(0x1E4F0, 0x1E4FA))
for cp in later_nd:
    ch = chr(cp)
    if unicodedata.unidata_version == "14.0.0":
        status, value = outcome(ch)
        if status != 400 or value is not None:
            raise SystemExit("U+%04X reference status %s" % (cp, status))
    for key in ("limit", "after"):
        cases.append({
            "name": "U+%04X %s" % (cp, key),
            "key": key,
            "raw": ch,
            "encoded": urllib.parse.quote(ch, safe=""),
            "status": 400,
            "value": None,
        })

extras = [
    ("ascii zero", "0"),
    ("ascii two", "2"),
    ("fullwidth zero", "０"),
    ("fullwidth two", "２"),
    ("arabic indic nine", "٩"),
    ("two superscripts", "²²"),
    ("mixed superscript", "2²"),
    ("mixed fullwidth", "２2"),
    ("underscore", "1_2"),
    ("space", " 2"),
    ("circled ten", "⑩"),
    ("fraction", "½"),
    ("roman", "Ⅳ"),
]
for name, raw in extras:
    for key in ("limit", "after"):
        add(cases, "%s %s" % (name, key), key, raw)

json.dump(cases, sys.stdout, ensure_ascii=False)
`
