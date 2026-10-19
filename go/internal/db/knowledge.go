package db

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"unicode"
	"unicode/utf16"
	"unicode/utf8"

	"golang.org/x/text/cases"

	"github.com/foolzzz/looper/go/internal/pyjson"
)

const documentMaxBytes = 2 * 1024 * 1024
const knowledgeIntro = "Reference documents for this project. They are not in this prompt: read the ones your work needs and say which ones you used. A document's path is relative to the directory named above it."
const documentColumns = "id, project_id, title, summary, source_kind, filename, path, size_bytes, sha256, created_at, updated_at"

var ErrDocumentNotFound = errors.New("document not found")
var ErrSkillNotFound = errors.New("skill not found")

func (s *Store) knowledgeProject(ctx context.Context, id string) (map[string]any, error) {
	rows, err := s.query(ctx, "knowledge project", "SELECT id, work_dir FROM projects WHERE id = $1", id)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, ErrProjectNotFound
	}
	return rows[0], nil
}

// ListDocuments reads metadata only, in the same stable order as Python.
func (s *Store) ListDocuments(ctx context.Context, projectID string) ([]map[string]any, error) {
	return s.query(ctx, "project documents", "SELECT "+documentColumns+" FROM project_documents WHERE project_id = $1 ORDER BY created_at, id", projectID)
}

func (s *Store) DocumentContent(ctx context.Context, id string) (any, error) {
	rows, err := s.query(ctx, "document content", "SELECT content FROM project_documents WHERE id = $1", id)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	return rows[0]["content"], nil
}

func (s *Store) ListDrafts(ctx context.Context, projectID string) ([]map[string]any, error) {
	return s.query(ctx, "document drafts", "SELECT id, project_id, task_id, title, summary, content, created_at FROM project_document_drafts WHERE project_id = $1 ORDER BY created_at, id", projectID)
}

type storedDocuments struct {
	directory string
	names     map[string]string
}

func isStored(row map[string]any) bool {
	return row["source_kind"] == "text" || row["source_kind"] == "upload"
}

// storedSet only computes read paths; a GET never materializes documents.
func storedSet(runtimeRoot, projectID string, rows []map[string]any) (storedDocuments, error) {
	out := storedDocuments{names: map[string]string{}}
	taken := map[string]bool{}
	listing := [][2]string{}
	fold := cases.Fold()
	for _, row := range rows {
		if !isStored(row) {
			continue
		}
		filename, name := asString(row["filename"]), asString(row["filename"])
		stem, extension := filename, ""
		if i := strings.LastIndex(filename, "."); i >= 0 {
			stem, extension = filename[:i], filename[i:]
		}
		for n := 2; taken[fold.String(name)]; n++ {
			name = fmt.Sprintf("%s-%d%s", stem, n, extension)
		}
		taken[fold.String(name)] = true
		out.names[asString(row["id"])] = name
		listing = append(listing, [2]string{name, asString(row["sha256"])})
	}
	if len(listing) == 0 {
		return out, nil
	}
	root, err := filepath.Abs(runtimeRoot)
	if err != nil {
		return out, fmt.Errorf("knowledge runtime root: %w", err)
	}
	key := sha256.Sum256([]byte(asciiJSON([]any{"knowledge-set/v1", listing})))
	out.directory = filepath.Join(root, ".local", "projects", projectID, "knowledge", fmt.Sprintf("%x", key))
	return out, nil
}

// asciiJSON is json.dumps(ensure_ascii=True, separators=(",", ":")).
func asciiJSON(value any) string {
	var encoded bytes.Buffer
	enc := json.NewEncoder(&encoded)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(value) // callers pass only strings and arrays of strings
	var out strings.Builder
	for _, r := range strings.TrimSuffix(encoded.String(), "\n") {
		if r < 127 {
			out.WriteRune(r)
		} else if r <= 0xffff {
			fmt.Fprintf(&out, `\u%04x`, r)
		} else {
			hi, lo := utf16.EncodeRune(r)
			fmt.Fprintf(&out, `\u%04x\u%04x`, hi, lo)
		}
	}
	return out.String()
}

func knowledgePromptName(text string) string {
	runes := []rune(text)
	plain := len(runes) <= 4096 && !strings.HasPrefix(text, "-") && !strings.ContainsAny(text, "\"`")
	for _, r := range runes {
		if r != ' ' && (unicode.IsControl(r) || unicode.Is(unicode.Z, r) || unicode.Is(unicode.Cf, r) || unicode.Is(unicode.Co, r) || unicode.Is(unicode.Cs, r) || !unicode.IsGraphic(r)) {
			plain = false
		}
	}
	if plain {
		return text
	}
	if len(runes) > 4096 {
		text = string(runes[:4095]) + "…"
	}
	return asciiJSON(text)
}

// Resolve the current regular file inside the project. The final stat and open
// stay confined to os.Root even if a link changes after resolution.
func projectDocument(projectDir, path string) (*os.Root, string, os.FileInfo) {
	if projectDir == "" {
		return nil, "", nil
	}
	rootPath, err := filepath.EvalSymlinks(projectDir)
	if err != nil {
		return nil, "", nil
	}
	rootPath, err = filepath.Abs(rootPath)
	if err != nil {
		return nil, "", nil
	}
	target := path
	if !filepath.IsAbs(target) {
		target = knowledgeReadPath(projectDir, target)
	}
	target, err = filepath.EvalSymlinks(target)
	if err != nil {
		return nil, "", nil
	}
	target, err = filepath.Abs(target)
	if err != nil {
		return nil, "", nil
	}
	relative, err := filepath.Rel(rootPath, target)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return nil, "", nil
	}
	root, err := os.OpenRoot(rootPath)
	if err != nil {
		return nil, "", nil
	}
	info, err := root.Stat(relative)
	if err != nil || !info.Mode().IsRegular() {
		root.Close()
		return nil, "", nil
	}
	return root, relative, info
}

func openProjectDocument(projectDir, path string) *os.File {
	file, _ := openProjectDocumentResult(projectDir, path)
	return file
}

func openProjectDocumentResult(projectDir, path string) (*os.File, error) {
	root, relative, _ := projectDocument(projectDir, path)
	if root == nil {
		return nil, nil
	}
	defer root.Close()
	// Nonblocking prevents a replaced FIFO from hanging a request between stat
	// and open; fstat still refuses every non-regular descriptor.
	file, err := root.OpenFile(relative, os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, fmt.Errorf("%s", knowledgeOSError(err, knowledgeReadPath(root.Name(), relative)))
	}
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		file.Close()
		return nil, nil
	}
	return file, nil
}

// pathlib drops empty and dot components but preserves parent components in
// displayed read paths, even when the file is refused by the directory guard.
func knowledgeReadPath(root, path string) string {
	joined := root + "/" + path
	if root == "" || filepath.IsAbs(path) {
		joined = path
	}
	parts := []string{}
	for _, part := range strings.Split(joined, "/") {
		if part != "" && part != "." {
			parts = append(parts, part)
		}
	}
	out := strings.Join(parts, "/")
	if strings.HasPrefix(joined, "/") {
		return "/" + out
	}
	if out == "" {
		return "."
	}
	return out
}

func documentView(stored storedDocuments, project, row map[string]any) map[string]any {
	kind := asString(row["source_kind"])
	readPath := filepath.Join(stored.directory, stored.names[asString(row["id"])])
	missing, size := false, asInt(row["size_bytes"])
	if kind == "path" {
		readPath = knowledgeReadPath(asString(project["work_dir"]), asString(row["path"]))
		root, _, info := projectDocument(asString(project["work_dir"]), asString(row["path"]))
		missing = root == nil
		if root != nil {
			size = int(info.Size())
			root.Close()
		}
	}
	var filename any
	if row["filename"] != "" {
		filename = row["filename"]
	}
	return map[string]any{
		"document_id": row["id"], "title": row["title"], "summary": row["summary"], "source_kind": kind,
		"filename": filename, "path": row["path"], "read_path": readPath, "missing": missing,
		"size_bytes": size, "sha256": row["sha256"], "created_at": row["created_at"], "updated_at": row["updated_at"],
	}
}

func (s *Store) Knowledge(ctx context.Context, projectID, runtimeRoot string) (map[string]any, error) {
	project, err := s.knowledgeProject(ctx, projectID)
	if err != nil {
		return nil, err
	}
	rows, err := s.ListDocuments(ctx, projectID)
	if err != nil {
		return nil, err
	}
	return knowledgeView(ctx, runtimeRoot, project, rows)
}

func knowledgeView(ctx context.Context, runtimeRoot string, project map[string]any, rows []map[string]any) (map[string]any, error) {
	projectID := asString(project["id"])
	stored, err := storedSet(runtimeRoot, projectID, rows)
	if err != nil {
		return nil, err
	}
	documents := []map[string]any{}
	groups, roots := map[string][]string{}, []string{}
	storedBytes := 0
	for _, row := range rows {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		view := documentView(stored, project, row)
		documents = append(documents, view)
		if isStored(row) {
			storedBytes += asInt(row["size_bytes"])
		}
	}
	// The index puts stored documents first while the API keeps database order.
	for _, storedKind := range []bool{true, false} {
		for i, row := range rows {
			if isStored(row) != storedKind {
				continue
			}
			root, path := stored.directory, stored.names[asString(row["id"])]
			if !storedKind {
				root, path = asString(project["work_dir"]), asString(row["path"])
			}
			if _, ok := groups[root]; !ok {
				roots = append(roots, root)
			}
			prefix := ""
			if documents[i]["missing"] == true {
				prefix = "missing: "
			}
			groups[root] = append(groups[root], "- "+asString(row["title"])+": "+asString(row["summary"])+" ("+prefix+knowledgePromptName(path)+")")
		}
	}
	index := ""
	if len(rows) > 0 {
		lines := []string{"## Project knowledge", knowledgeIntro}
		for _, root := range roots {
			label := knowledgePromptName(root)
			if root == "" {
				label = "the project directory, which is not set"
			}
			lines = append(lines, "In "+label+":")
			lines = append(lines, groups[root]...)
		}
		index = strings.Join(lines, "\n")
	}
	return map[string]any{"project_id": projectID, "documents": documents, "count": len(rows), "stored_bytes": storedBytes,
		"index": index, "index_chars": utf8.RuneCountInString(index), "caps": map[string]any{"document_max_bytes": documentMaxBytes, "project_max_documents": 100, "project_max_bytes": 50 * 1024 * 1024, "index_max_chars": 4000}}, nil
}

func (s *Store) Document(ctx context.Context, projectID, documentID, runtimeRoot string) (map[string]any, error) {
	project, err := s.knowledgeProject(ctx, projectID)
	if err != nil {
		return nil, err
	}
	rows, err := s.ListDocuments(ctx, projectID)
	if err != nil {
		return nil, err
	}
	var row map[string]any
	for _, candidate := range rows {
		if candidate["id"] == documentID {
			row = candidate
			break
		}
	}
	if row == nil {
		return nil, ErrDocumentNotFound
	}
	stored, err := storedSet(runtimeRoot, projectID, rows)
	if err != nil {
		return nil, err
	}
	var content any
	if isStored(row) {
		content, err = s.DocumentContent(ctx, documentID)
		if err != nil {
			return nil, err
		}
	}
	return documentResponse(ctx, stored, project, row, content)
}

func documentResponse(ctx context.Context, stored storedDocuments, project, row map[string]any, content any) (map[string]any, error) {
	body := map[string]any{"document": documentView(stored, project, row), "content": nil}
	if isStored(row) {
		body["content"] = content
		return body, nil
	}
	path := asString(row["path"])
	file, err := openProjectDocumentResult(asString(project["work_dir"]), path)
	if err != nil {
		body["content_error"] = err.Error()
		return body, nil
	}
	if file == nil {
		body["content_error"] = path + " is missing from the project's default directory"
		return body, nil
	}
	defer file.Close()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	data, err := io.ReadAll(io.LimitReader(file, documentMaxBytes+1))
	if err != nil {
		body["content_error"] = knowledgeOSError(err, file.Name())
		return body, nil
	}
	if len(data) > documentMaxBytes {
		info, err := file.Stat()
		if err != nil {
			body["content_error"] = err.Error()
		} else {
			body["content_error"] = fmt.Sprintf("a document has at most 2 MiB (2097152 bytes); %s has %d bytes", path, info.Size())
		}
	} else if !utf8.Valid(data) || bytes.ContainsRune(data, 0) {
		body["content_error"] = path + " is not a text file (it is not UTF-8 text or holds NUL bytes); knowledge takes text such as Markdown, plain text, source code, CSV or JSON"
	} else {
		body["content"] = string(data)
	}
	return body, nil
}

// Python exposes the OSError text for a present file that cannot be read.
func knowledgeOSError(err error, path string) string {
	var errno syscall.Errno
	if errors.As(err, &errno) {
		message := errno.Error()
		if len(message) > 0 {
			message = strings.ToUpper(message[:1]) + message[1:]
		}
		return fmt.Sprintf("[Errno %d] %s: %s", errno, message, pyRepr(path))
	}
	return err.Error()
}

func (s *Store) Drafts(ctx context.Context, projectID string) (map[string]any, error) {
	if _, err := s.knowledgeProject(ctx, projectID); err != nil {
		return nil, err
	}
	rows, err := s.ListDrafts(ctx, projectID)
	if err != nil {
		return nil, err
	}
	drafts := []map[string]any{}
	for _, row := range rows {
		drafts = append(drafts, draftView(row))
	}
	return map[string]any{"project_id": projectID, "drafts": drafts, "count": len(drafts)}, nil
}

func draftView(row map[string]any) map[string]any {
	return map[string]any{"draft_id": row["id"], "task_id": row["task_id"], "title": row["title"], "summary": row["summary"], "content": row["content"], "size_bytes": len(asString(row["content"])), "created_at": row["created_at"]}
}

func (s *Store) ProjectSkill(ctx context.Context, projectID, skillID string) (map[string]any, error) {
	rows, err := s.query(ctx, "project skill", "SELECT id, name, description, size_bytes, imported_at, updated_at FROM library_items WHERE id = $1 AND origin = 'project' AND owner_project_id = $2", skillID, projectID)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, ErrSkillNotFound
	}
	row := rows[0]
	files, err := s.query(ctx, "project skill content", "SELECT content FROM library_files WHERE item_id = $1 AND path = 'SKILL.md'", skillID)
	if err != nil {
		return nil, err
	}
	text := ""
	if len(files) > 0 {
		text = asString(files[0]["content"])
	}
	return skillView(row, text), nil
}

func skillView(row map[string]any, text string) map[string]any {
	// Python's frontmatter uses ensure_ascii=False with the normal JSON string spelling.
	description := jsonString(asString(row["description"]))
	head := "---\nname: " + asString(row["name"]) + "\ndescription: " + description + "\n---\n\n"
	text = strings.TrimPrefix(text, head)
	return map[string]any{"skill": map[string]any{"skill_id": row["id"], "name": row["name"], "qualified_name": "project:" + asString(row["name"]), "description": row["description"], "size_bytes": row["size_bytes"], "created_at": row["imported_at"], "updated_at": row["updated_at"]}, "content": text}
}

func jsonString(text string) string {
	encoded, _ := pyjson.Marshal(text) // a string cannot fail encoding
	return string(encoded)
}
