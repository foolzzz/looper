package parity

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/configtransfer"
	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/httpapi"
)

// The Python generator owns schema/seed/migration. This test only reads that
// dedicated fixture, so it cannot race other parity suites replacing public.
func TestConfigExportHTTPParity(t *testing.T) {
	dsn := os.Getenv("LOOPGO_CONFIG_EXPORT_PARITY_URL")
	if dsn == "" {
		t.Skip("set LOOPGO_CONFIG_EXPORT_PARITY_URL to the generated disposable config export fixture")
	}
	if !acceptableDatabase(dsn) {
		t.Fatal("refusing non-disposable database")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	sqlDB, err := db.Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer sqlDB.Close()
	now := time.Date(2026, 6, 15, 12, 0, 0, 0, time.UTC)
	store := db.NewStore(sqlDB)
	s := httpapi.New("go-development", db.Probe{DB: sqlDB}, "")
	s.Now = func() time.Time { return now }
	s.Reads = store
	s.Port = 18081
	loginHome := t.TempDir()
	s.LoginEnviron = func() []string { return []string{"HOME=" + loginHome, "PATH=" + loginHome} }
	token := useOperatorToken(t, s)
	handler := s.Handler()
	root := filepath.Join("..", "..", "testdata", "parity", "config_export")
	casesRaw, err := os.ReadFile(filepath.Join(root, "cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name  string            `json:"name"`
		Query map[string]string `json:"query"`
	}
	if err = json.Unmarshal(casesRaw, &cases); err != nil {
		t.Fatal(err)
	}
	for _, tt := range cases {
		t.Run(tt.Name, func(t *testing.T) {
			query := url.Values{}
			for k, v := range tt.Query {
				query.Set(k, v)
			}
			req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:18081/api/config/export?"+query.Encode(), nil)
			req.RemoteAddr = "127.0.0.1:12345"
			req.Header.Set("Authorization", "Bearer "+token)
			rr := httptest.NewRecorder()
			handler.ServeHTTP(rr, req)
			raw, err := os.ReadFile(filepath.Join(root, tt.Name+".http"))
			if err != nil {
				t.Fatal(err)
			}
			expected, err := http.ReadResponse(bufio.NewReader(bytes.NewReader(raw)), req)
			if err != nil {
				t.Fatal(err)
			}
			defer expected.Body.Close()
			if rr.Code != expected.StatusCode {
				t.Fatalf("status %d body %s", rr.Code, rr.Body.String())
			}
			for key, values := range expected.Header {
				if rr.Header().Get(key) != strings.Join(values, ", ") {
					t.Errorf("header %s: %q != %q", key, rr.Header().Get(key), values)
				}
			}
			// Recorder retains net/http's internal identity instruction; the
			// wire test verifies that the server strips it and closes at EOF.
			if rr.Header().Get("Content-Length") != "" || rr.Header().Get("Transfer-Encoding") != "identity" {
				t.Fatal("streaming download gained framing header")
			}
			want, err := os.ReadFile(filepath.Join(root, tt.Name+".json"))
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(rr.Body.Bytes(), want) {
				t.Fatalf("download bytes differ: got %d want %d", rr.Body.Len(), len(want))
			}
			secret := []byte("SYNTHETIC_CONFIG_EXPORT_SECRET_DO_NOT_USE")
			if bytes.Contains(rr.Body.Bytes(), secret) != (tt.Query["secrets"] == "1") {
				t.Fatal("secret opt-in mismatch")
			}
			if bytes.Contains(rr.Body.Bytes(), []byte("SYNTHETIC_CHANNEL_STORAGE_ONLY_DO_NOT_EXPORT")) {
				t.Fatal("DB channel token exported")
			}
			opts := configtransfer.Options{IncludeProjects: tt.Query["projects"] == "1", IncludeSecrets: tt.Query["secrets"] == "1", Now: now}
			path := filepath.Join(t.TempDir(), "export.json")
			summary, err := store.WriteExport(ctx, path, opts)
			if err != nil {
				t.Fatal(err)
			}
			saved, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(saved, want) || summary.Bytes != int64(len(want)) {
				t.Fatal("file export differs from download")
			}
		})
	}
	// Ordinary dashboard reads must not gain the export's raw provider/MCP
	// values. Registry probes use an empty synthetic home.
	for _, path := range []string{"/api/catalog", "/api/registry"} {
		req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:18081"+path, nil)
		req.RemoteAddr = "127.0.0.1:12345"
		req.Header.Set("Authorization", "Bearer "+token)
		rr := httptest.NewRecorder()
		handler.ServeHTTP(rr, req)
		exposed := bytes.Contains(rr.Body.Bytes(), []byte("SYNTHETIC_CONFIG_EXPORT_SECRET_DO_NOT_USE"))
		if rr.Code != http.StatusOK || exposed {
			t.Fatalf("ordinary GET %s status %d exposed a secret: %t", path, rr.Code, exposed)
		}
	}
}
