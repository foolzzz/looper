package operatorauth

import "testing"

func TestAccessOf(t *testing.T) {
	tests := []struct {
		path string
		want Access
	}{
		{path: "/login", want: AccessSignIn},
		{path: "/logout", want: AccessSignIn},
		{path: "/login/", want: AccessDashboard},
		{path: "/login/extra", want: AccessDashboard},
		{path: "/api/agent/v1/tools", want: AccessAgent},
		{path: "/api/agent/v1/../status", want: AccessAgent},
		{path: "/api/agent/v1", want: AccessOperator},
		{path: "/api/status", want: AccessOperator},
		{path: "/api/", want: AccessOperator},
		{path: "/api", want: AccessDashboard},
		{path: "/", want: AccessDashboard},
		{path: "/index.html", want: AccessDashboard},
		{path: "/tasks/task-1", want: AccessDashboard},
	}
	for _, tt := range tests {
		t.Run(tt.path, func(t *testing.T) {
			if got := AccessOf(tt.path); got != tt.want {
				t.Fatalf("access %d, want %d", got, tt.want)
			}
		})
	}
}

func TestPresentedAndIsOperator(t *testing.T) {
	const port = 18834
	cookie := "other=value; " + CookieName(port) + "=" + sampleToken
	tests := []struct {
		name          string
		authorization string
		cookie        string
		wantCred      string
		wantPresent   bool
		wantOperator  bool
	}{
		{name: "missing"},
		{name: "bearer", authorization: "Bearer " + sampleToken, wantCred: sampleToken, wantPresent: true, wantOperator: true},
		{name: "mixed case scheme", authorization: "bEaReR " + sampleToken, wantCred: sampleToken, wantPresent: true, wantOperator: true},
		{name: "bearer keeps the rest", authorization: "Bearer abc def", wantCred: "abc def", wantPresent: true},
		{name: "scheme only", authorization: "Bearer"},
		{name: "cookie", cookie: cookie, wantCred: sampleToken, wantPresent: true, wantOperator: true},
		{name: "bearer wins over cookie", authorization: "Bearer wrong", cookie: cookie, wantCred: "wrong", wantPresent: true, wantOperator: true},
		{name: "other port", cookie: CookieName(port+1) + "=" + sampleToken},
		{name: "empty cookie value", cookie: CookieName(port) + "=", wantCred: "", wantPresent: true},
		{name: "wrong bearer still presented", authorization: "Bearer wrong", wantCred: "wrong", wantPresent: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cred, present := Presented(tt.authorization, tt.cookie, port)
			if cred != tt.wantCred || present != tt.wantPresent {
				t.Fatalf("presented %q %v", cred, present)
			}
			if got := IsOperator(sampleToken, tt.authorization, tt.cookie, port); got != tt.wantOperator {
				t.Fatalf("operator %v", got)
			}
		})
	}
	if IsOperator("", "Bearer "+sampleToken, cookie, port) {
		t.Fatal("empty token matched")
	}
	if !IsOperator(sampleToken, "Bearer nope", cookie, port) {
		t.Fatal("cookie should match when the bearer does not")
	}
	if IsOperator("ab", "", CookieName(port)+"=a", port) {
		t.Fatal("length mismatch matched")
	}
}

func TestSignsIn(t *testing.T) {
	tests := []struct {
		name       string
		mode, dest string
		host, peer string
		forwarded  bool
		xff        bool
		want       bool
	}{
		{name: "localhost", mode: "navigate", dest: "document", host: "localhost:18834", peer: "127.0.0.1", want: true},
		{name: "ipv6", mode: "navigate", dest: "document", host: "[::1]:18834", peer: "::1", want: true},
		{name: "peer not loopback", mode: "navigate", dest: "document", host: "127.0.0.1:18834", peer: "10.0.0.5"},
		{name: "host not loopback", mode: "navigate", dest: "document", host: "evil.example:18834", peer: "127.0.0.1"},
		{name: "no peer", mode: "navigate", dest: "document", host: "127.0.0.1:18834"},
		{name: "iframe", mode: "navigate", dest: "iframe", host: "127.0.0.1:18834", peer: "127.0.0.1"},
		{name: "no fetch", host: "127.0.0.1:18834", peer: "127.0.0.1"},
		{name: "forwarded", mode: "navigate", dest: "document", host: "127.0.0.1:18834", peer: "127.0.0.1", forwarded: true},
		{name: "xff", mode: "navigate", dest: "document", host: "127.0.0.1:18834", peer: "127.0.0.1", xff: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := SignsIn(tt.mode, tt.dest, tt.host, tt.peer, tt.forwarded, tt.xff); got != tt.want {
				t.Fatalf("signs in %v", got)
			}
		})
	}
}

func TestSetCookie(t *testing.T) {
	got := SetCookie(sampleToken, 18834)
	want := "looper_operator_18834=" + sampleToken + "; Path=/; HttpOnly; SameSite=Strict"
	if got != want {
		t.Fatalf("cookie %s", got)
	}
}
