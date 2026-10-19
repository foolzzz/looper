package db

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/configtransfer"
)

const exportFixtures = "../../testdata/parity/config_export"

func exportFixtureRead(t *testing.T) exportRead {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(exportFixtures, "rows.json"))
	if err != nil {
		t.Fatal(err)
	}
	var data any
	if err := unmarshalPythonJSON(raw, &data); err != nil {
		t.Fatal(err)
	}
	sections := data.(map[string]any)
	return func(ctx context.Context, section, id string) ([]map[string]any, error) {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		raw, ok := sections[section]
		if !ok {
			t.Fatalf("fixture lacks read %s", section)
		}
		if section == "files" {
			raw = raw.(map[string]any)[id]
		}
		out := []map[string]any{}
		for _, row := range asAnySlice(raw) {
			out = append(out, row.(map[string]any))
		}
		return out, nil
	}
}
func TestPythonVersionIsReference(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "looper", "__init__.py"))
	if err != nil {
		t.Fatal(err)
	}
	const marker = `__version__ = "`
	text := string(raw)
	i := strings.Index(text, marker)
	if i < 0 {
		t.Fatal("looper/__init__.py has no __version__ assignment")
	}
	rest := text[i+len(marker):]
	end := strings.IndexByte(rest, '"')
	if end < 0 || rest[:end] != PythonVersion {
		got := ""
		if end >= 0 {
			got = rest[:end]
		}
		t.Fatalf("PythonVersion %q != looper.__version__ %q", PythonVersion, got)
	}
}

func TestConfigExportGolden(t *testing.T) {
	for _, tt := range []struct {
		name              string
		projects, secrets bool
	}{
		{"default", false, false}, {"secrets", false, true}, {"projects", true, false}, {"projects-secrets", true, true},
	} {
		t.Run(tt.name, func(t *testing.T) {
			var out bytes.Buffer
			opts := configtransfer.Options{IncludeProjects: tt.projects, IncludeSecrets: tt.secrets, Now: time.Date(2026, 6, 15, 12, 0, 0, 0, time.UTC), Version: PythonVersion}
			summary, err := exportConfig(context.Background(), &out, opts, exportFixtureRead(t))
			if err != nil {
				t.Fatal(err)
			}
			want, err := os.ReadFile(filepath.Join(exportFixtures, tt.name+".json"))
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(out.Bytes(), want) {
				first := 0
				for first < len(want) && first < out.Len() && want[first] == out.Bytes()[first] {
					first++
				}
				t.Fatalf("export differs at byte %d:\ngot %s\nwant %s", first, out.String(), want)
			}
			const secret = "SYNTHETIC_CONFIG_EXPORT_SECRET_DO_NOT_USE"
			if bytes.Contains(out.Bytes(), []byte(secret)) != tt.secrets {
				t.Fatal("secret opt-in did not control exported values")
			}
			var expectedSummary struct {
				Counts           map[string]int `json:"counts"`
				Secrets, Repoint int
				Bytes            int64
			}
			summaryRaw, err := os.ReadFile(filepath.Join(exportFixtures, tt.name+".summary.json"))
			if err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(summaryRaw, &expectedSummary); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(summary.Counts, expectedSummary.Counts) || summary.Secrets != expectedSummary.Secrets || summary.Repoint != expectedSummary.Repoint || summary.Bytes != expectedSummary.Bytes {
				t.Fatalf("summary %#v != %#v", summary, expectedSummary)
			}
			if summary.Bytes != int64(out.Len()) || summary.Secrets == 0 {
				t.Fatalf("summary %#v, bytes %d", summary, out.Len())
			}
			var doc map[string]any
			if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
				t.Fatal(err)
			}
			if _, ok := doc["projects"]; ok != tt.projects {
				t.Fatal("projects opt-in ignored")
			}
			if tt.projects && (summary.Counts["projects"] < 1 || summary.Counts["schedules"] < 1 || summary.Repoint < 1) {
				t.Fatalf("project summary %#v", summary)
			}
		})
	}
}
func TestConfigExportFailure(t *testing.T) {
	fail := errors.New("synthetic failure")
	for _, section := range []string{"providers", "documents", "files"} {
		t.Run(section, func(t *testing.T) {
			read := exportFixtureRead(t)
			var out bytes.Buffer
			_, err := exportConfig(context.Background(), &out, configtransfer.Options{IncludeProjects: true}, func(ctx context.Context, key, id string) ([]map[string]any, error) {
				if key == section {
					return nil, fail
				}
				return read(ctx, key, id)
			})
			if !errors.Is(err, fail) {
				t.Fatalf("error %v", err)
			}
			if section == "providers" && out.Len() != 0 {
				t.Fatal("header written before reads completed")
			}
			if section != "providers" && (out.Len() == 0 || json.Valid(out.Bytes())) {
				t.Fatal("post-header failure did not leave partial JSON")
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := exportConfig(ctx, io.Discard, configtransfer.Options{}, exportFixtureRead(t)); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled export: %v", err)
	}
	if _, err := exportConfig(context.Background(), shortExportWriter{}, configtransfer.Options{}, exportFixtureRead(t)); !errors.Is(err, io.ErrShortWrite) {
		t.Fatalf("short write: %v", err)
	}
}

type shortExportWriter struct{}

func (shortExportWriter) Write(p []byte) (int, error) { return len(p) - 1, nil }
func TestWriteExportAtomicAndPermissions(t *testing.T) {
	for _, include := range []bool{false, true} {
		t.Run(map[bool]string{false: "public", true: "secret"}[include], func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "export.json")
			if err := os.WriteFile(path, []byte("original"), 0600); err != nil {
				t.Fatal(err)
			}
			fail := errors.New("read failed")
			export := func(ctx context.Context, w io.Writer, opts configtransfer.Options) (configtransfer.Summary, error) {
				_, err := io.WriteString(w, "partial")
				if err != nil {
					return configtransfer.Summary{}, err
				}
				return configtransfer.Summary{}, fail
			}
			opts := configtransfer.Options{IncludeSecrets: include}
			if _, err := writeExport(context.Background(), path, opts, export); !errors.Is(err, fail) {
				t.Fatal(err)
			}
			old, err := os.ReadFile(path)
			if err != nil || string(old) != "original" {
				t.Fatalf("failed export replaced original %q: %v", old, err)
			}
			fail = nil
			if _, err := writeExport(context.Background(), path, opts, export); err != nil {
				t.Fatal(err)
			}
			info, err := os.Stat(path)
			if err != nil {
				t.Fatal(err)
			}
			if include && info.Mode().Perm() != 0600 {
				t.Fatalf("secret mode %o", info.Mode().Perm())
			}
			if info.Mode().Perm()&0111 != 0 {
				t.Fatal("export executable")
			}
			entries, err := os.ReadDir(dir)
			if err != nil || len(entries) != 1 {
				t.Fatalf("temporary files left: %v %v", entries, err)
			}
		})
	}
}
func TestPortableExportSource(t *testing.T) {
	for _, value := range []string{"", "/local/file", "file:relative", "C:\\local\\file", "d:/local/file"} {
		if portableExportSource(value) != nil {
			t.Fatalf("machine path exported: %q", value)
		}
	}
	for _, value := range []string{"claude", "https://synthetic.invalid/repo", "github:org/repo"} {
		if portableExportSource(value) != value {
			t.Fatal(value)
		}
	}
}
func TestExportBadStoredInput(t *testing.T) {
	if _, err := exportProvider(map[string]any{"models": []any{true}}, false); err == nil {
		t.Fatal("bad catalog accepted")
	}
	if _, _, err := exportChannel(map[string]any{"config": "bad"}, false); err == nil {
		t.Fatal("bad channel accepted")
	}
	row := map[string]any{"channel_type": "telegram", "config": map[string]any{"allowed_user_ids": "invalid", "key": "synthetic"}}
	entry, count, err := exportChannel(row, false)
	if err != nil || count != 1 {
		t.Fatalf("bad legacy allowlist: %v %d", err, count)
	}
	raw, err := exportDump(entry)
	if err != nil || !strings.Contains(string(raw), `"allowed_user_ids":"invalid"`) || strings.Contains(string(raw), "synthetic") {
		t.Fatalf("invalid legacy allowlist changed or secret exposed: %s %v", raw, err)
	}
}

func TestExportDumpNonfiniteAndStringSpaces(t *testing.T) {
	value := exportObject{{Key: "nan", Value: math.NaN()}, {Key: "inf", Value: math.Inf(1)}, {Key: "neg", Value: math.Inf(-1)}, {Key: "text", Value: "a b \" c \\ d"}}
	got, err := exportDump(value)
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"nan":NaN,"inf":Infinity,"neg":-Infinity,"text":"a b \" c \\ d"}`
	if string(got) != want {
		t.Fatalf("compact dump %s != %s", got, want)
	}
}
