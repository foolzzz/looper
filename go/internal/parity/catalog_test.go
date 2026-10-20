package parity

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/httpapi"
)

var catalogCases = []struct {
	name, path string
	status     int
}{
	{"catalog", "/api/catalog", 200},
	{"catalog-lib-machineplugin01", "/api/catalog/lib-machineplugin01", 200},
	{"catalog-lib-uploadplugin01", "/api/catalog/lib-uploadplugin01", 200},
	{"catalog-lib-gitplugin01", "/api/catalog/lib-gitplugin01", 200},
	{"catalog-lib-machineskill01", "/api/catalog/lib-machineskill01", 200},
	{"catalog-lib-uploadskill01", "/api/catalog/lib-uploadskill01", 200},
	{"catalog-lib-gitskill01", "/api/catalog/lib-gitskill01", 200},
	{"catalog-lib-uiskill01", "/api/catalog/lib-uiskill01", 200},
	{"catalog-skill-ownedalpha01", "/api/catalog/skill-ownedalpha01", 200},
	{"catalog-skill-ownedbeta01", "/api/catalog/skill-ownedbeta01", 200},
	{"catalog-skill-missingfile01", "/api/catalog/skill-missingfile01", 200},
	{"catalog-mcp-machine01", "/api/catalog/mcp-machine01", 200},
	{"catalog-mcp-http01", "/api/catalog/mcp-http01", 200},
	{"catalog-mcp-sse01", "/api/catalog/mcp-sse01", 200},
	{"catalog-missing", "/api/catalog/lib-absent01", 404},
	{"catalog-installs01", "/api/projects/proj-catalog01/installs", 200},
	{"catalog-installs02", "/api/projects/proj-catalog02/installs", 200},
	{"catalog-installs03", "/api/projects/proj-catalog03/installs", 200},
	{"catalog-installs-missing", "/api/projects/proj-absent01/installs", 404},
	{"catalog-invalid-reports", "/api/catalog", 200},
	{"catalog-scalar-reports", "/api/catalog", 200},
	{"catalog-no-reports", "/api/catalog", 200},
	{"catalog-empty", "/api/catalog", 200},
}

// This suite owns the public schema of the explicitly supplied disposable DB.
// It is sequential because the other parity suites replace that schema too.
func TestCatalogGoldenParity(t *testing.T) {
	dsn := os.Getenv("LOOPGO_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("LOOPGO_TEST_DATABASE_URL is not set")
	}
	if !acceptableDatabase(dsn) {
		t.Fatal("refusing database URL")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	if err := db.ExecScript(ctx, dsn, "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"); err != nil {
		t.Fatal(err)
	}
	if err := db.EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join("..", "..", "testdata", "parity")
	fixture, err := os.ReadFile(filepath.Join(dir, "catalog_fixture.sql"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.ExecScript(ctx, dsn, string(fixture)); err != nil {
		t.Fatalf("seed catalog: %v", err)
	}
	sqlDB, err := db.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	api := httpapi.New("parity-test", db.Probe{DB: sqlDB}, "")
	api.Reads = db.NewStore(sqlDB)
	api.Port = 18091
	token := useOperatorToken(t, api)
	handler := api.Handler()

	check := func(name, path string, status int) {
		t.Helper()
		t.Run(name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:18091"+path, nil).WithContext(ctx)
			req.Header.Set("Authorization", "Bearer "+token)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, req)
			if response.Code != status {
				t.Fatalf("status %d body %s, want %d", response.Code, response.Body.Bytes(), status)
			}
			body := response.Body.Bytes()
			for _, secret := range []string{"MCP_ENV_SECRET_SENTINEL", "MCP_HEADER_SECRET_SENTINEL", "MCP_AUTH_SECRET_SENTINEL",
				"PLUGIN_FILE_CONTENT_SENTINEL", "UPLOADED_PLUGIN_CONTENT_SENTINEL", "REFERENCE_FILE_CONTENT_SENTINEL"} {
				if bytes.Contains(body, []byte(secret)) {
					t.Fatalf("response leaked secret or listing-only file content %q", secret)
				}
			}
			var value map[string]any
			if err := json.Unmarshal(body, &value); err != nil {
				t.Fatal(err)
			}
			if files, ok := value["files"].([]any); ok {
				for _, file := range files {
					listing, ok := file.(map[string]any)
					if !ok || len(listing) != 3 || listing["path"] == nil || listing["size_bytes"] == nil || listing["executable"] == nil {
						t.Fatalf("file listing must contain only path, size_bytes and executable: %#v", file)
					}
				}
			}
			golden, err := os.ReadFile(filepath.Join(dir, name+".json"))
			if err != nil {
				t.Fatal(err)
			}
			// Catalog versions and timestamps are seeded business data. Preserve
			// them, unlike the legacy harness's volatile build-version rewrite.
			got, want := catalogJSON(t, body), catalogJSON(t, golden)
			if !bytes.Equal(got, want) {
				t.Fatalf("catalog JSON differs\n got %s\nwant %s", got, want)
			}
			compareCaseRaw(t, name, response.Result(), body, false)
		})
	}

	for _, tc := range catalogCases[:19] {
		check(tc.name, tc.path, tc.status)
	}

	for _, phase := range []struct {
		name, sql string
	}{
		{"catalog-invalid-reports", `UPDATE system_settings SET value = CASE key WHEN 'library.last_import' THEN '{bad' ELSE '[' END WHERE key IN ('library.last_import','library.last_mcp_import')`},
		{"catalog-scalar-reports", `UPDATE system_settings SET value = CASE key WHEN 'library.last_import' THEN '7' ELSE '[true, null]' END WHERE key IN ('library.last_import','library.last_mcp_import')`},
		{"catalog-no-reports", `DELETE FROM system_settings WHERE key IN ('library.last_import','library.last_mcp_import')`},
		{"catalog-empty", `DELETE FROM library_items`},
	} {
		if _, err := sqlDB.ExecContext(ctx, phase.sql); err != nil {
			t.Fatalf("prepare %s: %v", phase.name, err)
		}
		check(phase.name, "/api/catalog", 200)
	}
}

func catalogJSON(t *testing.T, body []byte) []byte {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		t.Fatal(err)
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		t.Fatalf("extra catalog JSON: %v", err)
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return encoded
}
