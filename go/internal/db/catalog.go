package db

import (
	"context"
	"errors"
	"regexp"
	"sort"
	"strings"
)

var ErrCatalogItemNotFound = errors.New("catalog item not found")

// Only key names leave PostgreSQL: MCP environment/header values never enter
// catalog rows, even before the public view masks them.
const catalogItemsSQL = `SELECT i.id, i.kind, i.name, i.origin, i.owner_project_id,
 p.name AS owner_project_name, i.description, i.source, i.version, i.sha256,
 i.size_bytes, i.file_count, i.modified, i.imported_at, i.updated_at,
 m.transport AS mcp_transport, m.command AS mcp_command, m.args AS mcp_args, m.url AS mcp_url,
 ARRAY(SELECT k FROM jsonb_object_keys(m.env) k ORDER BY k COLLATE "C") AS mcp_env_keys,
 ARRAY(SELECT k FROM jsonb_object_keys(m.headers) k ORDER BY k COLLATE "C") AS mcp_header_keys
 FROM library_items i LEFT JOIN projects p ON p.id = i.owner_project_id
 LEFT JOIN library_mcp_servers m ON m.item_id = i.id`

const catalogInstallsSQL = `SELECT s.id, s.item_id, s.scope, s.project_id,
 p.name AS project_name, s.roles, s.enabled, s.created_at, s.updated_at
 FROM library_installs s LEFT JOIN projects p ON p.id = s.project_id`

func (s *Store) listCatalogItems(ctx context.Context) ([]map[string]any, error) {
	return s.query(ctx, "catalog items", catalogItemsSQL+` ORDER BY i.kind, i.name COLLATE "C", i.owner_project_id NULLS FIRST`)
}

func (s *Store) listCatalogInstalls(ctx context.Context, itemID, projectID string) ([]map[string]any, error) {
	return s.query(ctx, "catalog installs", catalogInstallsSQL+`
 WHERE ($1::text IS NULL OR s.item_id = $1) AND ($2::text IS NULL OR s.scope = 'global' OR s.project_id = $2)
 ORDER BY s.scope, p.name COLLATE "C" NULLS FIRST, s.id`, nullableCatalogID(itemID), nullableCatalogID(projectID))
}

func nullableCatalogID(id string) any {
	if id == "" {
		return nil
	}
	return id
}

func catalogQualifiedName(row map[string]any) string {
	name := asString(row["name"])
	switch row["kind"] {
	case "mcp":
		return name
	case "plugin":
		directory, _, _ := strings.Cut(name, "@")
		return directory
	default:
		if row["origin"] == "project" {
			return "project:" + name
		}
		return "personal:" + name
	}
}

func catalogInstallView(row map[string]any) map[string]any {
	return map[string]any{
		"install_id": row["id"], "item_id": row["item_id"], "scope": row["scope"],
		"project_id": row["project_id"], "project_name": row["project_name"],
		"roles": stringList(row["roles"]), "enabled": asBool(row["enabled"]),
		"created_at": row["created_at"], "updated_at": row["updated_at"],
	}
}

func catalogMCPView(row map[string]any) map[string]any {
	masked := func(raw any) map[string]any {
		out := map[string]any{}
		for _, key := range stringList(raw) {
			out[asString(key)] = "***"
		}
		return out
	}
	return map[string]any{
		"transport": row["mcp_transport"], "command": asString(row["mcp_command"]),
		"args": stringList(row["mcp_args"]), "url": asString(row["mcp_url"]),
		"env": masked(row["mcp_env_keys"]), "headers": masked(row["mcp_header_keys"]),
	}
}

func catalogItemView(row map[string]any, installs []map[string]any) map[string]any {
	views := make([]map[string]any, 0, len(installs))
	for _, install := range installs {
		views = append(views, catalogInstallView(install))
	}
	var mcp any
	if row["kind"] == "mcp" {
		mcp = catalogMCPView(row)
	}
	return map[string]any{
		"item_id": row["id"], "kind": row["kind"], "name": row["name"],
		"qualified_name": catalogQualifiedName(row), "origin": row["origin"],
		"owner_project_id": row["owner_project_id"], "owner_project_name": row["owner_project_name"],
		"description": row["description"], "source": row["source"], "version": row["version"], "sha256": row["sha256"],
		"size_bytes": row["size_bytes"], "file_count": row["file_count"], "modified": asBool(row["modified"]),
		"imported_at": row["imported_at"], "updated_at": row["updated_at"], "mcp": mcp, "installs": views,
	}
}

// Catalog is catalog.catalog_view: acquired items and all installs, including
// disabled ones. installed_count counts distinct items, not enabled installs.
func (s *Store) Catalog(ctx context.Context) (map[string]any, error) {
	items, err := s.listCatalogItems(ctx)
	if err != nil {
		return nil, err
	}
	installs, err := s.listCatalogInstalls(ctx, "", "")
	if err != nil {
		return nil, err
	}
	settings, err := s.query(ctx, "catalog import reports", `SELECT key, value FROM system_settings WHERE key = ANY($1::text[])`, []string{"library.last_import", "library.last_mcp_import"})
	if err != nil {
		return nil, err
	}
	return catalogView(items, installs, settings), nil
}

func catalogView(items, installs, settings []map[string]any) map[string]any {
	byItem := map[string][]map[string]any{}
	for _, row := range installs {
		id := asString(row["item_id"])
		byItem[id] = append(byItem[id], row)
	}
	views := make([]map[string]any, 0, len(items))
	var total int64
	installed := 0
	for _, row := range items {
		rows := byItem[asString(row["id"])]
		views = append(views, catalogItemView(row, rows))
		total += int64(asInt(row["size_bytes"]))
		if len(rows) > 0 {
			installed++
		}
	}
	reports := map[string]any{}
	for _, row := range settings {
		var value any
		if err := unmarshalPythonJSON([]byte(asString(row["value"])), &value); err == nil {
			reports[asString(row["key"])] = value
		}
	}
	return map[string]any{
		"items": views, "installed_count": installed, "total_bytes": total,
		"last_import": reports["library.last_import"], "last_mcp_import": reports["library.last_mcp_import"],
		"roles":      []string{"manager", "planner", "worker", "reviewer", "task_role"},
		"task_roles": catalogTaskRoles(),
		"caps": map[string]any{"file_max_bytes": 2 * 1024 * 1024, "item_max_bytes": 32 * 1024 * 1024,
			"item_max_files": 5000, "library_max_bytes": 128 * 1024 * 1024, "upload_max_bytes": 6000000},
		"mcp_caps": map[string]any{"servers_max": 200, "variables_max": 50, "value_max": 8192, "args_max": 100, "mask": "***"},
	}
}

// CatalogItem returns item_detail. Only SKILL.md content is fetched, separately
// from the file listing. Plugin files are never read by this API.
func (s *Store) CatalogItem(ctx context.Context, id string) (map[string]any, error) {
	item, err := s.one(ctx, "catalog item", catalogItemsSQL+` WHERE i.id = $1`, id)
	if err != nil {
		return nil, err
	}
	if item == nil {
		return nil, ErrCatalogItemNotFound
	}
	installs, err := s.listCatalogInstalls(ctx, id, "")
	if err != nil {
		return nil, err
	}
	files, err := s.query(ctx, "catalog file listing", `SELECT path, octet_length(content) AS size_bytes, executable FROM library_files WHERE item_id = $1 ORDER BY path`, id)
	if err != nil {
		return nil, err
	}
	var skillMD, content any
	if item["kind"] == "skill" {
		file, err := s.one(ctx, "catalog skill content", `SELECT content FROM library_files WHERE item_id = $1 AND path = 'SKILL.md'`, id)
		if err != nil {
			return nil, err
		}
		if file != nil {
			skillMD = file["content"]
			content = catalogSkillBody(asString(skillMD))
		}
	}
	return map[string]any{"item": catalogItemView(item, installs), "files": files, "skill_md": skillMD, "content": content}, nil
}

var catalogLeadingBlankLines = regexp.MustCompile(`^(?:[ \t]*\r?\n)+`)

// The body half of catalog_sources.split_skill_md; do not parse or reformat YAML.
func catalogSkillBody(text string) string {
	lines := strings.Split(strings.TrimPrefix(text, "\ufeff"), "\n")
	if pythonStrip(strings.TrimRight(lines[0], "\r")) != "---" {
		return text
	}
	for end := 1; end < len(lines); end++ {
		if pythonStrip(strings.TrimRight(lines[end], "\r")) == "---" {
			return catalogLeadingBlankLines.ReplaceAllString(strings.Join(lines[end+1:], "\n"), "")
		}
	}
	return text
}

func catalogTaskRoles() []string {
	return []string{"planner", "worker", "reviewer", "task_role"}
}

// ProjectInstalls includes the project's explicit installs and inherited global
// installs reaching task roles, with each nested item carrying an empty installs list.
func (s *Store) ProjectInstalls(ctx context.Context, projectID string) (map[string]any, error) {
	exists, err := s.ProjectExists(ctx, projectID)
	if err != nil {
		return nil, err
	}
	if !exists {
		return nil, ErrProjectNotFound
	}
	installs, err := s.listCatalogInstalls(ctx, "", projectID)
	if err != nil {
		return nil, err
	}
	items, err := s.listCatalogItems(ctx)
	if err != nil {
		return nil, err
	}
	return projectInstallsView(projectID, items, installs), nil
}

func projectInstallsView(projectID string, items, installs []map[string]any) map[string]any {
	byID := map[string]map[string]any{}
	owned := 0
	for _, row := range items {
		byID[asString(row["id"])] = row
		if row["owner_project_id"] == projectID {
			owned++
		}
	}
	entries := []map[string]any{}
	for _, row := range installs {
		inherited := row["scope"] == "global"
		if inherited {
			reachesTask := false
			for _, role := range stringList(row["roles"]) {
				switch role {
				case "planner", "worker", "reviewer", "task_role":
					reachesTask = true
				}
			}
			if !reachesTask {
				continue
			}
		}
		item, ok := byID[asString(row["item_id"])]
		if !ok {
			continue
		}
		entry := catalogInstallView(row)
		entry["inherited"], entry["item"] = inherited, catalogItemView(item, nil)
		entries = append(entries, entry)
	}
	sort.SliceStable(entries, func(i, j int) bool {
		a, b := entries[i], entries[j]
		if a["inherited"] != b["inherited"] {
			return !asBool(a["inherited"])
		}
		x, y := a["item"].(map[string]any), b["item"].(map[string]any)
		if x["kind"] != y["kind"] {
			return asString(x["kind"]) < asString(y["kind"])
		}
		return asString(x["name"]) < asString(y["name"])
	})
	return map[string]any{
		"project_id": projectID, "installs": entries, "owned_count": owned, "task_roles": catalogTaskRoles(),
		"skill_caps": map[string]any{"name_max_chars": 64, "description_max_chars": 1024, "content_max_bytes": 64 * 1024, "project_max_skills": 20},
	}
}
