package registry

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestTypeViews(t *testing.T) {
	views := TypeViews()
	if len(views) != 4 {
		t.Fatalf("types %d", len(views))
	}
	want := []struct {
		typ, label, exe string
		auth            string
		protocols       int
	}{
		{"claude-code", "Claude Code", "claude", "api_key", 1},
		{"codex", "Codex", "codex", "api_key", 1},
		{"omp", "OMP", "omp", "api_key", 3},
		{"grok", "Grok", "grok", "oauth", 1},
	}
	for i, row := range want {
		view := views[i].(map[string]any)
		if view["type"] != row.typ || view["label"] != row.label || view["default_executable"] != row.exe {
			t.Fatalf("type %v", view)
		}
		if view["runtime_id"] != "rt-"+row.typ || view["can_replace_system_prompt"] != true || view["replace_system_prompt_refusal"] != nil {
			t.Fatalf("flags %v", view)
		}
		auth := view["auth_kinds"].([]any)
		if auth[0] != row.auth {
			t.Fatalf("auth %v", auth)
		}
		if len(view["protocols"].([]any)) != row.protocols || len(view["roles"].([]any)) != 4 {
			t.Fatalf("protocols or roles %v", view)
		}
	}
	grok, ok := AdapterByType("grok")
	if !ok || grok.RuntimeCostCapped {
		t.Fatal("grok cap")
	}
	claude, ok := AdapterByType("claude-code")
	if !ok || !claude.RuntimeCostCapped {
		t.Fatal("claude cap")
	}
	if _, ok := AdapterByType("missing"); ok {
		t.Fatal("unknown type")
	}
}

func TestLimitsAndCatalog(t *testing.T) {
	limits := Limits()
	if limits["secret_mask"] != "***" || limits["name_max_chars"] != 240 || limits["models_max"] != 100 {
		t.Fatalf("limits %v", limits)
	}
	protocols := limits["protocols"].([]any)
	if protocols[0] != "anthropic-messages" || protocols[1] != "openai-responses" || protocols[2] != "openai-chat" {
		t.Fatalf("protocols %v", protocols)
	}
	auth := limits["auth_kinds"].([]any)
	if auth[0] != "oauth" || auth[1] != "api_key" {
		t.Fatalf("auth %v", auth)
	}
	owned := limits["provider_env_owned"].([]any)
	if len(owned) != 44 || owned[0] != "ANTHROPIC_API_KEY" {
		t.Fatalf("env owned %d %v", len(owned), owned)
	}
	roles := limits["role_capabilities"].(map[string]any)
	manager := roles["manager"].([]any)
	if len(manager) != 2 || manager[0] != "resident_session" || manager[1] != "resume" {
		t.Fatalf("manager %v", manager)
	}
	window := int64(1000)
	entries, err := CatalogEntries([]any{map[string]any{"name": "mystery-model", "context_window": window}})
	if err != nil {
		t.Fatal(err)
	}
	entry := entries[0].(map[string]any)
	if entry["context_window"] != window || entry["input_usd_per_mtok"] != nil || entry["name"] != "mystery-model" {
		t.Fatalf("catalog %#v", entry)
	}
	if _, err := CatalogEntries([]any{"nope"}); err == nil {
		t.Fatal("non-object catalog")
	}
}

func TestServableRoles(t *testing.T) {
	cases := []struct {
		name string
		caps []string
		want string
	}{
		{"all", []string{"run", "resident_session", "resume"}, "manager,planner,worker,reviewer"},
		{"run", []string{"run", "mcp"}, "worker,reviewer"},
		{"resident", []string{"resident_session", "resume"}, "manager,planner"},
		{"unknown", []string{"nope"}, ""},
		{"empty", nil, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := strings.Join(ServableRoles(tc.caps), ",")
			if got != tc.want {
				t.Fatalf("got %q want %q", got, tc.want)
			}
		})
	}
	spec := RuntimeSpecFrom("rt", "name", "custom", "", []string{"run", "nope"})
	if strings.Join(spec.Capabilities, ",") != "run" {
		t.Fatalf("spec %v", spec.Capabilities)
	}
}

func TestParseStatus(t *testing.T) {
	cases := []struct {
		name       string
		code       int
		output     string
		state      string
		detailPart string
	}{
		{"chatgpt", 0, "Logged in using ChatGPT\n", "chatgpt", "logged in using ChatGPT"},
		{"other", 0, "Logged in using API key - sk\n", "other_method", "logged in using API key, not ChatGPT"},
		{"out", 1, "noise\nNot logged in\n", "not_logged_in", "not logged in"},
		{"unknown", 2, "boom\n", "unknown", "exited 2: boom"},
		{"empty", 2, "\n", "unknown", "exited 2"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			state, detail := ParseStatus(tc.code, tc.output)
			if state != tc.state || !strings.Contains(detail, tc.detailPart) {
				t.Fatalf("state %s detail %s", state, detail)
			}
		})
	}
}

func TestProbeGrokNeverReadsFile(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "grok-home")
	if err := os.Mkdir(home, 0o755); err != nil {
		t.Fatal(err)
	}
	env := []string{"GROK_HOME=" + home, "HOME=" + root}
	state, detail := ProbeGrok(env)
	if state != "not_logged_in" || !strings.Contains(detail, filepath.Join(home, "auth.json")) || strings.Contains(detail, "SENTINEL") {
		t.Fatalf("missing %s %s", state, detail)
	}
	const sentinel = "SENTINEL-DO-NOT-LEAK"
	if err := os.WriteFile(filepath.Join(home, "auth.json"), []byte(sentinel), 0o600); err != nil {
		t.Fatal(err)
	}
	state, detail = ProbeGrok(env)
	if state != "logged_in" || strings.Contains(detail, sentinel) || !strings.HasPrefix(detail, "signed in (") {
		t.Fatalf("file %s %s", state, detail)
	}
	if err := os.Remove(filepath.Join(home, "auth.json")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(home, "auth.json"), 0o755); err != nil {
		t.Fatal(err)
	}
	state, detail = ProbeGrok(env)
	if state != "not_logged_in" || !strings.Contains(detail, "is not a file") {
		t.Fatalf("dir %s %s", state, detail)
	}
}

func TestProbeGrokFollowsSymlink(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "grok-home")
	other := filepath.Join(root, "real-auth.json")
	if err := os.Mkdir(home, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(other, []byte("SENTINEL-DO-NOT-LEAK"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(other, filepath.Join(home, "auth.json")); err != nil {
		t.Fatal(err)
	}
	state, detail := ProbeGrok([]string{"GROK_HOME=" + home, "HOME=" + root})
	if state != "logged_in" || strings.Contains(detail, "SENTINEL") {
		t.Fatalf("link %s %s", state, detail)
	}
}

func TestLoginViewSkipsProbeWhenProblemRecorded(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "grok-home")
	if err := os.Mkdir(home, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, "auth.json"), []byte("SENTINEL-DO-NOT-LEAK"), 0o600); err != nil {
		t.Fatal(err)
	}
	view := LoginView(context.Background(), "grok", []string{"GROK_HOME=" + home}, &Problem{At: "2020-01-01T00:00:00+00:00", Detail: "copied"}, nil)
	if view["state"] != "unavailable" || !strings.Contains(view["detail"], "copied") || strings.Contains(view["detail"], "signed in") {
		t.Fatalf("problem %#v", view)
	}
	broken := LoginView(context.Background(), "codex", []string{"CODEX_HOME=" + root}, nil, os.ErrClosed)
	if broken["state"] != "unknown" || !strings.Contains(broken["detail"], "cannot be read") {
		t.Fatalf("read %#v", broken)
	}
	bad := LoginView(context.Background(), "grok", nil, &Problem{Unreadable: true}, nil)
	if bad["detail"] != "since ?: the recorded problem cannot be read" {
		t.Fatalf("unreadable %s", bad["detail"])
	}
}

func TestProbeCodexDoesNotReadLogin(t *testing.T) {
	root := t.TempDir()
	bin := filepath.Join(root, "bin")
	home := filepath.Join(root, "codex-home")
	marker := filepath.Join(root, "marker")
	if err := os.Mkdir(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(home, 0o755); err != nil {
		t.Fatal(err)
	}
	const sentinel = "SENTINEL-DO-NOT-LEAK"
	if err := os.WriteFile(filepath.Join(home, "auth.json"), []byte(sentinel), 0o600); err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\n" +
		"link=$(/usr/bin/readlink \"$CODEX_HOME/auth.json\" 2>/dev/null || printf '%s' MISSING)\n" +
		"{\n" +
		"printf '%s\\n' \"LINK=$link\"\n" +
		"printf '%s\\n' \"HOMESET=$CODEX_HOME\"\n" +
		"printf '%s\\n' \"KEY=${CODEX_API_KEY-unset}\"\n" +
		"printf '%s\\n' \"TOKEN=${CODEX_ACCESS_TOKEN-unset}\"\n" +
		"printf '%s\\n' \"$*\"\n" +
		"} > " + shellQuote(marker) + "\n" +
		"echo 'Not logged in' >&2\nexit 1\n"
	if err := os.WriteFile(filepath.Join(bin, "codex"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	env := []string{
		"PATH=" + bin,
		"CODEX_HOME=" + home,
		"HOME=" + root,
		"CODEX_API_KEY=" + sentinel,
		"CODEX_ACCESS_TOKEN=" + sentinel,
	}
	state, detail := ProbeCodex(context.Background(), env)
	if state != "not_logged_in" || detail != "not logged in" || strings.Contains(detail, sentinel) {
		t.Fatalf("probe %s %s", state, detail)
	}
	body, err := os.ReadFile(marker)
	if err != nil {
		t.Fatal(err)
	}
	text := string(body)
	lines := strings.Split(text, "\n")
	wantLink := filepath.Join(home, "auth.json")
	if !containsLine(lines, "LINK="+wantLink) || !containsLine(lines, "KEY=unset") || !containsLine(lines, "TOKEN=unset") {
		t.Fatalf("marker %s", text)
	}
	if strings.Contains(text, sentinel) || !strings.Contains(text, "login status") {
		t.Fatalf("marker %s", text)
	}
	if strings.Contains(text, "HOMESET="+home) || !strings.Contains(text, "HOMESET=") {
		t.Fatal("probe reused the operator CODEX_HOME")
	}
	missing, missingDetail := ProbeCodex(context.Background(), []string{"PATH=" + root, "CODEX_HOME=" + home, "HOME=" + root})
	if missing != "unknown" || missingDetail != "`codex` is not on the server's PATH" {
		t.Fatalf("missing %s %s", missing, missingDetail)
	}
}

func TestRuntimePath(t *testing.T) {
	root := t.TempDir()
	bin := filepath.Join(root, "bin")
	if err := os.Mkdir(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	claude := filepath.Join(bin, "claude")
	if err := os.WriteFile(claude, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	env := []string{"PATH=" + bin}
	if RuntimePath("claude-code", "", claude, env) != claude {
		t.Fatalf("claude path %v", RuntimePath("claude-code", "", claude, env))
	}
	if RuntimePath("custom", "", claude, env) != nil {
		t.Fatal("unknown empty executable")
	}
	if RuntimePath("custom", claude, "claude", env) != claude {
		t.Fatal("unknown absolute")
	}
	if RuntimePath("omp", "", "claude", []string{"PATH=" + root}) != nil {
		t.Fatal("omp not on injected PATH")
	}
}

func TestPriceWarning(t *testing.T) {
	got := PriceWarning("mystery", "masked-key")
	if !strings.Contains(got, "'mystery'") || !strings.Contains(got, "'masked-key'") {
		t.Fatal(got)
	}
	if !strings.Contains(RuntimeCostWarning("Grok"), "per-run spend caps do not stop its runs") {
		t.Fatal(RuntimeCostWarning("Grok"))
	}
}

func TestProbeCodexKillsWrapperChild(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	bin := filepath.Join(root, "bin")
	home := filepath.Join(root, "codex-home")
	inGroup := filepath.Join(root, "ingroup.pid")
	escaped := filepath.Join(root, "escaped.pid")
	py := filepath.Join(root, "hold.py")
	if err := os.Mkdir(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(home, 0o755); err != nil {
		t.Fatal(err)
	}
	hold := "import os, sys, time\nos.setsid()\nwith open(sys.argv[1], \"w\", encoding=\"ascii\") as fh:\n    fh.write(str(os.getpid()))\ntime.sleep(120)\n"
	if err := os.WriteFile(py, []byte(hold), 0o644); err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\n" +
		"/bin/sh -c 'echo $$ > \"$1\"; exec /bin/sleep 120' sh " + shellQuote(inGroup) + " &\n" +
		shellQuote(python) + " " + shellQuote(py) + " " + shellQuote(escaped) + " &\n" +
		"i=0\n" +
		"while [ ! -s " + shellQuote(inGroup) + " ] || [ ! -s " + shellQuote(escaped) + " ]; do\n" +
		"  i=$((i+1))\n" +
		"  if [ \"$i\" -gt 100 ]; then\n" +
		"    echo 'child did not start' >&2\n" +
		"    exit 1\n" +
		"  fi\n" +
		"  /bin/sleep 0.05\n" +
		"done\n" +
		"/bin/sleep 120\n"
	if err := os.WriteFile(filepath.Join(bin, "codex"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		killPidFile(inGroup)
		killPidFile(escaped)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	type result struct {
		state  string
		detail string
	}
	done := make(chan result, 1)
	started := time.Now()
	go func() {
		state, detail := ProbeCodex(ctx, []string{"PATH=" + bin, "CODEX_HOME=" + home, "HOME=" + root})
		done <- result{state, detail}
	}()
	var got result
	select {
	case got = <-done:
	case <-time.After(12 * time.Second):
		t.Fatal("probe did not return; a child is holding the pipes")
	}
	if time.Since(started) > 10*time.Second {
		t.Fatalf("probe took %s", time.Since(started))
	}
	if got.state != "unknown" || got.detail != "`codex login status` did not run: TimeoutExpired" {
		t.Fatalf("probe %s %s", got.state, got.detail)
	}
	inPID := readPID(t, inGroup)
	if err := syscall.Kill(inPID, 0); err == nil {
		t.Fatalf("wrapper child %d still running", inPID)
	}
	escPID := readPID(t, escaped)
	if err := syscall.Kill(escPID, 0); err != nil {
		t.Fatalf("escaped child %d is gone (%v); pipe wait was not bounded", escPID, err)
	}
}

func TestLoginProbeCached(t *testing.T) {
	forgetLoginProbes()
	t.Cleanup(forgetLoginProbes)
	old := probeClock
	t.Cleanup(func() { probeClock = old })
	now := time.Unix(1_000, 0)
	probeClock = func() time.Time { return now }

	root := t.TempDir()
	bin := filepath.Join(root, "bin")
	home := filepath.Join(root, "codex-home")
	marker := filepath.Join(root, "runs")
	if err := os.Mkdir(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(home, 0o755); err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\nprintf 'x\\n' >> " + shellQuote(marker) + "\necho 'Not logged in' >&2\nexit 1\n"
	if err := os.WriteFile(filepath.Join(bin, "codex"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	env := []string{"PATH=" + bin, "CODEX_HOME=" + home, "HOME=" + root}
	ctx := context.Background()
	first := LoginView(ctx, "codex", env, nil, nil)
	second := LoginView(ctx, "codex", env, nil, nil)
	if first["state"] != "not_logged_in" || second["detail"] != first["detail"] {
		t.Fatalf("cached %#v %#v", first, second)
	}
	if runs := countLines(t, marker); runs != 1 {
		t.Fatalf("runs %d", runs)
	}
	now = now.Add(probeTTL)
	LoginView(ctx, "codex", env, nil, nil)
	if runs := countLines(t, marker); runs != 1 {
		t.Fatalf("runs at ttl %d", runs)
	}
	now = now.Add(time.Nanosecond)
	LoginView(ctx, "codex", env, nil, nil)
	if runs := countLines(t, marker); runs != 2 {
		t.Fatalf("runs after ttl %d", runs)
	}
	other := append(append([]string{}, env...), "EXTRA=1")
	LoginView(ctx, "codex", other, nil, nil)
	if runs := countLines(t, marker); runs != 3 {
		t.Fatalf("runs for other env %d", runs)
	}

	grokHome := filepath.Join(root, "grok-home")
	if err := os.Mkdir(grokHome, 0o755); err != nil {
		t.Fatal(err)
	}
	auth := filepath.Join(grokHome, "auth.json")
	if err := os.WriteFile(auth, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	grokEnv := []string{"GROK_HOME=" + grokHome, "HOME=" + root}
	logged := LoginView(ctx, "grok", grokEnv, nil, nil)
	if err := os.Remove(auth); err != nil {
		t.Fatal(err)
	}
	still := LoginView(ctx, "grok", grokEnv, nil, nil)
	if logged["state"] != "logged_in" || still["state"] != "logged_in" {
		t.Fatalf("grok cache %#v %#v", logged, still)
	}
	LoginView(ctx, "grok", grokEnv, &Problem{At: "2020-01-01T00:00:00+00:00", Detail: "copied"}, nil)
	again := LoginView(ctx, "grok", grokEnv, nil, nil)
	if again["state"] != "not_logged_in" {
		t.Fatalf("after forget %#v", again)
	}
}

func TestPrepareParityLoginUsesUniqueDirectory(t *testing.T) {
	names := []string{
		ParityBinPlaceholder,
		ParityCodexHomePlaceholder,
		ParityGrokHomePlaceholder,
		ParityEmptyHomePlaceholder,
	}
	before := map[string]os.FileInfo{}
	for _, name := range names {
		info, err := os.Lstat(name)
		if err == nil {
			before[name] = info
			continue
		}
		if !os.IsNotExist(err) {
			t.Fatal(err)
		}
	}
	if err := PrepareParityLogin(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(CloseParityLogin)
	for _, name := range names {
		info, err := os.Lstat(name)
		prev, had := before[name]
		if !had {
			if !os.IsNotExist(err) {
				t.Fatalf("created %s", name)
			}
			continue
		}
		if err != nil || !os.SameFile(prev, info) {
			t.Fatalf("changed %s", name)
		}
	}
	root := ParityRoot()
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		t.Fatalf("root %s %#v %v", root, info, err)
	}
	if ParityBin() == ParityBinPlaceholder || !strings.Contains(root, "loopgo-parity-login-") {
		t.Fatalf("bin %s root %s", ParityBin(), root)
	}
	for _, name := range []string{"claude", "codex", "omp", "grok"} {
		path := filepath.Join(ParityBin(), name)
		fi, err := os.Lstat(path)
		if err != nil || !fi.Mode().IsRegular() || fi.Mode()&os.ModeSymlink != 0 {
			t.Fatalf("%s mode %v err %v", name, fi, err)
		}
	}
	for _, home := range []string{ParityCodexHome(), ParityGrokHome(), ParityEmptyHome()} {
		fi, err := os.Lstat(home)
		if err != nil || !fi.IsDir() || fi.Mode()&os.ModeSymlink != 0 {
			t.Fatalf("home %s %#v %v", home, fi, err)
		}
		if _, err := os.Lstat(filepath.Join(home, "auth.json")); !os.IsNotExist(err) {
			t.Fatalf("auth.json %s %v", home, err)
		}
	}
	body := RewriteParityPaths([]byte(ParityClaude() + " " + filepath.Join(ParityGrokHome(), "auth.json")))
	want := ParityBinPlaceholder + "/claude " + ParityGrokHomePlaceholder + "/auth.json"
	if string(body) != want {
		t.Fatalf("rewrite %s", body)
	}
	first := ParityRoot()
	if err := PrepareParityLogin(); err != nil {
		t.Fatal(err)
	}
	if ParityRoot() == first {
		t.Fatal("second prepare reused the directory")
	}
	if _, err := os.Lstat(first); !os.IsNotExist(err) {
		t.Fatalf("first root remains: %v", err)
	}
}

func TestWriteExecDoesNotFollowSymlink(t *testing.T) {
	dir := t.TempDir()
	secret := filepath.Join(dir, "secret")
	if err := os.WriteFile(secret, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dir, "bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(secret, filepath.Join(dir, "bin", "codex")); err != nil {
		t.Fatal(err)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	if err := writeExec(root, filepath.Join("bin", "codex"), []byte("#!/bin/sh\nexit 0\n")); err == nil {
		t.Fatal("wrote through a symlink")
	}
	body, err := os.ReadFile(secret)
	if err != nil || string(body) != "keep" {
		t.Fatalf("secret %q err %v", body, err)
	}
}

func TestRemoveWithinDoesNotFollowSymlink(t *testing.T) {
	dir := t.TempDir()
	secret := filepath.Join(dir, "secret")
	if err := os.WriteFile(secret, []byte("credentials"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dir, "home"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(secret, filepath.Join(dir, "home", "auth.json")); err != nil {
		t.Fatal(err)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	if err := removeWithin(root, filepath.Join("home", "auth.json")); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(secret)
	if err != nil || string(body) != "credentials" {
		t.Fatalf("secret %q err %v", body, err)
	}
	if _, err := os.Lstat(filepath.Join(dir, "home", "auth.json")); !os.IsNotExist(err) {
		t.Fatalf("link remains: %v", err)
	}
}

func TestProbeCodexConflictingProcessPATH(t *testing.T) {
	root := t.TempDir()
	fakeDir := filepath.Join(root, "fake")
	decoyDir := filepath.Join(root, "decoy")
	home := filepath.Join(root, "codex-home")
	marker := filepath.Join(root, "marker")
	for _, dir := range []string{fakeDir, decoyDir, home} {
		if err := os.Mkdir(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	fakeScript := "#!/bin/sh\nprintf '%s\\n' FAKE > " + shellQuote(marker) +
		"\nprintf '%s\\n' \"$0\" >> " + shellQuote(marker) +
		"\necho 'Not logged in' >&2\nexit 1\n"
	decoyScript := "#!/bin/sh\nprintf '%s\\n' DECOY > " + shellQuote(marker) +
		"\nprintf '%s\\n' \"$0\" >> " + shellQuote(marker) +
		"\necho 'Logged in using ChatGPT' >&2\nexit 0\n"
	if err := os.WriteFile(filepath.Join(fakeDir, "codex"), []byte(fakeScript), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(decoyDir, "codex"), []byte(decoyScript), 0o755); err != nil {
		t.Fatal(err)
	}
	// The process PATH names the decoy first. Injected "." and empty entries
	// name the working directory, which holds the fake. A bare "codex" would
	// make exec.Command run the decoy.
	t.Setenv("PATH", decoyDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Chdir(fakeDir)
	found, err := exec.LookPath("codex")
	if err != nil || found != filepath.Join(decoyDir, "codex") {
		t.Fatalf("process PATH codex is %q err %v", found, err)
	}
	cases := []struct {
		name string
		path string
	}{
		{name: "dot", path: "."},
		{name: "empty", path: ""},
		{name: "empty before decoy", path: string(os.PathListSeparator) + decoyDir},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if err := os.Remove(marker); err != nil && !os.IsNotExist(err) {
				t.Fatal(err)
			}
			env := []string{"PATH=" + tc.path, "CODEX_HOME=" + home, "HOME=" + root}
			got := Which("codex", env)
			if !filepath.IsAbs(got) || filepath.Base(got) == got || !sameFile(t, got, filepath.Join(fakeDir, "codex")) {
				t.Fatalf("which %q", got)
			}
			state, detail := ProbeCodex(context.Background(), env)
			body, err := os.ReadFile(marker)
			if err != nil {
				t.Fatalf("probe %s %s err %v", state, detail, err)
			}
			lines := strings.Split(strings.TrimRight(string(body), "\n"), "\n")
			if len(lines) != 2 || lines[0] != "FAKE" || !filepath.IsAbs(lines[1]) || !sameFile(t, lines[1], filepath.Join(fakeDir, "codex")) {
				t.Fatalf("ran %q state %s %s", body, state, detail)
			}
			if state != "not_logged_in" || detail != "not logged in" {
				t.Fatalf("probe %s %s", state, detail)
			}
		})
	}
}

func TestProbeCacheWaitHonorsCancel(t *testing.T) {
	forgetLoginProbes()
	t.Cleanup(forgetLoginProbes)
	root := t.TempDir()
	bin := filepath.Join(root, "bin")
	home := filepath.Join(root, "codex-home")
	started := filepath.Join(root, "started")
	release := filepath.Join(root, "release")
	if err := os.Mkdir(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(home, 0o755); err != nil {
		t.Fatal(err)
	}
	script := "#!/bin/sh\nprintf 'x\\n' >> " + shellQuote(started) + "\n" +
		"i=0\n" +
		"while [ ! -f " + shellQuote(release) + " ]; do\n" +
		"  i=$((i+1))\n" +
		"  if [ \"$i\" -gt 400 ]; then\n" +
		"    echo 'held too long' >&2\n" +
		"    exit 1\n" +
		"  fi\n" +
		"  /bin/sleep 0.05\n" +
		"done\n" +
		"echo 'Not logged in' >&2\nexit 1\n"
	if err := os.WriteFile(filepath.Join(bin, "codex"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	env := []string{"PATH=" + bin, "CODEX_HOME=" + home, "HOME=" + root}
	leaderCtx, leaderCancel := context.WithCancel(context.Background())
	t.Cleanup(func() {
		leaderCancel()
		_ = os.WriteFile(release, []byte("x"), 0o644)
	})

	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	if view := LoginView(canceled, "codex", env, nil, nil); view["detail"] != "login probe wait canceled: context canceled" {
		t.Fatalf("before probe %#v", view)
	}
	if _, err := os.Stat(started); !os.IsNotExist(err) {
		t.Fatal("canceled view ran the probe")
	}

	leaderDone := make(chan map[string]string, 1)
	go func() {
		leaderDone <- LoginView(leaderCtx, "codex", env, nil, nil)
	}()
	waitForFile(t, started, 5*time.Second)

	deadline, deadlineCancel := context.WithDeadline(context.Background(), time.Unix(0, 0))
	defer deadlineCancel()
	start := time.Now()
	view := LoginView(deadline, "codex", env, nil, nil)
	if time.Since(start) > time.Second || view["detail"] != "login probe wait canceled: deadline exceeded" {
		t.Fatalf("deadline wait %s %#v", time.Since(start), view)
	}
	start = time.Now()
	view = LoginView(canceled, "codex", env, nil, nil)
	if time.Since(start) > time.Second || view["detail"] != "login probe wait canceled: context canceled" {
		t.Fatalf("canceled wait %s %#v", time.Since(start), view)
	}
	if runs := countLines(t, started); runs != 1 {
		t.Fatalf("probes while waiting %d", runs)
	}

	if err := os.WriteFile(release, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	var leader map[string]string
	select {
	case leader = <-leaderDone:
	case <-time.After(5 * time.Second):
		t.Fatal("leader did not return after release")
	}
	if leader["state"] != "not_logged_in" || leader["detail"] != "not logged in" {
		t.Fatalf("leader %#v", leader)
	}
	again := LoginView(context.Background(), "codex", env, nil, nil)
	if again["state"] != "not_logged_in" || again["detail"] != leader["detail"] {
		t.Fatalf("cached %#v", again)
	}
	if runs := countLines(t, started); runs != 1 {
		t.Fatalf("probes after cache %d", runs)
	}
}

func waitForFile(t *testing.T, path string, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", path)
}

func sameFile(t *testing.T, a, b string) bool {
	t.Helper()
	left, err := os.Stat(a)
	if err != nil {
		t.Fatalf("stat %s: %v", a, err)
	}
	right, err := os.Stat(b)
	if err != nil {
		t.Fatalf("stat %s: %v", b, err)
	}
	return os.SameFile(left, right)
}

func readPID(t *testing.T, path string) int {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(body)))
	if err != nil || pid <= 0 {
		t.Fatalf("pid %q", body)
	}
	return pid
}

func killPidFile(path string) {
	body, err := os.ReadFile(path)
	if err != nil {
		return
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(body)))
	if err != nil || pid <= 0 {
		return
	}
	_ = syscall.Kill(pid, syscall.SIGKILL)
}

func countLines(t *testing.T, path string) int {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, line := range strings.Split(string(body), "\n") {
		if line != "" {
			n++
		}
	}
	return n
}

func shellQuote(path string) string {
	return "'" + strings.ReplaceAll(path, "'", `'\''`) + "'"
}
