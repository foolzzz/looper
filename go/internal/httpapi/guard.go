package httpapi

import (
	"net"
	"net/http"
	"net/url"
	"strings"
)

// requestRefusal is looper/agents/request_guard.py refusal.
// A loopback bind rejects a Host that is not loopback (DNS rebinding).
// An Origin whose netloc is not the Host is a cross-origin browser request.
// A missing Origin is a non-browser client and is allowed.
// The messages match Python, including repr of the raw header value.
func requestRefusal(bindHost string, r *http.Request) string {
	// Python strips each header value before refusal() sees it.
	host := strings.TrimSpace(r.Host)
	if host != "" && isLoopback(bindHost) && !isLoopback(authorityHostname(host)) {
		return "host " + pythonRepr(host) + " is not this server"
	}
	origins := r.Header.Values("Origin")
	if len(origins) == 0 {
		return ""
	}
	// Duplicate Origin headers: Python's header map keeps the last value.
	origin := strings.TrimSpace(origins[len(origins)-1])
	if strings.ToLower(originNetloc(origin)) != strings.ToLower(host) {
		return "cross-origin request from " + pythonRepr(origin) + " refused"
	}
	return ""
}

// isLoopback matches request_guard._is_loopback. "localhost" is compared as
// written: the Host header is lowercased by authorityHostname first, the bind
// address is not.
func isLoopback(hostname string) bool {
	if hostname == "localhost" {
		return true
	}
	ip := net.ParseIP(hostname)
	return ip != nil && ip.IsLoopback()
}

// authorityHostname is urllib.parse.urlsplit("//"+authority).hostname.
func authorityHostname(authority string) string {
	u, err := url.Parse("//" + authority)
	if err != nil || u == nil {
		return ""
	}
	return strings.ToLower(u.Hostname())
}

// originNetloc is urllib.parse.urlsplit(origin).netloc, userinfo included.
func originNetloc(origin string) string {
	u, err := url.Parse(origin)
	if err != nil || u == nil {
		return ""
	}
	if u.User != nil {
		return u.User.String() + "@" + u.Host
	}
	return u.Host
}
