package db

import (
	"context"
	"fmt"
	"sort"
	"time"
)

const projectTasksSQL = `SELECT t.id FROM tasks t WHERE t.project_id = $1 ` +
	`AND ($2::timestamptz IS NULL OR t.created_at >= $2) ` +
	`AND EXISTS (SELECT 1 FROM task_roles r WHERE r.task_id = t.id) ` +
	`ORDER BY t.created_at, t.id`

const taskStepsSQL = `SELECT * FROM task_steps WHERE task_id = $1 ORDER BY step_index`

// taskRoleSource is one task's versions, role runs, and steps.
type taskRoleSource struct {
	versions []map[string]any
	runs     []map[string]any
	steps    []map[string]any
}

// ProjectRoleStats is task_roles.project_role_stats. since nil is every task.
// The project check happens before this, matching role_api.role_stats.
func (s *Store) ProjectRoleStats(ctx context.Context, projectID string, since *time.Time) (map[string]any, error) {
	var sinceArg any
	if since != nil {
		sinceArg = *since
	}
	rows, err := s.query(ctx, "project tasks", projectTasksSQL, projectID, sinceArg)
	if err != nil {
		return nil, err
	}
	sources := make([]taskRoleSource, 0, len(rows))
	for _, row := range rows {
		id := asString(row["id"])
		versions, err := s.query(ctx, "task roles", roleVersionsSQL, id)
		if err != nil {
			return nil, err
		}
		runs, err := s.query(ctx, "role runs", roleRunsSQL, id)
		if err != nil {
			return nil, err
		}
		steps, err := s.query(ctx, "task steps", taskStepsSQL, id)
		if err != nil {
			return nil, err
		}
		sources = append(sources, taskRoleSource{versions: versions, runs: runs, steps: steps})
	}
	body, err := aggregateProjectRoles(projectID, sources)
	if err != nil {
		return nil, fmt.Errorf("project role stats %s: %w", projectID, err)
	}
	return body, nil
}

// ProjectExists reports whether projects has id. A missing project is false, nil.
func (s *Store) ProjectExists(ctx context.Context, id string) (bool, error) {
	row, err := s.one(ctx, "project", `SELECT id FROM projects WHERE id = $1`, id)
	if err != nil {
		return false, err
	}
	return row != nil, nil
}

type roleAccum struct {
	id        string
	kinds     map[string]struct{}
	tasks     int
	templates map[string]struct{}
	inputs    roleInputs
}

// aggregateProjectRoles sums each role across tasks, then computes each rate
// once. Summing the already-rounded rates would not match Python.
func aggregateProjectRoles(projectID string, tasks []taskRoleSource) (map[string]any, error) {
	byKey := map[string]*roleAccum{}
	var order []string
	var worker, reviewer roleInputs
	for _, task := range tasks {
		taskWorker, taskReviewer := builtinInputs(task.runs, task.steps)
		worker.add(taskWorker)
		reviewer.add(taskReviewer)
		for _, rows := range groupRoleVersions(task.versions) {
			if len(rows) == 0 {
				continue
			}
			current := rows[len(rows)-1]
			key := asString(current["role_key"])
			kind := asString(current["kind"])
			if kind != "work" && kind != "review" {
				return nil, fmt.Errorf("role %s has kind %q", key, kind)
			}
			entry := byKey[key]
			if entry == nil {
				entry = &roleAccum{
					id:        key,
					kinds:     map[string]struct{}{},
					templates: map[string]struct{}{},
				}
				byKey[key] = entry
				order = append(order, key)
			}
			entry.kinds[kind] = struct{}{}
			entry.tasks++
			for _, row := range rows {
				if row["template_key"] != nil && asString(row["template_key"]) != "" {
					entry.templates[asString(row["template_key"])] = struct{}{}
				}
			}
			entry.inputs.add(roleInputsFor(kind, key, task.runs, task.steps))
		}
	}
	sort.Slice(order, func(i, j int) bool {
		left, right := byKey[order[i]], byKey[order[j]]
		if left.tasks != right.tasks {
			return left.tasks > right.tasks
		}
		return left.id < right.id
	})
	roles := make([]any, 0, len(order))
	for _, key := range order {
		entry := byKey[key]
		roles = append(roles, map[string]any{
			"id":        entry.id,
			"kinds":     sortedStrings(entry.kinds),
			"tasks":     entry.tasks,
			"templates": sortedStrings(entry.templates),
			"stats":     entry.inputs.stats(),
		})
	}
	return map[string]any{
		"project_id": projectID,
		"tasks":      len(tasks),
		"roles":      roles,
		"builtin": map[string]any{
			"worker":   worker.stats(),
			"reviewer": reviewer.stats(),
		},
	}, nil
}

func roleInputsFor(kind, key string, runs, steps []map[string]any) roleInputs {
	if kind == "work" {
		return roleInputs{
			runs:    runsOf(runs, "worker", key),
			steps:   stepsWithRole(steps, key),
			reviews: reviewsOf(runs, steps, "work", key),
		}
	}
	own := runsOf(runs, "reviewer", key)
	return roleInputs{runs: own, steps: stepsReviewedBy(steps, key), reviews: own}
}

func groupRoleVersions(rows []map[string]any) [][]map[string]any {
	var groups [][]map[string]any
	at := map[string]int{}
	for _, row := range rows {
		key := asString(row["role_key"])
		i, ok := at[key]
		if !ok {
			at[key] = len(groups)
			groups = append(groups, nil)
			i = at[key]
		}
		groups[i] = append(groups[i], row)
	}
	return groups
}

func sortedStrings(set map[string]struct{}) []any {
	keys := make([]string, 0, len(set))
	for key := range set {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make([]any, len(keys))
	for i, key := range keys {
		out[i] = key
	}
	return out
}
