package httpapi

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/user"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/foolzzz/looper/go/internal/db"
)

// noWorkDir is projects.NO_WORK_DIR. A schedule's copy names the schedule.
const noWorkDir = "a work directory is required: set one on the task or on its project"

const (
	workspaceInPlace  = "in_place"
	workspaceWorktree = "worktree"
	baseMaxChars      = 200
)

func (s *Server) handleWorkspaceCheck(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/workspaces/check" {
		notFound(w)
		return
	}
	query := pythonQuery(r.URL.RawQuery)
	projectID := queryValue(query, "project_id")
	named := pyStrip(queryValue(query, "dir"))

	ctx, cancel := readContext(r)
	defer cancel()

	var project db.ProjectWorkspace
	hasProject := projectID != ""
	if hasProject {
		if s.Reads == nil {
			requestFailed(w, "workspace check", errNoDatabase)
			return
		}
		row, err := s.Reads.ProjectWorkspace(ctx, projectID)
		if err != nil {
			requestFailed(w, "workspace check", err)
			return
		}
		if !row.Found {
			missingProject(w, projectID)
			return
		}
		project = row
	}

	defaultDir := ""
	projectMode := workspaceWorktree
	notesOn := true
	if hasProject {
		defaultDir = project.WorkDir
		projectMode = workspaceInPlace
		if project.TaskWorkspaces == workspaceWorktree {
			projectMode = workspaceWorktree
		}
		if project.NotesOff {
			notesOn = false
		}
	}

	workDir, err := taskWorkDir(named, defaultDir)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: err.Error()})
		return
	}
	base, err := parseWorkspaceBase(queryValue(query, "base"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: err.Error()})
		return
	}
	namedPath := ""
	if named != "" {
		namedPath = workDir
	}
	requested := queryValue(query, "mode")
	mode, err := resolveWorkspaceMode(requested, namedPath, defaultDir, projectMode)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: err.Error()})
		return
	}
	if base != "" && mode != workspaceWorktree {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: "base is where a task's worktree starts: give it with workspace_mode worktree"})
		return
	}
	defaultMode, err := resolveWorkspaceMode("", namedPath, defaultDir, projectMode)
	if err != nil {
		requestFailed(w, "workspace check", err)
		return
	}

	body, status, err := s.inspectWorkspace(ctx, workDir, mode, base, requested != "", defaultMode, notesOn)
	if err != nil {
		if status == http.StatusBadRequest {
			writeJSON(w, status, errorBody{Error: err.Error()})
			return
		}
		requestFailed(w, "workspace check", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func taskWorkDir(named, projectDefault string) (string, error) {
	if named != "" {
		return resolveWorkDir(named)
	}
	if projectDefault != "" {
		return projectDefault, nil
	}
	return "", errors.New(noWorkDir)
}

// resolveWorkDir is tiers.task.resolve_work_dir. The error quotes the stripped
// text, before ~ is expanded.
func resolveWorkDir(raw string) (string, error) {
	text := pyStrip(raw)
	if text == "" {
		return "", errors.New("a work directory is required")
	}
	path := expandUser(text)
	if !strings.HasPrefix(path, "/") {
		return "", fmt.Errorf("work directory must be an absolute path, got %s", pythonRepr(text))
	}
	return pathClean(path), nil
}

func parseWorkspaceBase(raw string) (string, error) {
	if raw == "" {
		return "", nil
	}
	if utf8.RuneCountInString(raw) > baseMaxChars || strings.Contains(raw, "\n") {
		return "", errors.New("base must be a branch, tag or commit of at most 200 characters")
	}
	return pyStrip(raw), nil
}

// resolveWorkspaceMode is task_workspaces.resolve_mode. named is the resolved
// work directory when the query named one, otherwise empty. An empty request
// follows a directory that is not the project's, and otherwise the project.
func resolveWorkspaceMode(requested, named, projectDir, projectMode string) (string, error) {
	if requested != "" {
		if requested != workspaceInPlace && requested != workspaceWorktree {
			return "", errors.New("workspace_mode must be one of in_place, worktree")
		}
		return requested, nil
	}
	if named != "" && !sameDirectory(named, projectDir) {
		return workspaceWorktree, nil
	}
	return projectMode, nil
}

func sameDirectory(a, b string) bool {
	return a != "" && b != "" && realpath(a) == realpath(b)
}

func (s *Server) inspectWorkspace(ctx context.Context, workspace, mode, base string, asked bool, defaultMode string, notesOn bool) (map[string]any, int, error) {
	repo, err := inspectRepository(ctx, workspace)
	if err != nil {
		return nil, http.StatusBadRequest, err
	}
	effective := mode
	warnings := []any{}
	var git any
	if repo == nil {
		if mode == workspaceWorktree {
			effective = workspaceInPlace
			text := "This project runs tasks in a worktree by default, but the workspace is not in a git repository with a commit, so the task runs in place, in the directory itself."
			if asked {
				text = "The workspace is not in a git repository with a commit: the task runs in place, in the directory itself."
			}
			warnings = append(warnings, map[string]any{"kind": "not_git", "text": text})
		}
	} else {
		git = map[string]any{
			"repository": repo.toplevel,
			"branch":     shortBranch(repo.branch),
			"head":       repo.head,
		}
		if mode == workspaceWorktree {
			commit, status, err := baseCommit(ctx, repo, base)
			if err != nil {
				return nil, status, err
			}
			git.(map[string]any)["base_commit"] = commit
			pending, size, stateErr := workspaceState(ctx, repo)
			if stateErr != nil {
				slog.Warn("cannot read the workspace state", "repository", repo.toplevel, "err", stateErr)
				pending, size = nil, 0
			}
			if len(pending) > 0 {
				files := pending
				if len(files) > 50 {
					files = files[:50]
				}
				fileList := make([]any, len(files))
				for i, path := range files {
					fileList[i] = path
				}
				warnings = append(warnings, map[string]any{
					"kind":  "uncommitted",
					"files": fileList,
					"text": fmt.Sprintf("%d uncommitted change(s) in the workspace will not be in the task's worktree: %s. Commit them first if the task needs them.",
						len(pending), filesText(pending)),
				})
			}
			if size > checkoutLimitBytes {
				warnings = append(warnings, map[string]any{
					"kind":  "large_checkout",
					"bytes": size,
					"text":  "The checkout is " + sizeText(size) + ": the task's worktree is a copy of it.",
				})
			}
		}
	}
	if s.Reads == nil {
		return nil, http.StatusInternalServerError, errNoDatabase
	}
	tasks, err := s.Reads.ActiveWorkspaces(ctx)
	if err != nil {
		return nil, http.StatusInternalServerError, err
	}
	shared := sharedTasks(ctx, workspace, tasks)
	if len(shared) > 0 {
		text := "Also in progress on this repository: " + sharedNames(shared) + ". A task in a worktree works on a branch of its own, so they do not get in each other's way; their work may conflict when you merge it."
		if overwrites(effective, shared) {
			text = "Also in progress on this repository: " + sharedNames(shared) + ". Tasks in place work in the same files and may overwrite each other's changes."
		}
		warnings = append(warnings, map[string]any{
			"kind":  "shared",
			"tasks": shared,
			"text":  text,
		})
	}
	return map[string]any{
		"workspace":      workspace,
		"mode":           effective,
		"requested_mode": mode,
		"git":            git,
		"warnings":       warnings,
		"notes":          notesView(ctx, workspace),
		"default_mode":   defaultMode,
		"notes_on":       notesOn,
	}, http.StatusOK, nil
}

func sharedTasks(ctx context.Context, workspace string, tasks []db.ActiveWorkspace) []any {
	key := notesKey(ctx, workspace)
	out := []any{}
	for _, task := range tasks {
		if task.Workspace == "" {
			continue
		}
		if notesKey(ctx, task.Workspace) != key {
			continue
		}
		out = append(out, map[string]any{
			"task_id": task.ID,
			"name":    task.Name,
			"status":  task.Status,
			"mode":    task.Mode,
		})
	}
	return out
}

func sharedNames(tasks []any) string {
	n := len(tasks)
	if n > 5 {
		n = 5
	}
	parts := make([]string, n)
	for i := 0; i < n; i++ {
		row := tasks[i].(map[string]any)
		parts[i] = fmt.Sprintf("%s (%s)", row["name"], row["status"])
	}
	return strings.Join(parts, ", ")
}

func overwrites(mode string, tasks []any) bool {
	if mode != workspaceInPlace {
		return false
	}
	for _, task := range tasks {
		row := task.(map[string]any)
		if row["mode"] == workspaceInPlace {
			return true
		}
	}
	return false
}

func filesText(paths []string) string {
	n := 10
	if len(paths) < n {
		n = len(paths)
	}
	text := strings.Join(paths[:n], ", ")
	if len(paths) > 10 {
		text += fmt.Sprintf(" (+%d more)", len(paths)-10)
	}
	return text
}

func shortBranch(ref string) any {
	name := strings.TrimPrefix(ref, "refs/heads/")
	if name == "" {
		return nil
	}
	return name
}

func sizeText(size int64) string {
	type unit struct {
		name  string
		scale int64
	}
	for _, item := range []unit{{"GiB", 1 << 30}, {"MiB", 1 << 20}, {"KiB", 1 << 10}} {
		if size >= item.scale {
			text := fmt.Sprintf("%.1f %s", float64(size)/float64(item.scale), item.name)
			return strings.Replace(text, ".0 ", " ", 1)
		}
	}
	return fmt.Sprintf("%d B", size)
}

// pyStrip is str.strip. pythonStrip in this package omits U+001C–U+001F
// because int() still rejects them. str.strip removes them.
func pyStrip(s string) string {
	return strings.TrimFunc(s, func(r rune) bool {
		return unicode.IsSpace(r) || (r >= 0x1C && r <= 0x1F)
	})
}

// expandUser is os.path.expanduser. "~" and "~/" use the home directory.
// "~name" and "~name/rest" use that account's home. An unknown account is
// left unchanged, so a later absolute-path check rejects it.
func expandUser(path string) string {
	if path == "" || path[0] != '~' {
		return path
	}
	name, suffix, slash := strings.Cut(path[1:], "/")
	home, ok := accountHome(name)
	if !ok {
		return path
	}
	if !slash {
		return home
	}
	return home + "/" + suffix
}

func accountHome(name string) (string, bool) {
	if name == "" {
		home, err := os.UserHomeDir()
		if err != nil || home == "" {
			return "", false
		}
		return home, true
	}
	account, err := user.Lookup(name)
	if err != nil || account.HomeDir == "" {
		return "", false
	}
	return account.HomeDir, true
}

// pathClean is os.path.normpath for an absolute Unix path: "." and ".." are
// lexical, and a ".." at the root stays at the root.
func pathClean(path string) string {
	if path == "" {
		return "."
	}
	abs := strings.HasPrefix(path, "/")
	parts := strings.Split(path, "/")
	stack := make([]string, 0, len(parts))
	for _, part := range parts {
		switch part {
		case "", ".":
			continue
		case "..":
			if len(stack) > 0 {
				stack = stack[:len(stack)-1]
			}
		default:
			stack = append(stack, part)
		}
	}
	cleaned := strings.Join(stack, "/")
	if abs {
		return "/" + cleaned
	}
	if cleaned == "" {
		return "."
	}
	return cleaned
}
