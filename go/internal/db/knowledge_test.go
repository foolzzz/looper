package db

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"syscall"
	"testing"
	"time"
	"unicode/utf8"
)

func knowledgeFixture(t *testing.T) (string, map[string][]map[string]any) {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"project", "project/subdir", "empty"} {
		if err := os.MkdirAll(filepath.Join(root, name), 0700); err != nil {
			t.Fatal(err)
		}
	}
	files := map[string][]byte{
		"project/live.md":     []byte("# Live\n最新内容 🌍\n"),
		"project/binary.dat":  []byte("text\x00binary"),
		"project/invalid.dat": []byte{0xff, 'i', 'n', 'v', 'a', 'l', 'i', 'd'},
		"project/large.txt":   bytes.Repeat([]byte("x"), 2*1024*1024+1),
		"outside.md":          []byte("OUTSIDE_SENTINEL"),
	}
	for name, data := range files {
		if err := os.WriteFile(filepath.Join(root, name), data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(root, "outside.md"), filepath.Join(root, "project", "escaped.md")); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", "knowledge_fixture_input.json"))
	if err != nil {
		t.Fatal(err)
	}
	data = bytes.ReplaceAll(data, []byte("<knowledge-root>"), []byte(root))
	var fixture map[string][]map[string]any
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	return root, fixture
}

func knowledgeRows(rows []map[string]any, projectID string) []map[string]any {
	out := []map[string]any{}
	for _, row := range rows {
		if row["project_id"] == projectID {
			out = append(out, row)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := asString(out[i]["created_at"]), asString(out[j]["created_at"])
		if a == b {
			return asString(out[i]["id"]) < asString(out[j]["id"])
		}
		return a < b
	})
	return out
}

func assertKnowledgeGolden(t *testing.T, root, name string, got map[string]any) {
	t.Helper()
	if index, ok := got["index"].(string); ok && got["index_chars"] != utf8.RuneCountInString(index) {
		t.Fatalf("index_chars %v does not count real index", got["index_chars"])
	}
	data, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(data, []byte("OUTSIDE_SENTINEL")) {
		t.Fatal("response leaked a file outside the project")
	}
	var normalized map[string]any
	if err := json.Unmarshal(bytes.ReplaceAll(data, []byte(root), []byte("<knowledge-root>")), &normalized); err != nil {
		t.Fatal(err)
	}
	if index, ok := normalized["index"].(string); ok {
		normalized["index_chars"] = float64(utf8.RuneCountInString(index))
	}
	golden, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", name+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var want map[string]any
	if err := json.Unmarshal(golden, &want); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(normalized, want) {
		actual, _ := json.Marshal(normalized)
		t.Fatalf("Python golden differs\n got %s\nwant %s", actual, golden)
	}
}

func TestKnowledgeViewsPythonGoldens(t *testing.T) {
	root, fixture := knowledgeFixture(t)
	for i, project := range fixture["projects"] {
		name := "knowledge"
		if i == 1 {
			name = "knowledge-empty"
		}
		t.Run(name, func(t *testing.T) {
			rows := knowledgeRows(fixture["project_documents"], asString(project["id"]))
			got, err := knowledgeView(context.Background(), root, project, rows)
			if err != nil {
				t.Fatal(err)
			}
			assertKnowledgeGolden(t, root, name, got)
		})
	}
	// GET computes read paths, but must not create a materialized stored set.
	if _, err := os.Stat(filepath.Join(root, ".local")); !os.IsNotExist(err) {
		t.Fatalf("read materialized stored files: %v", err)
	}
}

func TestDocumentResponsesPythonGoldens(t *testing.T) {
	root, fixture := knowledgeFixture(t)
	project := fixture["projects"][0]
	rows := knowledgeRows(fixture["project_documents"], asString(project["id"]))
	stored, err := storedSet(root, asString(project["id"]), rows)
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct{ id, golden string }{
		{"doc-text001", "document-text"},
		{"doc-upload01", "document-upload"},
		{"doc-unicode01", "document-unicode"},
		{"doc-live001", "document-live"},
		{"doc-missing01", "document-file-missing"},
		{"doc-binary01", "document-binary"},
		{"doc-invalid01", "document-invalid-utf8"},
		{"doc-large001", "document-large"},
		{"doc-symlink01", "document-symlink"},
		{"doc-parent01", "document-parent"},
		{"doc-dir0001", "document-directory"},
	}
	for _, tc := range cases {
		t.Run(tc.golden, func(t *testing.T) {
			var row map[string]any
			for _, candidate := range rows {
				if candidate["id"] == tc.id {
					row = candidate
					break
				}
			}
			if row == nil {
				t.Fatalf("fixture lacks document %s", tc.id)
			}
			got, err := documentResponse(context.Background(), stored, project, row, row["content"])
			if err != nil {
				t.Fatal(err)
			}
			assertKnowledgeGolden(t, root, tc.golden, got)
		})
	}
}

func TestDraftAndSkillViewsPythonGoldens(t *testing.T) {
	root, fixture := knowledgeFixture(t)
	for i, project := range fixture["projects"] {
		name := "knowledge-drafts"
		if i == 1 {
			name = "knowledge-drafts-empty"
		}
		t.Run(name, func(t *testing.T) {
			rows := knowledgeRows(fixture["project_document_drafts"], asString(project["id"]))
			drafts := []map[string]any{}
			for _, row := range rows {
				drafts = append(drafts, draftView(row))
			}
			assertKnowledgeGolden(t, root, name, map[string]any{"project_id": project["id"], "drafts": drafts, "count": len(drafts)})
		})
	}
	cases := []struct{ id, golden string }{
		{"skill-project001", "project-skill"},
		{"skill-nofile001", "project-skill-file-missing"},
		{"skill-rawbody01", "project-skill-raw-body"},
	}
	for _, tc := range cases {
		t.Run(tc.golden, func(t *testing.T) {
			var row map[string]any
			for _, candidate := range fixture["library_items"] {
				if candidate["id"] == tc.id {
					row = candidate
					break
				}
			}
			if row == nil {
				t.Fatalf("fixture lacks skill %s", tc.id)
			}
			text := ""
			for _, file := range fixture["library_files"] {
				if file["item_id"] == tc.id && file["path"] == "SKILL.md" {
					text = asString(file["content"])
				}
			}
			assertKnowledgeGolden(t, root, tc.golden, skillView(row, text))
		})
	}
}

func TestOpenProjectDocumentBoundaries(t *testing.T) {
	root := t.TempDir()
	project := filepath.Join(root, "project")
	for _, name := range []string{project, filepath.Join(root, "outside-dir")} {
		if err := os.Mkdir(name, 0700); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{filepath.Join(project, "inside.md"), filepath.Join(root, "outside.md"), filepath.Join(root, "outside-dir", "outside.md")} {
		if err := os.WriteFile(name, []byte("fixture content"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	for name, target := range map[string]string{
		"absolute-inside.md":  filepath.Join(project, "inside.md"),
		"relative-inside.md":  "inside.md",
		"relative-outside.md": "../outside.md",
		"outside-dir":         "../outside-dir",
		"missing-dir":         "../missing-dir",
	} {
		if err := os.Symlink(target, filepath.Join(project, name)); err != nil {
			t.Fatal(err)
		}
	}
	cases := []struct {
		name, directory, path string
		open                  bool
	}{
		{"relative regular", project, "inside.md", true},
		{"absolute regular", project, filepath.Join(project, "inside.md"), true},
		{"absolute internal symlink", project, "absolute-inside.md", true},
		{"relative internal symlink", project, "relative-inside.md", true},
		{"relative external symlink", project, "relative-outside.md", false},
		{"external directory symlink", project, "outside-dir/outside.md", false},
		{"missing external directory symlink", project, "missing-dir/outside.md", false},
		{"directory", project, ".", false},
		{"parent traversal", project, "../outside.md", false},
		{"absolute external file", project, filepath.Join(root, "outside.md"), false},
		{"missing file", project, "missing.md", false},
		{"unset directory", "", filepath.Join(project, "inside.md"), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			file := openProjectDocument(tc.directory, tc.path)
			if file != nil {
				defer file.Close()
			}
			if (file != nil) != tc.open {
				t.Fatalf("opened = %v, want %v", file != nil, tc.open)
			}
		})
	}
}

func TestOpenProjectDocumentRejectsFIFOWithoutBlocking(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "pipe")
	if err := syscall.Mkfifo(path, 0600); err != nil {
		t.Fatal(err)
	}
	done := make(chan *os.File, 1)
	go func() { done <- openProjectDocument(root, "pipe") }()
	select {
	case file := <-done:
		if file != nil {
			file.Close()
			t.Fatal("opened a FIFO as a document")
		}
	case <-time.After(time.Second):
		// Unblock a buggy read-only open so the failing regression leaves no goroutine behind.
		fd, err := syscall.Open(path, syscall.O_RDWR|syscall.O_NONBLOCK, 0600)
		if err != nil {
			t.Fatalf("open blocked on FIFO; unblock: %v", err)
		}
		defer syscall.Close(fd)
		select {
		case file := <-done:
			if file != nil {
				file.Close()
			}
		case <-time.After(time.Second):
			t.Fatal("open stayed blocked after FIFO writer connected")
		}
		t.Fatal("open blocked on FIFO")
	}
}

func TestKnowledgeViewUsesLiveSize(t *testing.T) {
	root, fixture := knowledgeFixture(t)
	project := fixture["projects"][0]
	rows := knowledgeRows(fixture["project_documents"], asString(project["id"]))
	const content = "Live content changed again 中文\n"
	if err := os.WriteFile(filepath.Join(root, "project", "live.md"), []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	got, err := knowledgeView(context.Background(), root, project, rows)
	if err != nil {
		t.Fatal(err)
	}
	for _, document := range got["documents"].([]map[string]any) {
		if document["document_id"] == "doc-live001" {
			if document["size_bytes"] != len(content) || document["missing"] != false {
				t.Fatalf("live document %#v", document)
			}
			return
		}
	}
	t.Fatal("live document missing from response")
}

func TestSkillViewStripsExactFrontmatter(t *testing.T) {
	cases := []struct{ name, description, quoted string }{
		{"unicode separators", "line\u2028paragraph\u2029end", "\"line\u2028paragraph\u2029end\""},
		{"literal unicode escapes", `literal \u2028 and \u2029`, `"literal \\u2028 and \\u2029"`},
		{"backticks and quotes", "Read `code`: \"中文\" # guide", "\"Read `code`: \\\"中文\\\" # guide\""},
		{"control characters", "a\nb\tc", `"a\nb\tc"`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			row := map[string]any{"id": "skill-one", "name": "guide", "description": tc.description}
			head := "---\nname: guide\ndescription: " + tc.quoted + "\n---\n\n"
			got := skillView(row, head+"Body\n")
			if got["content"] != "Body\n" {
				t.Fatalf("frontmatter was not stripped: %q", got["content"])
			}
			if got["skill"].(map[string]any)["description"] != tc.description {
				t.Fatalf("description changed: %#v", got["skill"])
			}
		})
	}
}

func TestKnowledgePromptNameEscapesDEL(t *testing.T) {
	if got := knowledgePromptName("x\x7f"); got != `"x\u007f"` {
		t.Fatalf("prompt name %q", got)
	}
}

func TestProjectDocumentResolvesParentAfterSymlink(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "inner", "deep"), 0700); err != nil {
		t.Fatal(err)
	}
	for name, text := range map[string]string{"live.md": "root file", "inner/live.md": "resolved file"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(text), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("inner/deep", filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	file := openProjectDocument(root, "link/../live.md")
	if file == nil {
		t.Fatal("expected internal resolved file")
	}
	defer file.Close()
	body, err := io.ReadAll(file)
	if err != nil || string(body) != "resolved file" {
		t.Fatalf("body %q error %v", body, err)
	}
}

func TestDocumentPermissionErrorKeepsMetadata(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can read mode 000 files")
	}
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "private.md")
	if err := os.WriteFile(path, []byte("text"), 0000); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(path, 0600)
	row := map[string]any{"id": "doc-private", "source_kind": "path", "path": "private.md", "filename": "", "size_bytes": 1}
	body, err := documentResponse(context.Background(), storedDocuments{}, map[string]any{"work_dir": root}, row, nil)
	if err != nil {
		t.Fatal(err)
	}
	metadata := body["document"].(map[string]any)
	want := "[Errno 13] Permission denied: " + pyRepr(path)
	if metadata["missing"] != false || metadata["size_bytes"] != 4 || body["content"] != nil || body["content_error"] != want {
		t.Fatalf("permission response %#v, want content_error %q", body, want)
	}
}
