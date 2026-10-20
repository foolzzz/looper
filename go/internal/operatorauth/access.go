package operatorauth

import (
	"crypto/subtle"
	"net"
	"net/url"
	"strconv"
	"strings"
	"unicode"
)

// Access is which credential a path requires. Classification uses the raw
// URL path, before "." or ".." are cleaned.
type Access int

const (
	AccessDashboard Access = iota
	AccessAgent
	AccessOperator
	AccessSignIn
)

const (
	agentPrefix    = "/api/agent/v1/"
	operatorPrefix = "/api/"
	cookiePrefix   = "looper_operator_"
)

// AccessOf matches operator_auth.access. /login and /logout are exact.
// The agent prefix is tested before /api/. /api/agent/v1 without a slash
// is an operator route. /api without a slash is the dashboard.
func AccessOf(path string) Access {
	if path == "/login" || path == "/logout" {
		return AccessSignIn
	}
	if strings.HasPrefix(path, agentPrefix) {
		return AccessAgent
	}
	if strings.HasPrefix(path, operatorPrefix) {
		return AccessOperator
	}
	return AccessDashboard
}

// CookieName is looper_operator_<port>.
func CookieName(port int) string {
	return cookiePrefix + strconv.Itoa(port)
}

// SetCookie is the dashboard auto-login cookie. No Secure and no Max-Age.
func SetCookie(token string, port int) string {
	return CookieName(port) + "=" + token + "; Path=/; HttpOnly; SameSite=Strict"
}

// Presented is the bearer value when the scheme is present, including a
// value that is only whitespace after the scheme is rejected as absent.
// Otherwise it is the first cookie with this port's name. The bool is false
// when neither is present. An empty cookie value is present and falsy.
func Presented(authorization, cookieHeader string, port int) (string, bool) {
	if cred, ok := bearer(authorization); ok {
		return cred, true
	}
	return cookieValue(cookieHeader, CookieName(port))
}

// IsOperator reports whether the bearer or the cookie matches token.
// The two are compared independently. An empty token is Python None and
// matches nothing. Length mismatches do not match.
func IsOperator(token, authorization, cookieHeader string, port int) bool {
	if token == "" {
		return false
	}
	want := []byte(token)
	if cred, ok := bearer(authorization); ok && subtle.ConstantTimeCompare([]byte(cred), want) == 1 {
		return true
	}
	if cred, ok := cookieValue(cookieHeader, CookieName(port)); ok && subtle.ConstantTimeCompare([]byte(cred), want) == 1 {
		return true
	}
	return false
}

// SignsIn is a top-level browser navigation from the server's own machine.
// host is the Host header; only its hostname is tested. peer is the TCP
// host with no port. Proxy header presence blocks sign-in even when the
// value is empty. An empty peer does not sign in.
func SignsIn(mode, dest, host, peer string, hasForwarded, hasXFF bool) bool {
	return mode == "navigate" && dest == "document" &&
		peer != "" && loopback(peer) &&
		loopback(hostName(host)) &&
		!hasForwarded && !hasXFF
}

func bearer(authorization string) (string, bool) {
	s := strings.TrimLeftFunc(authorization, unicode.IsSpace)
	if s == "" {
		return "", false
	}
	i := strings.IndexFunc(s, unicode.IsSpace)
	if i < 0 {
		return "", false
	}
	scheme := s[:i]
	rest := strings.TrimLeftFunc(s[i+1:], unicode.IsSpace)
	if rest == "" || !strings.EqualFold(scheme, "bearer") {
		return "", false
	}
	return rest, true
}

func cookieValue(header, name string) (string, bool) {
	for _, item := range strings.Split(header, ";") {
		key, value, ok := strings.Cut(item, "=")
		if !ok {
			continue
		}
		if strings.TrimSpace(key) == name {
			return strings.TrimSpace(value), true
		}
	}
	return "", false
}

func loopback(hostname string) bool {
	if hostname == "localhost" {
		return true
	}
	ip := net.ParseIP(hostname)
	return ip != nil && ip.IsLoopback()
}

func hostName(authority string) string {
	u, err := url.Parse("//" + authority)
	if err != nil || u == nil {
		return ""
	}
	return strings.ToLower(u.Hostname())
}
