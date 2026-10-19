package db

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/foolzzz/looper/go/internal/configtransfer"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
)

// PythonVersion is looper.__version__ on the parity target. A configuration
// export writes it as looper_version.
const PythonVersion = "2.11.0"

// Each read is deliberately separate from dashboard reads: only this explicit
// download path may read provider keys and unmasked MCP values.
var configExportSQL = map[string]string{
	"layers":    `SELECT l.scope,l.role,l.project_id,l.harness_prompt,l.profile_id,a.name AS profile,l.env,l.run_budget_usd,l.max_concurrency,l.boundaries,l.system_prompt_mode FROM agent_setting_layers l LEFT JOIN agent_profiles a ON a.id=l.profile_id ORDER BY l.project_id NULLS FIRST,l.scope,l.role NULLS FIRST`,
	"runtimes":  `SELECT id,name,type,executable,capabilities,builtin FROM agent_runtimes ORDER BY name COLLATE "C"`,
	"providers": `SELECT p.id,p.name,p.protocols,p.auth,p.oauth_runtime_type,p.base_url,p.models,p.env,p.max_concurrency,p.builtin,s.api_key FROM llm_providers p LEFT JOIN llm_provider_secrets s ON s.provider_id=p.id ORDER BY p.name COLLATE "C"`,
	"profiles":  `SELECT a.id,a.name,r.name AS runtime,p.name AS provider,a.model,a.effort,a.agent_teams,a.custom_prompt,a.builtin FROM agent_profiles a JOIN agent_runtimes r ON r.id=a.runtime_id JOIN llm_providers p ON p.id=a.provider_id ORDER BY a.name COLLATE "C"`,
	"roles":     `SELECT role,system_prompt,default_prompt,timeout_seconds,enabled FROM role_configs WHERE role=ANY(ARRAY['manager','planner','worker','reviewer']) ORDER BY role`,
	"items":     `SELECT i.id,i.kind,i.name,i.origin,i.owner_project_id,p.name AS owner_project_name,i.description,i.source,i.version,i.sha256,i.size_bytes,i.file_count,i.modified,m.transport,m.command,m.args,m.env,m.url,m.headers FROM library_items i LEFT JOIN projects p ON p.id=i.owner_project_id LEFT JOIN library_mcp_servers m ON m.item_id=i.id ORDER BY i.owner_project_id NULLS FIRST,i.kind,i.name COLLATE "C"`,
	"files":     `SELECT path,content,sha256,executable FROM library_files WHERE item_id=$1 ORDER BY path COLLATE "C"`,
	"installs":  `SELECT s.id,s.item_id,s.scope,s.project_id,p.name AS project_name,s.roles,s.enabled,i.kind,i.name,o.name AS owner_project_name FROM library_installs s JOIN library_items i ON i.id=s.item_id LEFT JOIN projects p ON p.id=s.project_id LEFT JOIN projects o ON o.id=i.owner_project_id ORDER BY s.scope,p.name COLLATE "C" NULLS FIRST,i.kind,i.name COLLATE "C",o.name COLLATE "C"`,
	"channels":  `SELECT channel_id,channel_type,display_name,enabled,config FROM channel_configs ORDER BY channel_id`,
	"projects":  `SELECT id,name,description,background,` + strings.Join(executionFields, ",") + ` FROM projects ORDER BY lower(name)`,
	"documents": `SELECT id,project_id,title,summary,source_kind,filename,path,size_bytes,sha256,content FROM project_documents ORDER BY project_id,created_at,id`,
	"templates": `SELECT t.project_id,t.template_key,t.kind,t.description,t.role_prompt,t.harness,t.profile_id,t.items,a.name AS profile FROM project_role_templates t LEFT JOIN agent_profiles a ON a.id=t.profile_id ORDER BY t.project_id,t.template_key`,
	"schedules": `SELECT s.*,p.name AS project_name FROM schedules s JOIN projects p ON p.id=s.project_id ORDER BY lower(p.name),p.id,lower(s.name),s.id`,
}

type exportRead func(context.Context, string, string) ([]map[string]any, error)

// ExportConfig streams a download from one read-only repeatable-read snapshot.
// It never calls schema migration or the registry's local credential probes.
func (s *Store) ExportConfig(ctx context.Context, out io.Writer, opts configtransfer.Options) (summary configtransfer.Summary, err error) {
	if s == nil || s.DB == nil {
		return summary, fmt.Errorf("export configuration: %w", errNoExportDatabase)
	}
	conn, err := s.DB.Conn(ctx)
	if err != nil {
		return summary, fmt.Errorf("export connection: %w", err)
	}
	defer conn.Close()
	err = conn.Raw(func(driver any) error {
		std, ok := driver.(*stdlib.Conn)
		if !ok {
			return fmt.Errorf("export: unexpected driver connection")
		}
		tx, err := std.Conn().BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly})
		if err != nil {
			return fmt.Errorf("export snapshot: %w", err)
		}
		defer func() {
			cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Second)
			defer cancel()
			_ = tx.Rollback(cleanup)
		}()
		read := func(ctx context.Context, section, id string) ([]map[string]any, error) {
			query, ok := configExportSQL[section]
			if !ok {
				return nil, fmt.Errorf("unknown export section %q", section)
			}
			args := []any{}
			if section == "files" {
				args = append(args, id)
			}
			rows, err := exportQuery(ctx, tx, query, args...)
			if err != nil {
				return nil, fmt.Errorf("export %s: %w", section, err)
			}
			if section == "schedules" {
				ids := []string{}
				byID := map[string]map[string]any{}
				for _, row := range rows {
					id := asString(row["id"])
					ids = append(ids, id)
					byID[id] = row
					row["profile_overrides"] = map[string]any{}
					row["task_limits"] = map[string]any{}
				}
				for _, extra := range []struct{ key, column, table string }{{"profile_overrides", "profile_id", "schedule_profile_overrides"}, {"task_limits", "max_concurrency", "schedule_task_limits"}} {
					values, err := exportQuery(ctx, tx, "SELECT schedule_id,role,"+extra.column+" FROM "+extra.table+" WHERE schedule_id=ANY($1::text[]) ORDER BY role", ids)
					if err != nil {
						return nil, fmt.Errorf("export schedule %s: %w", extra.key, err)
					}
					for _, v := range values {
						byID[asString(v["schedule_id"])][extra.key].(map[string]any)[asString(v["role"])] = v[extra.column]
					}
				}
			}
			return rows, nil
		}
		summary, err = exportConfig(ctx, out, opts, read)
		if err != nil {
			return err
		}
		if err := tx.Commit(ctx); err != nil {
			return fmt.Errorf("export commit snapshot: %w", err)
		}
		return nil
	})
	return summary, err
}

var errNoExportDatabase = fmt.Errorf("no database")

func exportQuery(ctx context.Context, tx pgx.Tx, query string, args ...any) ([]map[string]any, error) {
	rows, err := tx.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []map[string]any{}
	fields := rows.FieldDescriptions()
	for rows.Next() {
		values, err := rows.Values()
		if err != nil {
			return nil, err
		}
		row := map[string]any{}
		for i, f := range fields {
			v, err := coerceColumn(f.DataTypeOID, values[i])
			if err != nil {
				return nil, fmt.Errorf("column %s: %w", f.Name, err)
			}
			row[f.Name] = v
		}
		out = append(out, row)
	}
	return out, rows.Err()
}
func exportConfig(ctx context.Context, out io.Writer, opts configtransfer.Options, read exportRead) (summary configtransfer.Summary, err error) {
	summary.Counts = map[string]int{}
	write := func(raw string) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		n, err := io.WriteString(out, raw)
		summary.Bytes += int64(n)
		if err == nil && n != len(raw) {
			err = io.ErrShortWrite
		}
		if err != nil {
			return fmt.Errorf("write configuration: %w", err)
		}
		return nil
	}
	dump := func(v any) (string, error) {
		raw, err := exportDump(v)
		if err != nil {
			return "", fmt.Errorf("encode configuration: %w", err)
		}
		return string(raw), nil
	}
	headRows := map[string][]map[string]any{}
	// Python reads these before yielding the header, so a failed read still
	// produces a normal HTTP error rather than a partial download.
	for _, section := range []string{"layers", "items", "installs", "channels", "providers", "runtimes", "profiles", "roles"} {
		rows, e := read(ctx, section, "")
		if e != nil {
			return summary, e
		}
		headRows[section] = rows
	}
	now := opts.Now
	if now.IsZero() {
		now = time.Now()
	}
	version := opts.Version
	if version == "" {
		version = PythonVersion
	}
	secrets := "omitted"
	if opts.IncludeSecrets {
		secrets = "included"
	}
	head := exportObject{{Key: "format", Value: "looper-config/v2"}, {Key: "looper_version", Value: version}, {Key: "exported_at", Value: now.UTC().Format("2006-01-02T15:04:05+00:00")}, {Key: "secrets", Value: secrets}}
	for _, section := range []string{"runtimes", "providers", "profiles"} {
		entries := []any{}
		for _, row := range headRows[section] {
			if asBool(row["builtin"]) {
				continue
			}
			var entry any
			switch section {
			case "runtimes":
				entry = exportRuntime(row)
			case "profiles":
				entry = exportProfile(row)
			case "providers":
				var e error
				entry, e = exportProvider(row, opts.IncludeSecrets)
				if e != nil {
					return summary, e
				}
				if row["api_key"] != nil {
					summary.Secrets++
				}
			}
			entries = append(entries, entry)
		}
		summary.Counts[section] = len(entries)
		head = append(head, exportField{Key: section, Value: entries})
	}
	layers := []any{}
	for _, row := range headRows["layers"] {
		if row["project_id"] == nil {
			layers = append(layers, exportLayer(row))
		}
	}
	roles := []any{}
	for _, row := range headRows["roles"] {
		roles = append(roles, exportRole(row))
	}
	summary.Counts["settings.layers"] = len(layers)
	summary.Counts["settings.roles"] = len(roles)
	head = append(head, exportField{Key: "settings", Value: exportObject{{Key: "layers", Value: layers}, {Key: "roles", Value: roles}}})
	channels := []any{}
	for _, row := range headRows["channels"] {
		entry, n, e := exportChannel(row, opts.IncludeSecrets)
		if e != nil {
			return summary, e
		}
		summary.Secrets += n
		channels = append(channels, entry)
	}
	summary.Counts["channels"] = len(channels)
	installs := []any{}
	for _, row := range headRows["installs"] {
		if row["scope"] == "global" {
			installs = append(installs, exportInstall(row))
		}
	}
	summary.Counts["installs"] = len(installs)
	head = append(head, exportField{Key: "channels", Value: channels}, exportField{Key: "installs", Value: installs})
	chunks := []string{}
	for _, field := range head {
		k, e := dump(field.Key)
		if e != nil {
			return summary, e
		}
		v, e := dump(field.Value)
		if e != nil {
			return summary, e
		}
		chunks = append(chunks, k+":"+v)
	}
	if err = write("{" + strings.Join(chunks, ",\n")); err != nil {
		return summary, err
	}
	itemEntry := func(row map[string]any) (exportObject, int, error) {
		var files []map[string]any
		if row["kind"] != "mcp" || row["owner_project_id"] != nil {
			var e error
			files, e = read(ctx, "files", asString(row["id"]))
			if e != nil {
				return nil, 0, e
			}
		}
		entry, n := exportItem(row, files, opts.IncludeSecrets)
		return entry, n, nil
	}
	if opts.IncludeProjects {
		projectRows := map[string][]map[string]any{}
		for _, section := range []string{"documents", "templates", "schedules", "profiles", "projects"} {
			rows, e := read(ctx, section, "")
			if e != nil {
				return summary, e
			}
			projectRows[section] = rows
		}
		profileNames := map[string]string{}
		for _, row := range projectRows["profiles"] {
			profileNames[asString(row["id"])] = asString(row["name"])
		}
		byID := map[string]map[string]any{}
		for _, row := range headRows["items"] {
			byID[asString(row["id"])] = row
		}
		summary.Counts["projects"] = len(projectRows["projects"])
		summary.Counts["schedules"] = len(projectRows["schedules"])
		if err = write(",\n\"projects\":["); err != nil {
			return summary, err
		}
		for index, project := range projectRows["projects"] {
			id := project["id"]
			layers, knowledge, items, installs, templates, schedules := []any{}, []any{}, []any{}, []any{}, []any{}, []any{}
			for _, row := range headRows["layers"] {
				if row["project_id"] == id {
					layers = append(layers, exportLayer(row))
				}
			}
			for _, row := range projectRows["documents"] {
				if row["project_id"] == id {
					knowledge = append(knowledge, exportDocument(row))
					if row["source_kind"] == "path" {
						summary.Repoint++
					}
				}
			}
			for _, row := range headRows["items"] {
				if row["owner_project_id"] == id {
					entry, _, e := itemEntry(row)
					if e != nil {
						return summary, e
					}
					items = append(items, entry)
				}
			}
			for _, row := range headRows["installs"] {
				if row["project_id"] == id {
					installs = append(installs, exportInstall(row))
				}
			}
			for _, row := range projectRows["templates"] {
				if row["project_id"] == id {
					templates = append(templates, exportTemplate(row, byID))
				}
			}
			for _, row := range projectRows["schedules"] {
				if row["project_id"] == id {
					overrides, _ := row["profile_overrides"].(map[string]any)
					limits, _ := row["task_limits"].(map[string]any)
					schedules = append(schedules, exportSchedule(row, profileNames, overrides, limits))
				}
			}
			entry := exportFields(project, "name", "description", "background")
			entry = append(entry, exportField{Key: "execution", Value: exportFields(project, executionFields...)}, exportField{Key: "layers", Value: layers}, exportField{Key: "knowledge", Value: knowledge}, exportField{Key: "items", Value: items}, exportField{Key: "installs", Value: installs})
			if len(templates) > 0 {
				entry = append(entry, exportField{Key: "role_templates", Value: templates})
			}
			if len(schedules) > 0 {
				entry = append(entry, exportField{Key: "schedules", Value: schedules})
			}
			raw, e := dump(entry)
			if e != nil {
				return summary, e
			}
			prefix := "\n"
			if index > 0 {
				prefix = ",\n"
			}
			if err = write(prefix + raw); err != nil {
				return summary, err
			}
		}
		if err = write("]"); err != nil {
			return summary, err
		}
	}
	if err = write(",\n\"catalog\":["); err != nil {
		return summary, err
	}
	summary.Counts["catalog"] = 0
	for _, row := range headRows["items"] {
		if row["owner_project_id"] != nil {
			continue
		}
		entry, n, e := itemEntry(row)
		if e != nil {
			return summary, e
		}
		summary.Secrets += n
		prefix := "\n"
		if summary.Counts["catalog"] > 0 {
			prefix = ",\n"
		}
		raw, e := dump(entry)
		if e != nil {
			return summary, e
		}
		if err = write(prefix + raw); err != nil {
			return summary, err
		}
		summary.Counts["catalog"]++
	}
	err = write("]}\n")
	return summary, err
}

// WriteExport atomically replaces path only after a complete snapshot was
// written. Secret exports are owner-only; public exports honor the umask.
func (s *Store) WriteExport(ctx context.Context, path string, opts configtransfer.Options) (configtransfer.Summary, error) {
	return writeExport(ctx, path, opts, s.ExportConfig)
}

func writeExport(ctx context.Context, path string, opts configtransfer.Options, export func(context.Context, io.Writer, configtransfer.Options) (configtransfer.Summary, error)) (summary configtransfer.Summary, err error) {
	if path == "~" || strings.HasPrefix(path, "~/") {
		home, e := os.UserHomeDir()
		if e != nil {
			return summary, fmt.Errorf("export home: %w", e)
		}
		if path == "~" {
			path = home
		} else {
			path = filepath.Join(home, strings.TrimPrefix(path, "~/"))
		}
	}
	dir := filepath.Dir(path)
	handle, err := os.CreateTemp(dir, "."+filepath.Base(path)+".")
	if err != nil {
		return summary, fmt.Errorf("create export: %w", err)
	}
	temp := handle.Name()
	defer func() { _ = handle.Close(); _ = os.Remove(temp) }()
	if !opts.IncludeSecrets {
		// Reopen the exclusively owned random name with mode 0644 so the kernel
		// applies umask without changing the process-wide mask in a Go server.
		if err = handle.Close(); err != nil {
			return summary, fmt.Errorf("close export: %w", err)
		}
		if err = os.Remove(temp); err != nil {
			return summary, fmt.Errorf("prepare export: %w", err)
		}
		handle, err = os.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
		if err != nil {
			return summary, fmt.Errorf("create public export: %w", err)
		}
	}
	summary, err = export(ctx, handle, opts)
	if err != nil {
		return summary, err
	}
	if err = handle.Close(); err != nil {
		return summary, fmt.Errorf("close export: %w", err)
	}
	if err = ctx.Err(); err != nil {
		return summary, err
	}
	if err = os.Rename(temp, path); err != nil {
		return summary, fmt.Errorf("replace export: %w", err)
	}
	return summary, nil
}
