package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"testing"
)

const operatorAuthFixtures = "../../testdata/parity/operator_auth"

type operatorAuthManifest struct {
	Port          int                `json:"port"`
	OperatorToken string             `json:"operator_token"`
	AgentToken    string             `json:"agent_token"`
	Files         map[string]string  `json:"files"`
	Cases         []operatorAuthCase `json:"cases"`
}

type operatorAuthCase struct {
	Name               string            `json:"name"`
	Method             string            `json:"method"`
	Target             string            `json:"target"`
	Headers            map[string]string `json:"headers"`
	Host               string            `json:"host"`
	Remote             string            `json:"remote"`
	ClearOperatorToken bool              `json:"clear_operator_token"`
}

func TestOperatorAuthMatchesPython(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join(operatorAuthFixtures, "cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest operatorAuthManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.Port == 0 || manifest.OperatorToken == "" || manifest.AgentToken == "" || len(manifest.Cases) == 0 {
		t.Fatal("operator auth manifest is incomplete")
	}
	web := t.TempDir()
	for name, body := range manifest.Files {
		path := filepath.Join(web, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	api := New("test", nil, web)
	api.OperatorAuth = true
	api.ManagerToken = manifest.AgentToken
	api.Port = manifest.Port
	handler := api.Handler()

	entries, err := os.ReadDir(operatorAuthFixtures)
	if err != nil {
		t.Fatal(err)
	}
	wantFiles := map[string]bool{"cases.json": true}
	for _, tt := range manifest.Cases {
		wantFiles[tt.Name+".http"] = true
	}
	for _, entry := range entries {
		if !wantFiles[entry.Name()] {
			t.Errorf("unexpected fixture %s", entry.Name())
		}
		delete(wantFiles, entry.Name())
	}
	if len(wantFiles) != 0 {
		t.Fatalf("missing fixtures %v", wantFiles)
	}

	host := "127.0.0.1:" + strconv.Itoa(manifest.Port)
	for _, tt := range manifest.Cases {
		t.Run(tt.Name, func(t *testing.T) {
			if tt.ClearOperatorToken {
				api.OperatorToken = ""
			} else {
				api.OperatorToken = manifest.OperatorToken
			}
			body := &boomBody{}
			req := httptest.NewRequest(tt.Method, "http://"+host+tt.Target, body)
			req.Host = host
			req.RemoteAddr = "127.0.0.1:12345"
			if tt.Host != "" {
				req.Host = tt.Host
			}
			if tt.Remote != "" {
				req.RemoteAddr = tt.Remote
			}
			for key, value := range tt.Headers {
				req.Header.Set(key, value)
			}
			rr := httptest.NewRecorder()
			handler.ServeHTTP(rr, req)
			if body.read {
				t.Fatal("handler read the body")
			}
			golden, err := os.ReadFile(filepath.Join(operatorAuthFixtures, tt.Name+".http"))
			if err != nil {
				t.Fatal(err)
			}
			assertPythonResponse(t, rr, golden)
			if bytes.Contains(rr.Body.Bytes(), []byte("operator-token")) {
				t.Fatal("body named the token file")
			}
		})
	}
}

func assertPythonResponse(t *testing.T, rr *httptest.ResponseRecorder, golden []byte) {
	t.Helper()
	gotStatus := "HTTP/1.1 " + strconv.Itoa(rr.Code) + " " + http.StatusText(rr.Code)
	got := rr.Header().Clone()
	got.Del("Date")
	assertRawHTTP(t, gotStatus, got, rr.Body.Bytes(), golden)
}

// parsePythonHTTP keeps Content-Length. net/http's ReadResponse removes it.
func parsePythonHTTP(t *testing.T, raw []byte) (status string, header http.Header, body []byte) {
	t.Helper()
	head, body, ok := bytes.Cut(raw, []byte("\r\n\r\n"))
	if !ok {
		t.Fatalf("no header break\n%s", raw)
	}
	lines := bytes.Split(head, []byte("\r\n"))
	if len(lines) == 0 || len(lines[0]) == 0 {
		t.Fatalf("empty status\n%s", raw)
	}
	status = string(lines[0])
	header = make(http.Header)
	for _, line := range lines[1:] {
		key, value, found := bytes.Cut(line, []byte(":"))
		if !found {
			t.Fatalf("bad header %s", line)
		}
		header.Add(string(bytes.TrimSpace(key)), string(bytes.TrimSpace(value)))
	}
	return status, header, body
}

func assertRawHTTP(t *testing.T, status string, header http.Header, body, golden []byte) {
	t.Helper()
	wantStatus, wantHeader, wantBody := parsePythonHTTP(t, golden)
	if status != wantStatus {
		t.Fatalf("status %s, python %s", status, wantStatus)
	}
	if !bytes.Equal(body, wantBody) {
		t.Fatalf("body\n got %q\nwant %q", body, wantBody)
	}
	if cl := header.Values("Content-Length"); len(cl) != 1 || cl[0] != strconv.Itoa(len(body)) {
		t.Fatalf("content-length %q, body %d", cl, len(body))
	}
	if !reflect.DeepEqual(header, wantHeader) {
		t.Fatalf("headers\n got %v\nwant %v", header, wantHeader)
	}
}
