package httpapi

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strings"
	"testing"
)

func TestScheduleLimitDoesNotRead(t *testing.T) {
	reads := &recordingReads{}
	h := New("dev", &fakeDB{}, "")
	h.Reads = reads
	handler := h.Handler()
	digits := strings.Repeat("9", 4301)
	tests := []struct {
		name   string
		path   string
		status int
		body   string
	}{
		{name: "zero", path: "/api/schedules/sched-nightly01/runs?limit=0", status: http.StatusBadRequest, body: `{"error": "limit must be a whole number from 1 to 200"}`},
		{name: "skips", path: "/api/schedules/sched-nightly01/runs?skips=nope", status: http.StatusBadRequest, body: `{"error": "skips must be one of show, hide"}`},
		{name: "superscript", path: "/api/schedules/sched-nightly01/runs?limit=%C2%B2", status: http.StatusInternalServerError, body: `{"error": "internal server error"}`},
		{name: "overflow", path: "/api/schedules/sched-nightly01/runs?limit=" + digits, status: http.StatusInternalServerError, body: `{"error": "internal server error"}`},
		{name: "notes overflow", path: "/api/schedules/sched-nightly01/notifications?limit=" + digits, status: http.StatusInternalServerError, body: `{"error": "internal server error"}`},
		{name: "preview", path: "/api/schedules/preview", status: http.StatusNotFound, body: `{"error": "not found"}`},
		{name: "post", path: "/api/schedules/sched-nightly01/runs", status: http.StatusNotFound, body: `{"error": "not found"}`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			logs := captureLogs(t)
			method := http.MethodGet
			if tt.name == "post" {
				method = http.MethodPost
			}
			rr := httptest.NewRecorder()
			handler.ServeHTTP(rr, loopbackRequest(method, tt.path))
			if rr.Code != tt.status || rr.Body.String() != tt.body {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			if strings.Contains(rr.Body.String(), digits) || strings.Contains(logs.String(), digits) {
				t.Fatal("response or log contains the digit string")
			}
		})
	}
	if len(reads.calls) != 0 {
		t.Fatalf("a refused schedule read called the store: %v", reads.calls)
	}
}

func TestScheduleLimitParses(t *testing.T) {
	tests := []struct {
		raw    string
		n      int
		status int
	}{
		{raw: "", n: 50},
		{raw: "1", n: 1},
		{raw: "01", n: 1},
		{raw: "200", n: 200},
		{raw: "0", status: http.StatusBadRequest},
		{raw: "201", status: http.StatusBadRequest},
		{raw: "nope", status: http.StatusBadRequest},
		{raw: "²", status: http.StatusInternalServerError},
		{raw: "②", status: http.StatusInternalServerError},
		{raw: strings.Repeat("9", 4300), status: http.StatusBadRequest},
		{raw: strings.Repeat("9", 4301), status: http.StatusInternalServerError},
	}
	for _, tt := range tests {
		n, status, err := scheduleLimit(tt.raw)
		if tt.status == 0 {
			if err != nil || n != tt.n {
				t.Fatalf("limit %q = %d %v, want %d", tt.raw, n, err, tt.n)
			}
			continue
		}
		if status != tt.status || err == nil {
			t.Fatalf("limit %q status %d err %v, want %d", tt.raw, status, err, tt.status)
		}
	}
}

func TestWorkspaceCheckRefusesBeforeStore(t *testing.T) {
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", t.TempDir())
	reads := &recordingReads{}
	h := New("dev", &fakeDB{}, "")
	h.Reads = reads
	handler := h.Handler()
	tests := []struct {
		name string
		path string
		body string
	}{
		{name: "no dir", path: "/api/workspaces/check", body: `{"error": "a work directory is required: set one on the task or on its project"}`},
		{name: "relative", path: "/api/workspaces/check?dir=relative&mode=nope", body: `{"error": "work directory must be an absolute path, got 'relative'"}`},
		{name: "bad mode", path: "/api/workspaces/check?dir=/loopgo-parity-absent-workspace&mode=nope", body: `{"error": "workspace_mode must be one of in_place, worktree"}`},
		{name: "base", path: "/api/workspaces/check?dir=/loopgo-parity-absent-workspace&mode=in_place&base=main", body: `{"error": "base is where a task's worktree starts: give it with workspace_mode worktree"}`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rr := httptest.NewRecorder()
			handler.ServeHTTP(rr, loopbackRequest(http.MethodGet, tt.path))
			if rr.Code != http.StatusBadRequest || rr.Body.String() != tt.body {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
		})
	}
	if len(reads.calls) != 0 {
		t.Fatalf("a refused workspace check called the store: %v", reads.calls)
	}
}

func TestWorkspaceCheckAbsentDir(t *testing.T) {
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", t.TempDir())
	reads := &recordingReads{}
	h := New("dev", &fakeDB{}, "")
	h.Reads = reads
	rr := httptest.NewRecorder()
	h.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/workspaces/check?dir=/loopgo-parity-absent-workspace"))
	if rr.Code != http.StatusOK {
		t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["mode"] != "in_place" || body["requested_mode"] != "worktree" || body["default_mode"] != "worktree" || body["git"] != nil {
		t.Fatalf("body %#v", body)
	}
	warnings, _ := body["warnings"].([]any)
	if len(warnings) != 1 {
		t.Fatalf("warnings %#v", body["warnings"])
	}
	warning := warnings[0].(map[string]any)
	if warning["kind"] != "not_git" || !strings.Contains(warning["text"].(string), "by default") {
		t.Fatalf("warning %#v", warning)
	}
	if len(reads.calls) != 1 || reads.calls[0] != "active-workspaces" {
		t.Fatalf("calls %v", reads.calls)
	}
}

func TestWorkspaceCheckGit(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git is not installed")
	}
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", t.TempDir())
	dir := t.TempDir()
	git := func(args ...string) {
		t.Helper()
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	git("init", "-b", "main")
	git("config", "user.email", "looper@localhost")
	git("config", "user.name", "Looper")
	if err := os.WriteFile(filepath.Join(dir, "readme.txt"), []byte("hi\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	git("add", "readme.txt")
	git("commit", "-m", "init")
	headCmd := exec.Command("git", "-C", dir, "rev-parse", "HEAD")
	headOut, err := headCmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	head := strings.TrimSpace(string(headOut))

	reads := &recordingReads{}
	srv := New("dev", &fakeDB{}, "")
	srv.Reads = reads
	q := url.Values{}
	q.Set("dir", dir)
	q.Set("mode", "worktree")
	rr := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/workspaces/check?"+q.Encode()))
	if rr.Code != http.StatusOK {
		t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rr.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["mode"] != "worktree" || body["workspace"] != dir {
		t.Fatalf("body %#v", body)
	}
	gitBody, _ := body["git"].(map[string]any)
	if gitBody["head"] != head || gitBody["branch"] != "main" || gitBody["base_commit"] != head {
		t.Fatalf("git %#v want head %s", gitBody, head)
	}
	warnings, _ := body["warnings"].([]any)
	if len(warnings) != 0 {
		t.Fatalf("warnings %#v", warnings)
	}

	q.Set("base", "not-a-ref")
	rr = httptest.NewRecorder()
	srv.Handler().ServeHTTP(rr, loopbackRequest(http.MethodGet, "/api/workspaces/check?"+q.Encode()))
	if rr.Code != http.StatusBadRequest || !strings.Contains(rr.Body.String(), "not a branch, tag or commit") {
		t.Fatalf("base status %d body %s", rr.Code, rr.Body.String())
	}
}

func TestOperatorNotesMissing(t *testing.T) {
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", t.TempDir())
	view := notesView(t.Context(), "/loopgo-parity-absent-workspace")
	if view["found"] != false {
		t.Fatalf("found %#v", view)
	}
	files, _ := view["files"].([]string)
	omitted, _ := view["omitted"].([]string)
	if len(files) != 0 || len(omitted) != 0 || view["error"] != nil {
		t.Fatalf("view %#v", view)
	}
}

func TestOperatorNotesIncludesEmptyLinkedFile(t *testing.T) {
	root := t.TempDir()
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", root)
	work := t.TempDir()
	mem := filepath.Join(root, directoryName(notesKey(t.Context(), work)), "memory")
	if err := os.MkdirAll(mem, 0o755); err != nil {
		t.Fatal(err)
	}
	index := "see [empty](empty.md) and [gone](gone.md)\n"
	if err := os.WriteFile(filepath.Join(mem, "MEMORY.md"), []byte(index), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(mem, "empty.md"), []byte{}, 0o644); err != nil {
		t.Fatal(err)
	}
	view := notesView(t.Context(), work)
	if view["found"] != true {
		t.Fatalf("view %#v", view)
	}
	if strings.Join(view["files"].([]string), ",") != "MEMORY.md,empty.md" {
		t.Fatalf("files %#v", view["files"])
	}
	if strings.Join(view["omitted"].([]string), ",") != "gone.md" {
		t.Fatalf("omitted %#v", view["omitted"])
	}
}

func TestOperatorNotesLineBreaksMatchPython(t *testing.T) {
	// 201 lines. The link on the last line is past the 200-line index.
	// CR is a universal newline. U+001C is a splitlines boundary and not one.
	breaks := []struct {
		name string
		sep  string
	}{
		{name: "cr", sep: "\r"},
		{name: "file separator", sep: "\x1c"},
	}
	for _, tt := range breaks {
		t.Run(tt.name, func(t *testing.T) {
			root := t.TempDir()
			t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", root)
			work := t.TempDir()
			mem := filepath.Join(root, directoryName(notesKey(t.Context(), work)), "memory")
			if err := os.MkdirAll(mem, 0o755); err != nil {
				t.Fatal(err)
			}
			var b strings.Builder
			b.WriteString("see [early](early.md)")
			for i := 1; i < notesIndexLines; i++ {
				b.WriteString(tt.sep + "line")
			}
			b.WriteString(tt.sep + "see [late](late.md)")
			if err := os.WriteFile(filepath.Join(mem, "MEMORY.md"), []byte(b.String()), 0o644); err != nil {
				t.Fatal(err)
			}
			notes := readOperatorNotes(t.Context(), work)
			if strings.Contains(notes.text, "late.md") || strings.Contains(strings.Join(notes.omitted, ","), "late.md") {
				t.Fatalf("line 201 was included: text %q omitted %v", notes.text, notes.omitted)
			}
			if !strings.Contains(notes.text, "early.md") && !strings.Contains(strings.Join(notes.omitted, ","), "early.md") {
				t.Fatalf("line 1 was dropped: text %q omitted %v", notes.text, notes.omitted)
			}
		})
	}
}

func TestOperatorNotesUnreadableHidesPath(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can read a mode 000 file")
	}
	root := t.TempDir()
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", root)
	work := t.TempDir()
	mem := filepath.Join(root, directoryName(notesKey(t.Context(), work)), "memory")
	if err := os.MkdirAll(mem, 0o755); err != nil {
		t.Fatal(err)
	}
	index := filepath.Join(mem, "MEMORY.md")
	if err := os.WriteFile(index, []byte("a note\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(index, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(index, 0o644) })
	view := notesView(t.Context(), work)
	errText, _ := view["error"].(string)
	if errText != "cannot read the operator's notes: Permission denied" {
		t.Fatalf("error %q", errText)
	}
	if strings.Contains(errText, root) || strings.Contains(errText, index) {
		t.Fatalf("error contains a filesystem path: %q", errText)
	}
	if view["found"] != false {
		t.Fatalf("found %#v", view["found"])
	}
}

func TestOperatorNotesUnsearchableDirectory(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root ignores directory search permission")
	}
	root := t.TempDir()
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", root)
	work := t.TempDir()
	mem := filepath.Join(root, directoryName(notesKey(t.Context(), work)), "memory")
	if err := os.MkdirAll(mem, 0o755); err != nil {
		t.Fatal(err)
	}
	index := filepath.Join(mem, "MEMORY.md")
	if err := os.WriteFile(index, []byte("see [secret](secret.md)\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// The directory still stats. Looking up MEMORY.md needs search permission.
	if err := os.Chmod(mem, 0o644); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(mem, 0o755) })
	view := notesView(t.Context(), work)
	errText, _ := view["error"].(string)
	if errText != "cannot read the operator's notes: Permission denied" {
		t.Fatalf("error %q view %#v", errText, view)
	}
	if strings.Contains(errText, root) || strings.Contains(errText, mem) || strings.Contains(errText, index) {
		t.Fatalf("error contains a filesystem path: %q", errText)
	}
	if view["found"] != false {
		t.Fatalf("found %#v", view["found"])
	}
}

func TestOperatorNotesByteBudgetMatchesPython(t *testing.T) {
	// Python's prefix ends with one newline. [n](n.md) plus a 24,143-byte
	// n.md fills 24 KiB exactly, so the file is included. 24,144 is omitted.
	root := t.TempDir()
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", root)
	work := t.TempDir()
	mem := filepath.Join(root, directoryName(notesKey(t.Context(), work)), "memory")
	if err := os.MkdirAll(mem, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(mem, "MEMORY.md"), []byte("[n](n.md)\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	write := func(n int) operatorNotes {
		t.Helper()
		body := strings.Repeat("x", n)
		if err := os.WriteFile(filepath.Join(mem, "n.md"), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		return readOperatorNotes(t.Context(), work)
	}
	included := write(24143)
	if strings.Join(included.files, ",") != "MEMORY.md,n.md" || len(included.omitted) != 0 {
		t.Fatalf("24143 files %v omitted %v", included.files, included.omitted)
	}
	if strings.Count(included.text, "x") != 24143 {
		t.Fatalf("included %d x", strings.Count(included.text, "x"))
	}
	if len([]byte(included.text)) > notesMaxBytes {
		t.Fatalf("text is %d bytes", len(included.text))
	}
	omitted := write(24144)
	if strings.Join(omitted.files, ",") != "MEMORY.md" || strings.Join(omitted.omitted, ",") != "n.md" {
		t.Fatalf("24144 files %v omitted %v", omitted.files, omitted.omitted)
	}
	if strings.Contains(omitted.text, "xxx") || !strings.Contains(omitted.text, "Not included") {
		t.Fatal("24144-byte file was included")
	}
}

func TestOperatorNotesNormalizesNewlinesBeforeBudget(t *testing.T) {
	// Path.read_text turns \r\n into \n before the byte budget is applied.
	// "a\r\n" × 7000 is 21,000 raw bytes and 14,000 after that translation.
	// Counting the raw size omits a 5,000-byte second file that still fits.
	root := t.TempDir()
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", root)
	work := t.TempDir()
	mem := filepath.Join(root, directoryName(notesKey(t.Context(), work)), "memory")
	if err := os.MkdirAll(mem, 0o755); err != nil {
		t.Fatal(err)
	}
	index := "[a](a.md)\n[b](b.md)\n"
	if err := os.WriteFile(filepath.Join(mem, "MEMORY.md"), []byte(index), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(mem, "a.md"), []byte(strings.Repeat("a\r\n", 7000)), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(mem, "b.md"), []byte(strings.Repeat("b", 5000)), 0o644); err != nil {
		t.Fatal(err)
	}
	got := readOperatorNotes(t.Context(), work)
	if strings.Join(got.files, ",") != "MEMORY.md,a.md,b.md" || len(got.omitted) != 0 {
		t.Fatalf("files %v omitted %v", got.files, got.omitted)
	}
	if strings.Contains(got.text, "\r") {
		t.Fatal("linked note kept a carriage return")
	}
	if !strings.Contains(got.text, strings.TrimSpace(strings.Repeat("a\n", 7000))) || !strings.Contains(got.text, strings.Repeat("b", 5000)) {
		t.Fatal("normalized text lost a linked file")
	}
}

func TestExpandUserNamedAccount(t *testing.T) {
	account, err := user.Current()
	if err != nil {
		t.Fatal(err)
	}
	if account.Username == "" || !strings.HasPrefix(account.HomeDir, "/") {
		t.Fatalf("current user %#v", account)
	}
	home := t.TempDir()
	t.Setenv("HOME", home)

	got, err := resolveWorkDir("~" + account.Username)
	if err != nil {
		t.Fatal(err)
	}
	if got != pathClean(account.HomeDir) {
		t.Fatalf("~user %q, want %q", got, pathClean(account.HomeDir))
	}
	got, err = resolveWorkDir("~" + account.Username + "/proj")
	if err != nil {
		t.Fatal(err)
	}
	if got != pathClean(account.HomeDir+"/proj") {
		t.Fatalf("~user/proj %q, want %q", got, pathClean(account.HomeDir+"/proj"))
	}
	if expandUser("~/proj") != home+"/proj" || expandUser("~") != home {
		t.Fatalf("tilde home %q %q, want %q", expandUser("~"), expandUser("~/proj"), home)
	}
	_, err = resolveWorkDir("~nosuchuser-loopgo-xyz/proj")
	if err == nil || !strings.Contains(err.Error(), "got '~nosuchuser-loopgo-xyz/proj'") {
		t.Fatalf("unknown user: %v", err)
	}
}

func TestOperatorNotesUsesClaudeConfigDir(t *testing.T) {
	home := t.TempDir()
	config := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", "")
	t.Setenv("CLAUDE_CONFIG_DIR", config)
	work := t.TempDir()
	name := directoryName(notesKey(t.Context(), work))
	writeNotes := func(dir, linked string) {
		t.Helper()
		mem := filepath.Join(dir, "projects", name, "memory")
		if err := os.MkdirAll(mem, 0o755); err != nil {
			t.Fatal(err)
		}
		body := "see [n](" + linked + ")\n"
		if err := os.WriteFile(filepath.Join(mem, "MEMORY.md"), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(mem, linked), []byte("x\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	writeNotes(config, "from-config.md")
	writeNotes(filepath.Join(home, ".claude"), "from-home.md")
	view := notesView(t.Context(), work)
	if strings.Join(stringSlice(view["files"]), ",") != "MEMORY.md,from-config.md" {
		t.Fatalf("view %#v", view)
	}
}

func TestOperatorNotesLongKeyUsesMultiplier31(t *testing.T) {
	root := t.TempDir()
	t.Setenv("LOOPER_OPERATOR_NOTES_ROOT", root)
	work := longNotesWorkDir(t)
	key := notesKey(t.Context(), work)
	plain := notesPlainName(key)
	if len(plain) <= notesNameMax {
		t.Fatalf("key %q sanitizes to %d characters", key, len(plain))
	}
	right := plain[:notesNameMax] + "-" + base36(absInt(notesHash(key, 31)))
	wrong := plain[:notesNameMax] + "-" + base36(absInt(notesHash(key, 33)))
	if right == wrong {
		t.Fatal("multipliers 31 and 33 produced the same directory")
	}
	writeHashed := func(dir, linked string) {
		t.Helper()
		mem := filepath.Join(root, dir, "memory")
		if err := os.MkdirAll(mem, 0o755); err != nil {
			t.Fatal(err)
		}
		body := "see [n](" + linked + ")\n"
		if err := os.WriteFile(filepath.Join(mem, "MEMORY.md"), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(mem, linked), []byte("x\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	writeHashed(right, "from-31.md")
	writeHashed(wrong, "from-33.md")
	view := notesView(t.Context(), work)
	if strings.Join(stringSlice(view["files"]), ",") != "MEMORY.md,from-31.md" {
		t.Fatalf("view %#v", view)
	}
}

func longNotesWorkDir(t *testing.T) string {
	t.Helper()
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	need := notesNameMax + 1 - len(base) - 1
	if need < 1 {
		need = 1
	}
	if need > 200 {
		t.Fatalf("temp dir %q leaves %d characters, above one path component", base, need)
	}
	dir := filepath.Join(base, strings.Repeat("a", need))
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	return dir
}

func notesPlainName(key string) string {
	var b strings.Builder
	for _, r := range key {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			continue
		}
		b.WriteByte('-')
	}
	return b.String()
}

func notesHash(text string, multiplier uint32) int32 {
	var value uint32
	data := utf16LE(text)
	for i := 0; i+1 < len(data); i += 2 {
		unit := uint32(data[i]) | uint32(data[i+1])<<8
		value = value*multiplier + unit
	}
	return int32(value)
}

func stringSlice(v any) []string {
	switch typed := v.(type) {
	case []string:
		return typed
	case []any:
		out := make([]string, len(typed))
		for i, item := range typed {
			out[i], _ = item.(string)
		}
		return out
	default:
		return nil
	}
}
