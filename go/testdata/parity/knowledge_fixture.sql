-- Loaded only after legacy cases, so new projects never change earlier goldens.
-- __KNOWLEDGE_ROOT__ is a fresh temporary root owned by the test harness.
INSERT INTO projects (id, name, work_dir, created_at, updated_at) VALUES
('proj-knowledge01', 'Knowledge fixture', '__KNOWLEDGE_ROOT__/project', '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z'),
('proj-knowledge02', 'Empty knowledge fixture', '__KNOWLEDGE_ROOT__/empty', '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z');

INSERT INTO project_documents
(id, project_id, title, summary, source_kind, filename, content, path, size_bytes, sha256, created_at, updated_at) VALUES
('doc-text001', 'proj-knowledge01', 'Text 文档', 'Stored text', 'text', 'Shared.md', E'# Text\n你好 🌍\n', NULL, 19, 'text-hash', '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z'),
('doc-upload01', 'proj-knowledge01', 'Upload', 'Uploaded text', 'upload', 'shared.md', E'alpha,beta\n1,2\n', NULL, 15, 'upload-hash', '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z'),
('doc-unicode01', 'proj-knowledge01', 'Unicode filename', '中文 reference', 'text', '说明.md', E'UTF-8 正文\n', NULL, 13, 'unicode-hash', '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z'),
('doc-live001', 'proj-knowledge01', 'Live file', 'Read updated bytes', 'path', '', NULL, 'live.md', 1, 'stale-live-hash', '2020-01-03T00:00:00Z', '2020-01-04T00:00:00Z'),
('doc-missing01', 'proj-knowledge01', 'Missing file', 'File was removed', 'path', '', NULL, 'gone.md', 73, 'missing-hash', '2020-01-03T00:00:00Z', '2020-01-04T00:00:00Z'),
('doc-binary01', 'proj-knowledge01', 'Binary file', 'File became binary', 'path', '', NULL, 'binary.dat', 1, 'binary-hash', '2020-01-03T00:00:00Z', '2020-01-04T00:00:00Z'),
('doc-invalid01', 'proj-knowledge01', 'Invalid UTF-8', 'File became invalid UTF-8', 'path', '', NULL, 'invalid.dat', 1, 'invalid-hash', '2020-01-03T00:00:00Z', '2020-01-04T00:00:00Z'),
('doc-large001', 'proj-knowledge01', 'Large file', 'File grew beyond cap', 'path', '', NULL, 'large.txt', 1, 'large-hash', '2020-01-03T00:00:00Z', '2020-01-04T00:00:00Z'),
('doc-symlink01', 'proj-knowledge01', 'Escaped symlink', 'Target outside project', 'path', '', NULL, 'escaped.md', 11, 'symlink-hash', '2020-01-03T00:00:00Z', '2020-01-04T00:00:00Z'),
('doc-parent01', 'proj-knowledge01', 'Parent traversal', 'Target outside project', 'path', '', NULL, '../outside.md', 11, 'parent-hash', '2020-01-03T00:00:00Z', '2020-01-04T00:00:00Z'),
('doc-dir0001', 'proj-knowledge01', 'Directory', 'Target is a directory', 'path', '', NULL, 'subdir', 4, 'directory-hash', '2020-01-03T00:00:00Z', '2020-01-04T00:00:00Z');

INSERT INTO project_document_drafts (id, project_id, task_id, title, summary, content, created_at) VALUES
('draft-z001', 'proj-knowledge01', 'task-rich00001', 'From a task', 'Task lessons', E'First lesson\n', '2020-01-01T00:00:00Z'),
('draft-a001', 'proj-knowledge01', NULL, '中文草稿', 'Unicode byte size', E'经验 🌍\n', '2020-01-01T00:00:00Z');

INSERT INTO library_items
(id, kind, name, description, sha256, size_bytes, file_count, origin, owner_project_id, imported_at, updated_at) VALUES
('skill-project001', 'skill', 'project-guide', 'Read: "中文" # guide', 'skill-hash', 95, 1, 'project', 'proj-knowledge01', '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z'),
('skill-nofile001', 'skill', 'missing-file', 'File removed', 'missing-hash', 10, 1, 'project', 'proj-knowledge01', '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z'),
('skill-rawbody01', 'skill', 'raw-body', 'Not matching frontmatter', 'raw-hash', 55, 1, 'project', 'proj-knowledge01', '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z');
INSERT INTO library_items
(id, kind, name, description, sha256, size_bytes, file_count, origin, imported_at, updated_at) VALUES
('skill-otherorigin01', 'skill', 'other-origin', 'Machine imported skill', 'machine-hash', 10, 1, 'machine', '2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z');
INSERT INTO library_files (item_id, path, content, sha256) VALUES
('skill-project001', 'SKILL.md', E'---\nname: project-guide\ndescription: "Read: \\"中文\\" # guide"\n---\n\n# Guide\n使用步骤 🌍\n', 'skill-file-hash'),
('skill-rawbody01', 'SKILL.md', E'---\nname: a-different-name\n---\n\nKeep this frontmatter.\n', 'raw-file-hash');
