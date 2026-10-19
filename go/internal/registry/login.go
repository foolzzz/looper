package registry

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	loginFileName      = "auth.json"
	codexStatusTimeout = 15 * time.Second
	// After the context fires, Run stops waiting for a descendant's pipes.
	codexWaitDelay    = 2 * time.Second
	probeTTL          = 30 * time.Second
	codexLoginForkKey = "codex_login_fork"
	grokLoginForkKey  = "grok_login_fork"
)

// probeClock is the cache clock. Tests replace it. time.Now is monotonic.
var probeClock = time.Now

// A login probe is cached for probeTTL, the way codex_login.cached_probe and
// grok_login.cached_probe are. The dashboard polls the registry every few
// seconds. The key is the injected environment, so one server reuses a result
// and a test with another environment does not.
//
// mu is not held while a probe runs. Callers that arrive for the same key
// wait on that probe and return when their own context ends.
type probeCache struct {
	mu      sync.Mutex
	at      time.Time
	key     string
	state   string
	detail  string
	ok      bool
	gen     uint64
	flights map[string]*probeFlight
}

// probeFlight is one probe running for a cache key.
type probeFlight struct {
	done   chan struct{}
	state  string
	detail string
}

var (
	codexLoginCache probeCache
	grokLoginCache  probeCache
)

// LoginForkKey is the system_settings key of a recorded login problem.
func LoginForkKey(kind string) string {
	switch kind {
	case "codex":
		return codexLoginForkKey
	case "grok":
		return grokLoginForkKey
	default:
		return ""
	}
}

// Problem is a recorded login problem, {"detail", "at"}. Unreadable means the
// stored value was not a JSON object; login_view still shows it as unavailable.
type Problem struct {
	At         string
	Detail     string
	Unreadable bool
}

// LoginView is codex_login.login_view or grok_login.login_view. readErr is a
// failure to read system_settings: the view is unknown and the probe does not
// run. A recorded problem does not run the probe either. env is the server
// environment; pass it explicitly so a test never stats the operator's login.
// kind is "codex" or "grok".
func LoginView(ctx context.Context, kind string, env []string, problem *Problem, readErr error) map[string]string {
	if readErr != nil {
		return map[string]string{
			"state":  "unknown",
			"detail": fmt.Sprintf("cannot be read (%s)", excName(readErr)),
		}
	}
	if problem != nil {
		// record_problems and clear_problem call forget_probe. A view that
		// still shows the problem drops the cached probe, so the next view
		// after the problem is gone reads the login again.
		forgetLoginProbe(kind)
		at := problem.At
		if at == "" {
			at = "?"
		}
		detail := problem.Detail
		if problem.Unreadable {
			detail = "the recorded problem cannot be read"
			at = "?"
		}
		return map[string]string{"state": "unavailable", "detail": "since " + at + ": " + detail}
	}
	var state, detail string
	switch kind {
	case "codex":
		state, detail = cachedCodexProbe(ctx, env)
	case "grok":
		state, detail = cachedGrokProbe(ctx, env)
	default:
		state, detail = "unknown", "cannot be read (Exception)"
	}
	return map[string]string{"state": state, "detail": detail}
}

// LoginFile is the operator's auth.json: $HOME_KEY/auth.json, else
// ~/fallback/auth.json. ~ expands with HOME from env, not the process home,
// so an injected environment cannot point at the real one by accident when
// HOME is set with it. The file is not opened.
func LoginFile(env []string, homeKey, fallback string) string {
	home := ""
	if value, ok := envValue(env, homeKey); ok {
		home = strings.TrimSpace(value)
	}
	var base string
	if home != "" {
		base = absPath(expandUser(home, env))
	} else {
		base = filepath.Join(homeDir(env), fallback)
	}
	return filepath.Join(base, loginFileName)
}

func homeDir(env []string) string {
	if value, ok := envValue(env, "HOME"); ok && strings.TrimSpace(value) != "" {
		return absPath(value)
	}
	// No HOME in the injected environment. Do not call os.UserHomeDir: that
	// would name the operator's home. A relative ".codex" stays relative and
	// the probe reports that path missing.
	return ""
}

func expandUser(path string, env []string) string {
	if path == "~" || strings.HasPrefix(path, "~/") {
		return filepath.Join(homeDir(env), strings.TrimPrefix(path, "~"))
	}
	return path
}

func absPath(path string) string {
	if path == "" {
		return ""
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return filepath.Clean(path)
	}
	return abs
}

// ProbeGrok is grok_login.probe_login. It stats the login file and does not
// read it. A symlink to a regular file counts as signed in, because os.stat
// follows links. A missing file or a dangling link is not logged in.
func ProbeGrok(env []string) (string, string) {
	target := LoginFile(env, "GROK_HOME", ".grok")
	info, err := os.Stat(target)
	if err != nil {
		return "not_logged_in", "no " + target
	}
	if !info.Mode().IsRegular() {
		return "not_logged_in", target + " is not a file"
	}
	return "logged_in", "signed in (" + target + ")"
}

// ProbeCodex is codex_login.probe_login. It symlinks the login file into a
// temporary CODEX_HOME and runs `codex login status`. It does not open the
// login file. CODEX_API_KEY and CODEX_ACCESS_TOKEN are removed from env.
// The executable is PATH's codex, from env, not a runtime row.
func ProbeCodex(ctx context.Context, env []string) (string, string) {
	// Which reads env's PATH. An empty entry or "." can still come back
	// relative. exec.CommandContext looks up a bare name on the process PATH
	// before cmd.Env is assigned, which would run a different codex.
	const executable = "codex"
	bin := Which(executable, env)
	if bin == "" {
		return "unknown", "`codex` is not on the server's PATH"
	}
	absBin, absErr := filepath.Abs(bin)
	if absErr != nil || !filepath.IsAbs(absBin) || !isExecutable(absBin) {
		return "unknown", "`codex` is not on the server's PATH"
	}
	home, err := os.MkdirTemp("", "looper-codex-login-")
	if err != nil {
		return "unknown", "`codex login status` did not run: " + excName(err)
	}
	defer os.RemoveAll(home)
	target := LoginFile(env, "CODEX_HOME", ".codex")
	if err := os.Symlink(target, filepath.Join(home, loginFileName)); err != nil {
		return "unknown", "`codex login status` did not run: " + excName(err)
	}
	probeCtx, cancel := context.WithTimeout(ctx, codexStatusTimeout)
	defer cancel()
	cmd := exec.CommandContext(probeCtx, absBin, "-c", `cli_auth_credentials_store="file"`, "login", "status")
	cmd.Env = codexProbeEnv(env, home)
	cmd.Stdin = nil
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.WaitDelay = codexWaitDelay
	// CommandContext kills this process only. A wrapper's child keeps the
	// pipes open and Run waits. The child stays in this process group.
	cmd.Cancel = func() error {
		return killProcessGroup(cmd.Process)
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err = cmd.Run()
	// A deadline kills the process, and Wait then returns *exec.ExitError
	// (status -1) ahead of the context error. Python raises TimeoutExpired.
	if errors.Is(probeCtx.Err(), context.DeadlineExceeded) {
		return "unknown", "`codex login status` did not run: TimeoutExpired"
	}
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			return parseStatus(exitErr.ExitCode(), stderr.String()+"\n"+stdout.String())
		}
		if errors.Is(err, exec.ErrNotFound) || os.IsNotExist(err) {
			return "unknown", "`codex` is not on the server's PATH"
		}
		return "unknown", "`codex login status` did not run: " + excName(err)
	}
	return parseStatus(0, stderr.String()+"\n"+stdout.String())
}

// killProcessGroup signals the child's process group. Setpgid made pid the
// group id. pid 0 would signal this process instead.
func killProcessGroup(proc *os.Process) error {
	if proc == nil || proc.Pid <= 0 {
		return os.ErrProcessDone
	}
	err := syscall.Kill(-proc.Pid, syscall.SIGKILL)
	if err == nil {
		return nil
	}
	if errors.Is(err, syscall.ESRCH) {
		return os.ErrProcessDone
	}
	if killErr := proc.Kill(); killErr == nil || errors.Is(killErr, os.ErrProcessDone) {
		return nil
	}
	return fmt.Errorf("kill process group: %w", err)
}

func cachedCodexProbe(ctx context.Context, env []string) (string, string) {
	return codexLoginCache.get(ctx, envKey(env), func() (string, string) {
		return ProbeCodex(ctx, env)
	})
}

func cachedGrokProbe(ctx context.Context, env []string) (string, string) {
	return grokLoginCache.get(ctx, envKey(env), func() (string, string) {
		return ProbeGrok(env)
	})
}

func envKey(env []string) string {
	return strings.Join(env, "\x00")
}

func (c *probeCache) get(ctx context.Context, key string, probe func() (string, string)) (string, string) {
	if ctx == nil {
		ctx = context.Background()
	}
	c.mu.Lock()
	now := probeClock()
	if c.ok && c.key == key && now.Sub(c.at) <= probeTTL {
		state, detail := c.state, c.detail
		c.mu.Unlock()
		return state, detail
	}
	if flight := c.flights[key]; flight != nil {
		c.mu.Unlock()
		return waitProbe(ctx, flight)
	}
	if err := ctx.Err(); err != nil {
		c.mu.Unlock()
		return canceledWait(err)
	}
	flight := &probeFlight{done: make(chan struct{})}
	gen := c.gen
	if c.flights == nil {
		c.flights = make(map[string]*probeFlight)
	}
	c.flights[key] = flight
	c.mu.Unlock()
	defer close(flight.done)

	state, detail := probe()

	c.mu.Lock()
	flight.state = state
	flight.detail = detail
	if c.flights[key] == flight {
		delete(c.flights, key)
	}
	// A cancel or a forget that arrived during the probe does not fill the
	// cache. The caller still receives this probe's own result.
	if c.gen == gen && ctx.Err() == nil {
		c.at = now
		c.key = key
		c.state = state
		c.detail = detail
		c.ok = true
	}
	c.mu.Unlock()
	return state, detail
}

// waitProbe waits for an in-flight probe. A finished probe wins over a
// context that has also ended. Otherwise the caller's context ends the wait
// without holding the cache lock.
func waitProbe(ctx context.Context, flight *probeFlight) (string, string) {
	select {
	case <-flight.done:
		return flight.state, flight.detail
	default:
	}
	select {
	case <-ctx.Done():
		return canceledWait(ctx.Err())
	case <-flight.done:
		return flight.state, flight.detail
	}
}

func canceledWait(err error) (string, string) {
	if errors.Is(err, context.DeadlineExceeded) {
		return "unknown", "login probe wait canceled: deadline exceeded"
	}
	return "unknown", "login probe wait canceled: context canceled"
}

func (c *probeCache) clear() {
	c.mu.Lock()
	c.ok = false
	c.gen++
	c.mu.Unlock()
}

func forgetLoginProbe(kind string) {
	switch kind {
	case "codex":
		codexLoginCache.clear()
	case "grok":
		grokLoginCache.clear()
	}
}

func forgetLoginProbes() {
	codexLoginCache.clear()
	grokLoginCache.clear()
}

func codexProbeEnv(env []string, home string) []string {
	out := make([]string, 0, len(env)+1)
	for _, item := range env {
		if strings.HasPrefix(item, "CODEX_API_KEY=") || strings.HasPrefix(item, "CODEX_ACCESS_TOKEN=") {
			continue
		}
		if strings.HasPrefix(item, "CODEX_HOME=") {
			continue
		}
		out = append(out, item)
	}
	out = append(out, "CODEX_HOME="+home)
	return out
}

// ParseStatus is codex_login.parse_status. output is stderr, a newline, then stdout.
func ParseStatus(returncode int, output string) (string, string) {
	return parseStatus(returncode, output)
}

func parseStatus(returncode int, output string) (string, string) {
	var lines []string
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			lines = append(lines, line)
		}
	}
	if returncode == 0 && containsLine(lines, "Logged in using ChatGPT") {
		return "chatgpt", "logged in using ChatGPT"
	}
	if returncode == 0 {
		for _, line := range lines {
			if strings.HasPrefix(line, "Logged in using ") {
				method := strings.TrimPrefix(line, "Logged in using ")
				if i := strings.Index(method, " - "); i >= 0 {
					method = method[:i]
				}
				return "other_method", "logged in using " + method + ", not ChatGPT"
			}
		}
	}
	if returncode == 1 && containsLine(lines, "Not logged in") {
		return "not_logged_in", "not logged in"
	}
	said := ""
	for _, line := range lines {
		if !strings.Contains(line, "Logged in using") {
			said = line
			break
		}
	}
	if len(said) > 200 {
		said = said[:200]
	}
	detail := fmt.Sprintf("`codex login status` exited %d", returncode)
	if said != "" {
		detail += ": " + said
	}
	return "unknown", detail
}

func containsLine(lines []string, want string) bool {
	for _, line := range lines {
		if line == want {
			return true
		}
	}
	return false
}

func excName(err error) string {
	current := err
	for current != nil {
		name := typeName(current)
		if name != "" && name != "wrapError" && name != "joinError" {
			return name
		}
		current = errors.Unwrap(current)
	}
	return "Exception"
}

func typeName(err error) string {
	t := reflect.TypeOf(err)
	if t == nil {
		return ""
	}
	for t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	return t.Name()
}
