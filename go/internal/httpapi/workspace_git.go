package httpapi

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// checkoutLimitBytes is step_workspaces.CHECKOUT_LIMIT_BYTES.
const checkoutLimitBytes int64 = 1 << 30

// gitTimeout is GIT_TIMEOUT_SEC. The request context is shorter, and
// CommandContext stops at whichever ends first.
const gitTimeout = 120 * time.Second

// notesGitTimeout is operator_notes._GIT_TIMEOUT_SEC.
const notesGitTimeout = 10 * time.Second

type repository struct {
	toplevel string
	subdir   string
	head     string
	branch   string
}

type gitRun struct {
	code           int
	stdout, stderr []byte
}

// inspectRepository is task_workspaces._repository. A path that is not a
// directory is not a repository. A git directory with no commit is an error,
// not the not_git warning.
func inspectRepository(ctx context.Context, workspace string) (*repository, error) {
	info, err := os.Stat(workspace)
	if err != nil || !info.IsDir() {
		return nil, nil
	}
	where, err := runGit(ctx, workspace, gitTimeout, true, "rev-parse", "--show-toplevel", "--show-prefix")
	if err != nil {
		return nil, fmt.Errorf("cannot inspect the workspace %s: %w", workspace, err)
	}
	if where.code != 0 {
		stderr := string(where.stderr)
		if strings.HasPrefix(stderr, "fatal: not a git repository (or any of the parent") ||
			strings.HasPrefix(stderr, "fatal: this operation must be run in a work tree") {
			return nil, nil
		}
		msg := clipGit(where.stderr)
		if msg == "" {
			msg = fmt.Sprintf("git repository inspection failed: exit %d", where.code)
		}
		return nil, fmt.Errorf("cannot inspect the workspace %s: %s", workspace, msg)
	}
	lines := strings.Split(string(where.stdout), "\n")
	toplevel := lines[0]
	subdir := ""
	if len(lines) > 1 {
		subdir = strings.TrimRight(lines[1], "/")
	}
	head, err := runGit(ctx, toplevel, gitTimeout, true, "rev-parse", "--verify", "-q", "HEAD^{commit}")
	if err != nil {
		return nil, fmt.Errorf("cannot inspect the workspace %s: %w", workspace, err)
	}
	if head.code != 0 {
		msg := clipGit(head.stderr)
		if msg == "" {
			msg = "git HEAD inspection failed: the repository has no readable commit"
		}
		return nil, fmt.Errorf("cannot inspect the workspace %s: %s", workspace, msg)
	}
	branch, err := runGit(ctx, toplevel, gitTimeout, true, "symbolic-ref", "-q", "HEAD")
	if err != nil {
		return nil, fmt.Errorf("cannot inspect the workspace %s: %w", workspace, err)
	}
	detached := branch.code == 1 && len(branch.stdout) == 0 && len(branch.stderr) == 0
	if branch.code != 0 && !detached {
		msg := clipGit(branch.stderr)
		if msg == "" {
			msg = fmt.Sprintf("git branch inspection failed: exit %d", branch.code)
		}
		return nil, fmt.Errorf("cannot inspect the workspace %s: %s", workspace, msg)
	}
	name := ""
	if branch.code == 0 {
		name = strings.TrimSpace(string(branch.stdout))
	}
	return &repository{
		toplevel: toplevel,
		subdir:   subdir,
		head:     strings.TrimSpace(string(head.stdout)),
		branch:   name,
	}, nil
}

// baseCommit is _base_commit. A missing name is 400. A git failure that is
// not a missing name (git is absent, or the command timed out) is 500:
// check() does not catch GitError from this call.
func baseCommit(ctx context.Context, repo *repository, base string) (string, int, error) {
	if base == "" {
		return repo.head, 0, nil
	}
	run, err := runGit(ctx, repo.toplevel, gitTimeout, true, "rev-parse", "--verify", "-q", base+"^{commit}")
	if err != nil {
		return "", 500, err
	}
	if run.code != 0 {
		return "", 400, fmt.Errorf("the base %s is not a branch, tag or commit of %s", pythonRepr(base), repo.toplevel)
	}
	return strings.TrimSpace(string(run.stdout)), 0, nil
}

func workspaceState(ctx context.Context, repo *repository) ([]string, int64, error) {
	spec := repo.subdir
	if spec == "" {
		spec = "."
	}
	status, err := runGit(ctx, repo.toplevel, gitTimeout, true, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", spec)
	if err != nil {
		return nil, 0, err
	}
	if status.code != 0 {
		return nil, 0, fmt.Errorf("git status failed: %s", clipGit(status.stderr))
	}
	size, err := checkoutSize(ctx, repo.toplevel)
	if err != nil {
		return nil, 0, err
	}
	return porcelainPaths(status.stdout), size, nil
}

func checkoutSize(ctx context.Context, toplevel string) (int64, error) {
	tree, err := runGit(ctx, toplevel, gitTimeout, true, "rev-parse", "--verify", "-q", "HEAD^{tree}")
	if err != nil {
		return 0, err
	}
	if tree.code != 0 {
		return 0, fmt.Errorf("git rev-parse failed: %s", clipGit(tree.stderr))
	}
	run, err := runGit(ctx, toplevel, gitTimeout, true, "ls-tree", "-r", "-z", "--format=%(objectsize)", strings.TrimSpace(string(tree.stdout)))
	if err != nil {
		return 0, err
	}
	if run.code != 0 {
		return 0, fmt.Errorf("git ls-tree failed: %s", clipGit(run.stderr))
	}
	var sum int64
	for _, field := range bytes.Split(run.stdout, []byte{0}) {
		if len(field) == 0 {
			continue
		}
		n, err := strconv.ParseInt(string(field), 10, 64)
		if err != nil {
			continue
		}
		sum += n
	}
	return sum, nil
}

func porcelainPaths(stdout []byte) []string {
	raw := bytes.Split(stdout, []byte{0})
	fields := make([][]byte, 0, len(raw))
	for _, field := range raw {
		if len(field) > 0 {
			fields = append(fields, field)
		}
	}
	paths := []string{}
	for i := 0; i < len(fields); i++ {
		entry := fields[i]
		if len(entry) >= 3 {
			paths = append(paths, string(entry[3:]))
		}
		if entry[0] == 'R' || entry[0] == 'C' {
			i++
			if i < len(fields) {
				paths = append(paths, string(fields[i]))
			}
		}
	}
	return paths
}

// runGit runs one git command. looper is step_workspaces._git: no hooks and
// no prompts. notes_key leaves those unset and only drops GIT_DIR and the
// other redirect variables. The environment is not logged.
func runGit(ctx context.Context, cwd string, limit time.Duration, looper bool, args ...string) (gitRun, error) {
	name := args[0]
	if looper {
		args = append([]string{"-c", "core.hooksPath=/dev/null"}, args...)
	}
	ctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.Dir = cwd
	cmd.Env = gitEnv(looper)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return gitRun{}, fmt.Errorf("git %s timed out after %ds", name, int(limit.Seconds()))
	}
	if err == nil {
		return gitRun{stdout: stdout.Bytes(), stderr: stderr.Bytes()}, nil
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return gitRun{code: exit.ExitCode(), stdout: stdout.Bytes(), stderr: stderr.Bytes()}, nil
	}
	return gitRun{}, fmt.Errorf("cannot run git in %s: %w", cwd, err)
}

func gitEnv(looper bool) []string {
	skip := map[string]bool{
		"GIT_DIR": true, "GIT_WORK_TREE": true, "GIT_INDEX_FILE": true, "GIT_COMMON_DIR": true,
	}
	env := make([]string, 0, len(os.Environ())+2)
	for _, item := range os.Environ() {
		name, _, _ := strings.Cut(item, "=")
		if skip[name] {
			continue
		}
		env = append(env, item)
	}
	if looper {
		env = append(env, "GIT_TERMINAL_PROMPT=0", "GIT_MERGE_AUTOEDIT=no")
	}
	return env
}

func clipGit(stderr []byte) string {
	msg := strings.TrimSpace(string(stderr))
	if len(msg) > 500 {
		msg = msg[:500]
	}
	return msg
}

// realpath is os.path.realpath. A missing tail stays, and the existing
// prefix is resolved, so /tmp/missing and /private/tmp/missing agree on macOS.
func realpath(path string) string {
	if path == "" {
		return ""
	}
	abs, err := absPath(path)
	if err != nil {
		abs = path
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		return resolved
	}
	var missing []string
	cur := abs
	for {
		if _, err := os.Lstat(cur); err == nil {
			break
		}
		parent := parentPath(cur)
		if parent == cur {
			return abs
		}
		missing = append(missing, basePath(cur))
		cur = parent
	}
	if resolved, err := filepath.EvalSymlinks(cur); err == nil {
		cur = resolved
	}
	for i := len(missing) - 1; i >= 0; i-- {
		cur = cur + "/" + missing[i]
	}
	return cur
}

func absPath(path string) (string, error) {
	if strings.HasPrefix(path, "/") {
		return pathClean(path), nil
	}
	wd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	return pathClean(wd + "/" + path), nil
}

func parentPath(path string) string {
	if path == "/" || path == "" {
		return "/"
	}
	trimmed := strings.TrimRight(path, "/")
	i := strings.LastIndex(trimmed, "/")
	if i < 0 {
		return ""
	}
	if i == 0 {
		return "/"
	}
	return trimmed[:i]
}

func basePath(path string) string {
	trimmed := strings.TrimRight(path, "/")
	i := strings.LastIndex(trimmed, "/")
	if i < 0 {
		return trimmed
	}
	return trimmed[i+1:]
}
