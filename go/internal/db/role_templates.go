package db

import (
	"context"
	"fmt"
	"strings"
)

var roleTemplateSQL = `SELECT t.project_id, t.template_key, t.kind, t.description, t.role_prompt, t.harness, ` +
	`t.profile_id, t.items, t.updated_at, a.name AS profile_name, ` +
	profileAllowedSQL("t.project_id", "t.profile_id") + ` AS profile_allowed ` +
	`FROM project_role_templates t LEFT JOIN agent_profiles a ON a.id = t.profile_id `

const roleItemSQL = `SELECT i.id, i.kind, i.name, i.description ` +
	`FROM library_installs s JOIN library_items i ON i.id = s.item_id ` +
	`WHERE s.enabled AND 'task_role' = ANY(s.roles) AND (s.scope = 'global' OR s.project_id = $1) ` +
	`ORDER BY i.kind, i.name COLLATE "C", i.owner_project_id NULLS FIRST, s.scope`

// RoleTemplates is role_api.templates_view. An unknown project is ErrProjectNotFound.
func (s *Store) RoleTemplates(ctx context.Context, projectID string) (map[string]any, error) {
	project, err := s.one(ctx, "project", `SELECT id, role_text_max_chars FROM projects WHERE id = $1`, projectID)
	if err != nil {
		return nil, err
	}
	if project == nil {
		return nil, ErrProjectNotFound
	}
	rows, err := s.query(ctx, "role templates", roleTemplateSQL+`WHERE t.project_id = $1 ORDER BY t.template_key`, projectID)
	if err != nil {
		return nil, err
	}
	templates := make([]any, 0, len(rows))
	for _, row := range rows {
		items, err := s.namedItems(ctx, textList(row["items"]))
		if err != nil {
			return nil, err
		}
		var profile any
		if row["profile_id"] != nil && asString(row["profile_id"]) != "" {
			profile = map[string]any{
				"profile_id": row["profile_id"],
				"name":       row["profile_name"],
				"allowed":    asBool(row["profile_allowed"]),
			}
		}
		templates = append(templates, map[string]any{
			"key":         row["template_key"],
			"kind":        row["kind"],
			"description": row["description"],
			"role_prompt": row["role_prompt"],
			"harness":     row["harness"],
			"profile":     profile,
			"items":       items,
			"updated_at":  row["updated_at"],
		})
	}
	roleItems, err := s.roleItems(ctx, projectID)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"project_id":          projectID,
		"role_text_max_chars": roleTextLimit(project),
		"templates":           templates,
		"role_items":          roleItems,
	}, nil
}

// roleItems is task_roles.role_items. The same item installed twice keeps the
// first position and the last row's view, which is what a dict comprehension does.
func (s *Store) roleItems(ctx context.Context, projectID string) ([]any, error) {
	rows, err := s.query(ctx, "role items", roleItemSQL, projectID)
	if err != nil {
		return nil, err
	}
	out := []any{}
	at := map[string]int{}
	for _, row := range rows {
		id := asString(row["id"])
		view := map[string]any{
			"id":          id,
			"kind":        row["kind"],
			"name":        row["name"],
			"description": oneLine(row["description"], 200),
		}
		if i, ok := at[id]; ok {
			out[i] = view
			continue
		}
		at[id] = len(out)
		out = append(out, view)
	}
	return out, nil
}

func oneLine(v any, limit int) string {
	var text string
	switch t := v.(type) {
	case nil:
	case string:
		text = t
	default:
		text = fmt.Sprint(t)
	}
	// str.split treats U+001C–U+001F as whitespace. strings.Fields does not.
	text = strings.Join(pythonFields(text), " ")
	runes := []rune(text)
	if len(runes) > limit {
		return string(runes[:limit])
	}
	return text
}
