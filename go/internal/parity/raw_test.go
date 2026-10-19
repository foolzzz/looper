package parity

import (
	"bytes"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestReplaceJSONPointers(t *testing.T) {
	body := []byte(`{"z": 1.0, "b": "keep", "a": {"b": 9, "a/b": 1.0}, "a~b": 2}`)
	tests := []struct {
		name    string
		paths   []string
		want    string
		wantErr string
	}{
		{
			name:  "nested leaves sibling spelling and order",
			paths: []string{"/a/b"},
			want:  `{"z": 1.0, "b": "keep", "a": {"b": "<volatile>", "a/b": 1.0}, "a~b": 2}`,
		},
		{
			name:  "top-level name does not touch nested",
			paths: []string{"/b"},
			want:  `{"z": 1.0, "b": "<volatile>", "a": {"b": 9, "a/b": 1.0}, "a~b": 2}`,
		},
		{
			name:  "escaped slash and tilde",
			paths: []string{"/a/a~1b", "/a~0b"},
			want:  `{"z": 1.0, "b": "keep", "a": {"b": 9, "a/b": "<volatile>"}, "a~b": "<volatile>"}`,
		},
		{
			name:    "missing",
			paths:   []string{"/nope"},
			wantErr: "not found",
		},
		{
			name:    "overlap",
			paths:   []string{"/a", "/a/b"},
			wantErr: "overlapping",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := replaceJSONPointers(body, tt.paths)
			if tt.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
					t.Fatalf("err %v, want substring %q", err, tt.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != tt.want {
				t.Fatalf("got %s\nwant %s", got, tt.want)
			}
			if !bytes.Contains(got, []byte("1.0")) && strings.Contains(tt.want, "1.0") {
				t.Fatal("number was re-encoded")
			}
		})
	}
}

func TestCompareRaw(t *testing.T) {
	body := `{"z": 1.0, "a": 2}`
	base := pythonMessage("200 OK", "", body)
	tests := []struct {
		name    string
		golden  []byte
		live    func() (*http.Response, []byte)
		paths   []string
		wantErr string
	}{
		{
			name:   "HTTP 1.0 protocol rejected",
			golden: base,
			live: func() (*http.Response, []byte) {
				resp, b := liveMessage(200, "200 OK", body, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
				))
				resp.Proto, resp.ProtoMajor, resp.ProtoMinor = "HTTP/1.0", 1, 0
				return resp, b
			},
			wantErr: "protocol",
		},
		{
			name:   "date ignored",
			golden: base,
			live: func() (*http.Response, []byte) {
				return liveMessage(200, "200 OK", body, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
					"Date", "Thu, 01 Jan 1970 00:00:00 GMT",
				))
			},
		},
		{
			name:   "set-cookie order matches",
			golden: pythonMessage("200 OK", "Set-Cookie: a=1\r\nSet-Cookie: b=2\r\n", body),
			live: func() (*http.Response, []byte) {
				h := http.Header{}
				h.Add("Content-Type", "application/json; charset=utf-8")
				h.Set("Content-Length", strconv.Itoa(len(body)))
				h.Add("Set-Cookie", "a=1")
				h.Add("Set-Cookie", "b=2")
				return liveWith(200, "200 OK", body, h)
			},
		},
		{
			name:   "set-cookie order differs",
			golden: pythonMessage("200 OK", "Set-Cookie: a=1\r\nSet-Cookie: b=2\r\n", body),
			live: func() (*http.Response, []byte) {
				h := http.Header{}
				h.Add("Content-Type", "application/json; charset=utf-8")
				h.Set("Content-Length", strconv.Itoa(len(body)))
				h.Add("Set-Cookie", "b=2")
				h.Add("Set-Cookie", "a=1")
				return liveWith(200, "200 OK", body, h)
			},
			wantErr: "Set-Cookie",
		},
		{
			name:   "cache-control differs",
			golden: pythonMessage("200 OK", "Cache-Control: no-store\r\n", body),
			live: func() (*http.Response, []byte) {
				return liveMessage(200, "200 OK", body, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
					"Cache-Control", "private",
				))
			},
			wantErr: "Cache-Control",
		},
		{
			name:   "www-authenticate matches",
			golden: pythonMessage("401 Unauthorized", "WWW-Authenticate: Bearer realm=\"looper\"\r\n", `{"error": "unauthorized"}`),
			live: func() (*http.Response, []byte) {
				b := `{"error": "unauthorized"}`
				return liveMessage(401, "401 Unauthorized", b, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
					"WWW-Authenticate", `Bearer realm="looper"`,
				))
			},
		},
		{
			name:   "www-authenticate differs",
			golden: pythonMessage("401 Unauthorized", "WWW-Authenticate: Bearer realm=\"looper\"\r\n", `{"error": "unauthorized"}`),
			live: func() (*http.Response, []byte) {
				b := `{"error": "unauthorized"}`
				return liveMessage(401, "401 Unauthorized", b, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
					"WWW-Authenticate", "Bearer",
				))
			},
			wantErr: "Www-Authenticate",
		},
		{
			name:   "extra header",
			golden: base,
			live: func() (*http.Response, []byte) {
				return liveMessage(200, "200 OK", body, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
					"X-Extra", "1",
				))
			},
			wantErr: "X-Extra",
		},
		{
			name:   "missing product header",
			golden: pythonMessage("200 OK", "Cache-Control: no-store\r\n", body),
			live: func() (*http.Response, []byte) {
				return liveMessage(200, "200 OK", body, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
				))
			},
			wantErr: "Cache-Control",
		},
		{
			name:   "key order",
			golden: base,
			live: func() (*http.Response, []byte) {
				b := `{"a": 2, "z": 1.0}`
				return liveMessage(200, "200 OK", b, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
				))
			},
			wantErr: "raw body",
		},
		{
			name:   "number spelling",
			golden: pythonMessage("200 OK", "", `{"n": 1.0}`),
			live: func() (*http.Response, []byte) {
				b := `{"n": 1e0}`
				return liveMessage(200, "200 OK", b, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
				))
			},
			wantErr: "raw body",
		},
		{
			name:   "shorter number changes content length",
			golden: pythonMessage("200 OK", "", `{"n": 1.0}`),
			live: func() (*http.Response, []byte) {
				b := `{"n": 1}`
				return liveMessage(200, "200 OK", b, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
				))
			},
			wantErr: "Content-Length",
		},
		{
			name:   "extra field",
			golden: pythonMessage("200 OK", "", `{"a": 1}`),
			live: func() (*http.Response, []byte) {
				b := `{"a": 1, "b": 2}`
				return liveMessage(200, "200 OK", b, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
				))
			},
			wantErr: "Content-Length",
		},
		{
			name:   "pointer masks one value",
			golden: pythonMessage("200 OK", "", `{"z": 1.0, "b": "py"}`),
			paths:  []string{"/b"},
			live: func() (*http.Response, []byte) {
				b := `{"z": 1.0, "b": "go"}`
				return liveMessage(200, "200 OK", b, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
				))
			},
		},
		{
			name:   "missing pointer",
			golden: base,
			paths:  []string{"/missing"},
			live: func() (*http.Response, []byte) {
				return liveMessage(200, "200 OK", body, headerPairs(
					"Content-Type", "application/json; charset=utf-8",
				))
			},
			wantErr: "not found",
		},
		{
			name:   "missing content-length is not synthesized",
			golden: []byte("HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"),
			live: func() (*http.Response, []byte) {
				// ContentLength is 0 because the status is 204. The header
				// was not sent. Filling it in would hide that.
				return &http.Response{
					Proto:         "HTTP/1.1",
					ProtoMajor:    1,
					ProtoMinor:    1,
					StatusCode:    204,
					Status:        "204 No Content",
					Header:        http.Header{},
					ContentLength: 0,
				}, nil
			},
			wantErr: "Content-Length",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			resp, liveBody := tt.live()
			err := CompareRaw(resp, liveBody, tt.golden, tt.paths)
			if tt.wantErr == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("err %v, want substring %q", err, tt.wantErr)
			}
		})
	}
}

func TestNoContentShape(t *testing.T) {
	const want = "HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
	raw := []byte(want)
	code, status, header, body, err := parsePythonResponse(raw)
	if err != nil {
		t.Fatal(err)
	}
	if code != 204 || status != "204 No Content" || len(body) != 0 {
		t.Fatalf("code %d status %q body %q", code, status, body)
	}
	if header.Get("Content-Length") != "0" || header.Get("Content-Type") != "" {
		t.Fatalf("headers %v", header)
	}
	file, err := os.ReadFile(filepath.Join("..", "..", "testdata", "parity", "raw", "no-content.http"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(file, raw) {
		t.Fatalf("fixture %q", file)
	}
	h := http.Header{}
	h.Set("Content-Length", "0")
	h.Set("Date", "Thu, 01 Jan 1970 00:00:00 GMT")
	resp, liveBody := liveWith(204, "204 No Content", "", h)
	if err := CompareRaw(resp, liveBody, raw, nil); err != nil {
		t.Fatal(err)
	}
}

// rawExtras are response fixtures that are not goldenCases names.
var rawExtras = map[string]bool{
	"error-not-found":    true,
	"error-bad-host":     true,
	"error-cross-origin": true,
	"no-content":         true,
}

func TestRawPending(t *testing.T) {
	dir := filepath.Join("..", "..", "testdata", "parity", "raw")
	pending, err := loadPending(filepath.Join(dir, "PENDING.txt"))
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, tc := range append(append(goldenCases, knowledgeCases...), catalogCases...) {
		_, dup := seen[tc.name]
		seen[tc.name] = true
		if dup {
			t.Fatalf("duplicate golden %s", tc.name)
		}
		_, hasFile := os.Stat(filepath.Join(dir, tc.name+".http"))
		file := hasFile == nil
		_, isPending := pending[tc.name]
		switch {
		case tc.name == "status" || tc.status != http.StatusOK:
			if isPending || !file {
				t.Fatalf("%s status %d must have a raw golden and not be pending (file %v pending %v)", tc.name, tc.status, file, isPending)
			}
		default:
			if file == isPending {
				t.Fatalf("%s must be pending or raw, not both and not neither (file %v pending %v)", tc.name, file, isPending)
			}
		}
	}
	for name := range pending {
		if !seen[name] {
			t.Fatalf("pending %s is not a golden case", name)
		}
		if _, err := os.Stat(filepath.Join(dir, name+".http")); err == nil {
			t.Fatalf("pending %s also has a raw file", name)
		}
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		name := entry.Name()
		if name == "PENDING.txt" || strings.HasSuffix(name, ".paths") {
			continue
		}
		if !strings.HasSuffix(name, ".http") {
			t.Fatalf("unexpected raw file %s", name)
		}
		base := strings.TrimSuffix(name, ".http")
		if seen[base] || rawExtras[base] {
			continue
		}
		t.Fatalf("raw file %s is not a golden or a known extra", name)
	}
}

func loadPending(path string) (map[string]bool, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	out := map[string]bool{}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimRight(line, "\r")
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if out[line] {
			return nil, os.ErrExist
		}
		out[line] = true
	}
	return out, nil
}

func pythonMessage(status, extra, body string) []byte {
	var b strings.Builder
	b.WriteString("HTTP/1.1 ")
	b.WriteString(status)
	b.WriteString("\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: ")
	b.WriteString(strconv.Itoa(len(body)))
	b.WriteString("\r\n")
	b.WriteString(extra)
	b.WriteString("\r\n")
	b.WriteString(body)
	return []byte(b.String())
}

func headerPairs(kv ...string) http.Header {
	h := make(http.Header)
	for i := 0; i < len(kv); i += 2 {
		h.Add(kv[i], kv[i+1])
	}
	return h
}

func liveMessage(code int, status, body string, h http.Header) (*http.Response, []byte) {
	if h.Get("Content-Length") == "" {
		h.Set("Content-Length", strconv.Itoa(len(body)))
	}
	return liveWith(code, status, body, h)
}

func liveWith(code int, status, body string, h http.Header) (*http.Response, []byte) {
	return &http.Response{
		Proto:         "HTTP/1.1",
		ProtoMajor:    1,
		ProtoMinor:    1,
		StatusCode:    code,
		Status:        status,
		Header:        h,
		ContentLength: int64(len(body)),
	}, []byte(body)
}

func TestReplaceJSONPointersPythonNaN(t *testing.T) {
	tests := []struct {
		name string
		body string
		path string
		want string
	}{
		{"scalar", `NaN`, "", `"<volatile>"`},
		{"object value", `{"n": NaN, "keep": 1.0}`, "/n", `{"n": "<volatile>", "keep": 1.0}`},
		{"skip preceding NaN", `{"n": NaN, "keep": 1.0}`, "/keep", `{"n": NaN, "keep": "<volatile>"}`},
		{"container", `{"values": [NaN, Infinity, -Infinity, null]}`, "/values", `{"values": "<volatile>"}`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := replaceJSONPointers([]byte(tt.body), []string{tt.path})
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != tt.want {
				t.Fatalf("got %s, want %s", got, tt.want)
			}
		})
	}
}
