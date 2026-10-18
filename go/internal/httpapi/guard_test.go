package httpapi

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRequestGuard(t *testing.T) {
	const attacker = "https://attacker.example"
	hostRebind := "host 'evil.example:8767' is not this server"
	crossAttacker := "cross-origin request from 'https://attacker.example' refused"
	tests := []struct {
		name    string
		bind    string
		host    string
		origins []string // nil: no Origin header. More than one: the last wins.
		path    string
		method  string
		want    int
		reason  string
	}{
		{name: "curl loopback", bind: "127.0.0.1", host: "127.0.0.1:8767", want: http.StatusOK},
		{name: "dashboard", bind: "127.0.0.1", host: "127.0.0.1:8767", origins: []string{"http://127.0.0.1:8767"}, want: http.StatusOK},
		{name: "localhost", bind: "127.0.0.1", host: "localhost:8767", origins: []string{"http://localhost:8767"}, want: http.StatusOK},
		{name: "vite proxy", bind: "127.0.0.1", host: "localhost:5173", origins: []string{"http://localhost:5173"}, want: http.StatusOK},
		{name: "ipv6", bind: "127.0.0.1", host: "[::1]:8767", origins: []string{"http://[::1]:8767"}, want: http.StatusOK},
		{name: "no host", bind: "127.0.0.1", host: "", want: http.StatusOK},
		{name: "default bind is loopback", bind: "", host: "127.0.0.1:8767", want: http.StatusOK},
		{name: "exposed same origin", bind: "0.0.0.0", host: "10.0.0.5:8767", origins: []string{"http://10.0.0.5:8767"}, want: http.StatusOK},
		{name: "last origin wins when it matches", bind: "127.0.0.1", host: "127.0.0.1:8767", origins: []string{attacker, "http://127.0.0.1:8767"}, want: http.StatusOK},

		{name: "cross origin", bind: "127.0.0.1", host: "127.0.0.1:8767", origins: []string{attacker}, want: http.StatusForbidden, reason: crossAttacker},
		{name: "other local port", bind: "127.0.0.1", host: "127.0.0.1:8767", origins: []string{"http://127.0.0.1:9999"}, want: http.StatusForbidden, reason: "cross-origin request from 'http://127.0.0.1:9999' refused"},
		{name: "null origin", bind: "127.0.0.1", host: "127.0.0.1:8767", origins: []string{"null"}, want: http.StatusForbidden, reason: "cross-origin request from 'null' refused"},
		{name: "dns rebind with matching origin", bind: "127.0.0.1", host: "evil.example:8767", origins: []string{"http://evil.example:8767"}, path: "/api/nope", want: http.StatusForbidden, reason: hostRebind},
		{name: "dns rebind without origin", bind: "127.0.0.1", host: "evil.example:8767", want: http.StatusForbidden, reason: hostRebind},
		{name: "exposed cross origin", bind: "0.0.0.0", host: "10.0.0.5:8767", origins: []string{attacker}, want: http.StatusForbidden, reason: crossAttacker},
		{name: "last origin wins when it does not match", bind: "127.0.0.1", host: "127.0.0.1:8767", origins: []string{"http://127.0.0.1:8767", attacker}, want: http.StatusForbidden, reason: crossAttacker},
		{name: "preflight", bind: "127.0.0.1", host: "127.0.0.1:8767", origins: []string{attacker}, method: http.MethodOptions, path: "/", want: http.StatusForbidden, reason: crossAttacker},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			method := tt.method
			if method == "" {
				method = http.MethodGet
			}
			path := tt.path
			if path == "" {
				path = "/api/status"
			}
			srv := New("dev", &fakeDB{}, "")
			srv.BindHost = tt.bind
			req := httptest.NewRequest(method, path, nil)
			req.Host = tt.host
			for _, origin := range tt.origins {
				req.Header.Add("Origin", origin)
			}
			rr := httptest.NewRecorder()
			srv.Handler().ServeHTTP(rr, req)
			if rr.Code != tt.want {
				t.Fatalf("status %d, want %d, body %s", rr.Code, tt.want, rr.Body.String())
			}
			if rr.Header().Get("Access-Control-Allow-Origin") != "" {
				t.Fatalf("CORS header %q", rr.Header().Get("Access-Control-Allow-Origin"))
			}
			if tt.reason == "" {
				return
			}
			want := `{"error":"` + tt.reason + `"}`
			if rr.Body.String() != want {
				t.Fatalf("body %s, want %s", rr.Body.String(), want)
			}
			if ct := rr.Header().Get("Content-Type"); ct != "application/json" {
				t.Fatalf("content type %q", ct)
			}
		})
	}
}
