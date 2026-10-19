INSERT INTO projects (created_at, id, name, updated_at, work_dir) VALUES ('2020-01-01T00:00:00Z', 'proj-catalog01', 'Alpha catalog', '2020-01-02T03:04:05Z', '');
INSERT INTO projects (created_at, id, name, updated_at, work_dir) VALUES ('2020-01-01T00:00:00Z', 'proj-catalog02', 'Beta catalog', '2020-01-02T03:04:05Z', '');
INSERT INTO projects (created_at, id, name, updated_at, work_dir) VALUES ('2020-01-01T00:00:00Z', 'proj-catalog03', 'Empty catalog', '2020-01-02T03:04:05Z', '');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('machine plugin fixture', 2, 'lib-machineplugin01', '2020-01-01T00:00:00Z', 'plugin', FALSE, 'Zebra@market', 'machine', NULL, 'lib-machineplugin01-hash', 46, 'fixture/machine/Zebra@market', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('upload plugin fixture', 1, 'lib-uploadplugin01', '2020-01-01T00:00:00Z', 'plugin', TRUE, 'Alpha', 'upload', NULL, 'lib-uploadplugin01-hash', 33, 'fixture/upload/Alpha', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('git plugin fixture', 1, 'lib-gitplugin01', '2020-01-01T00:00:00Z', 'plugin', FALSE, 'alpha', 'git', NULL, 'lib-gitplugin01-hash', 11, 'fixture/git/alpha', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('machine skill fixture', 2, 'lib-machineskill01', '2020-01-01T00:00:00Z', 'skill', FALSE, 'machine-guide', 'machine', NULL, 'lib-machineskill01-hash', 106, 'fixture/machine/machine-guide', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('upload skill fixture', 2, 'lib-uploadskill01', '2020-01-01T00:00:00Z', 'skill', FALSE, 'upload-guide', 'upload', NULL, 'lib-uploadskill01-hash', 63, 'fixture/upload/upload-guide', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('git skill fixture', 2, 'lib-gitskill01', '2020-01-01T00:00:00Z', 'skill', TRUE, 'git-guide', 'git', NULL, 'lib-gitskill01-hash', 66, 'fixture/git/git-guide', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('ui skill fixture', 2, 'lib-uiskill01', '2020-01-01T00:00:00Z', 'skill', FALSE, 'ui-guide', 'ui', NULL, 'lib-uiskill01-hash', 51, 'fixture/ui/ui-guide', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('project skill fixture', 1, 'skill-ownedalpha01', '2020-01-01T00:00:00Z', 'skill', FALSE, 'shared-guide', 'project', 'proj-catalog01', 'skill-ownedalpha01-hash', 66, 'fixture/project/shared-guide', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('project skill fixture', 1, 'skill-ownedbeta01', '2020-01-01T00:00:00Z', 'skill', FALSE, 'shared-guide', 'project', 'proj-catalog02', 'skill-ownedbeta01-hash', 66, 'fixture/project/shared-guide', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('project skill fixture', 0, 'skill-missingfile01', '2020-01-01T00:00:00Z', 'skill', FALSE, 'no-file', 'project', 'proj-catalog01', 'skill-missingfile01-hash', 0, 'fixture/project/no-file', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('machine mcp fixture', 0, 'mcp-machine01', '2020-01-01T00:00:00Z', 'mcp', FALSE, 'mcp_stdio', 'machine', NULL, 'mcp-machine01-hash', 123, 'fixture/machine/mcp_stdio', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('ui mcp fixture', 0, 'mcp-http01', '2020-01-01T00:00:00Z', 'mcp', FALSE, 'mcp_http', 'ui', NULL, 'mcp-http01-hash', 123, 'fixture/ui/mcp_http', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_items (description, file_count, id, imported_at, kind, modified, name, origin, owner_project_id, sha256, size_bytes, source, updated_at, version) VALUES ('ui mcp fixture', 0, 'mcp-sse01', '2020-01-01T00:00:00Z', 'mcp', FALSE, 'mcp_sse', 'ui', NULL, 'mcp-sse01-hash', 123, 'fixture/ui/mcp_sse', '2020-01-02T03:04:05Z', '1.2.3');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('PLUGIN_FILE_CONTENT_SENTINEL
', TRUE, 'lib-machineplugin01', 'tools/run.sh', 'lib-machineplugin01-tools/run.sh-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('{"name":"Zebra"}
', FALSE, 'lib-machineplugin01', '.claude-plugin/plugin.json', 'lib-machineplugin01-.claude-plugin/plugin.json-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('UPLOADED_PLUGIN_CONTENT_SENTINEL
', FALSE, 'lib-uploadplugin01', 'README.md', 'lib-uploadplugin01-README.md-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('git plugin
', FALSE, 'lib-gitplugin01', 'README.md', 'lib-gitplugin01-README.md-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('---
name: machine-guide
description: Read machine
---

# Read
你好 🌍
', FALSE, 'lib-machineskill01', 'SKILL.md', 'lib-machineskill01-SKILL.md-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('REFERENCE_FILE_CONTENT_SENTINEL
', FALSE, 'lib-machineskill01', 'references/note.txt', 'lib-machineskill01-references/note.txt-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('plain body without frontmatter
', FALSE, 'lib-uploadskill01', 'SKILL.md', 'lib-uploadskill01-SKILL.md-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('REFERENCE_FILE_CONTENT_SENTINEL
', FALSE, 'lib-uploadskill01', 'references/note.txt', 'lib-uploadskill01-references/note.txt-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('---
name: git-guide
---

Git body
', FALSE, 'lib-gitskill01', 'SKILL.md', 'lib-gitskill01-SKILL.md-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('REFERENCE_FILE_CONTENT_SENTINEL
', FALSE, 'lib-gitskill01', 'references/note.txt', 'lib-gitskill01-references/note.txt-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('---
name: ui-guide
', FALSE, 'lib-uiskill01', 'SKILL.md', 'lib-uiskill01-SKILL.md-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('REFERENCE_FILE_CONTENT_SENTINEL
', FALSE, 'lib-uiskill01', 'references/note.txt', 'lib-uiskill01-references/note.txt-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('---
name: shared-guide
description: Owned guide
---

Project body
', FALSE, 'skill-ownedalpha01', 'SKILL.md', 'skill-ownedalpha01-SKILL.md-hash');
INSERT INTO library_files (content, executable, item_id, path, sha256) VALUES ('---
name: shared-guide
description: Owned guide
---

Project body
', FALSE, 'skill-ownedbeta01', 'SKILL.md', 'skill-ownedbeta01-SKILL.md-hash');
INSERT INTO library_mcp_servers (args, command, env, headers, item_id, transport, url) VALUES (ARRAY['--unicode','中文','--flag=<fixture>']::text[], 'fixture-command', '{"A_EMPTY": "", "Z_TOKEN": "MCP_ENV_SECRET_SENTINEL"}', '{}', 'mcp-machine01', 'stdio', '');
INSERT INTO library_mcp_servers (args, command, env, headers, item_id, transport, url) VALUES (ARRAY[]::text[], '', '{}', '{"Authorization": "MCP_AUTH_SECRET_SENTINEL", "X-Token": "MCP_HEADER_SECRET_SENTINEL"}', 'mcp-http01', 'http', 'https://example.invalid/mcp');
INSERT INTO library_mcp_servers (args, command, env, headers, item_id, transport, url) VALUES (ARRAY[]::text[], '', '{}', '{"Authorization": "MCP_AUTH_SECRET_SENTINEL", "X-Token": "MCP_HEADER_SECRET_SENTINEL"}', 'mcp-sse01', 'sse', 'https://example.invalid/mcp');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', TRUE, 'inst-globalmanager01', 'lib-machineplugin01', NULL, ARRAY['manager']::text[], 'global', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', TRUE, 'inst-globaltask01', 'lib-uploadplugin01', NULL, ARRAY['task_role']::text[], 'global', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', TRUE, 'inst-globalmixed01', 'lib-machineskill01', NULL, ARRAY['manager','planner','worker','reviewer','task_role']::text[], 'global', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', FALSE, 'inst-globaldisabled01', 'mcp-http01', NULL, ARRAY['reviewer']::text[], 'global', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', TRUE, 'inst-globalstdio01', 'mcp-machine01', NULL, ARRAY['worker']::text[], 'global', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', TRUE, 'inst-localplugin01', 'lib-gitplugin01', 'proj-catalog01', ARRAY['planner','worker','reviewer','task_role']::text[], 'project', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', FALSE, 'inst-localoverride01', 'lib-machineskill01', 'proj-catalog01', ARRAY['task_role']::text[], 'project', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', TRUE, 'inst-localowned01', 'skill-ownedalpha01', 'proj-catalog01', ARRAY['worker']::text[], 'project', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', FALSE, 'inst-crossowned01', 'skill-ownedalpha01', 'proj-catalog02', ARRAY['reviewer','task_role']::text[], 'project', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', TRUE, 'inst-betaskill01', 'skill-ownedbeta01', 'proj-catalog02', ARRAY['planner']::text[], 'project', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', TRUE, 'inst-betaplugin01', 'lib-uploadplugin01', 'proj-catalog02', ARRAY['worker']::text[], 'project', '2020-01-02T03:04:05Z');
INSERT INTO library_installs (created_at, enabled, id, item_id, project_id, roles, scope, updated_at) VALUES ('2020-01-01T00:00:00Z', TRUE, 'inst-localhttp01', 'mcp-http01', 'proj-catalog01', ARRAY['worker']::text[], 'project', '2020-01-02T03:04:05Z');
INSERT INTO system_settings (key, value) VALUES ('library.last_import', '{"imported_at": "2020-01-02T03:04:05Z", "added": ["plugin:Zebra@market"], "unchanged": 2, "skipped": [], "skipped_count": 0}');
INSERT INTO system_settings (key, value) VALUES ('library.last_mcp_import', '{"imported_at": "2020-01-02T03:04:05Z", "source": "fixture/mcp.json", "servers": 3, "added": ["mcp:mcp_http"], "updated": [], "removed": [], "unchanged": 2, "skipped": [], "skipped_count": 0}');
