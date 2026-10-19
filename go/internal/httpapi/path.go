package httpapi

import (
	"net/http"
	"net/url"
	"path"
	"strings"
	"unicode"
)

// pythonURLPath is urllib.parse.urlparse(target).path for an HTTP request
// target. urlparse does not percent-decode and does not clean "." or
// duplicate slashes. The empty scheme is in uses_params, so a semicolon
// after the last slash is a path parameter and is not part of the path.
// /api/tasks/task-rich00001;foo is therefore /api/tasks/task-rich00001.
// A semicolon before a later slash stays, which is why
// /api/tasks/task-rich00001;foo/bar does not load the task.
func pythonURLPath(target string) string {
	raw := sanitizeRequestTarget(target)
	scheme, rest, ok := cutScheme(raw)
	if ok {
		raw = rest
	} else {
		scheme = ""
	}
	if strings.HasPrefix(raw, "//") {
		_, raw = splitNetloc(raw)
	}
	if i := strings.IndexByte(raw, '#'); i >= 0 {
		raw = raw[:i]
	}
	if i := strings.IndexByte(raw, '?'); i >= 0 {
		raw = raw[:i]
	}
	if usesParams(scheme) {
		raw = stripPathParams(raw)
	}
	return raw
}

// canonicalPath is net/http's cleanPath. ServeMux redirects when this
// differs from the escaped path. Python does not, so those paths are 404.
func canonicalPath(p string) string {
	if p == "" {
		return "/"
	}
	if p[0] != '/' {
		p = "/" + p
	}
	np := path.Clean(p)
	if p[len(p)-1] == '/' && np != "/" {
		if len(p) == len(np)+1 && strings.HasPrefix(p, np) {
			np = p
		} else {
			np += "/"
		}
	}
	return np
}

func requestTarget(r *http.Request) string {
	if r.RequestURI != "" {
		return r.RequestURI
	}
	if r.URL == nil {
		return ""
	}
	return r.URL.RequestURI()
}

// withPythonPath returns a request whose URL path is the path Python routes.
// The query string is left as Go parsed it. A path that is already that
// string is returned unchanged.
func withPythonPath(r *http.Request, escaped string) *http.Request {
	if r.URL != nil && r.URL.EscapedPath() == escaped {
		return r
	}
	r2 := r.Clone(r.Context())
	if r2.URL == nil {
		r2.URL = &url.URL{}
	}
	decoded, err := url.PathUnescape(escaped)
	if err != nil {
		decoded = escaped
	}
	r2.URL.Path = decoded
	if decoded == escaped {
		r2.URL.RawPath = ""
	} else {
		r2.URL.RawPath = escaped
	}
	return r2
}

func sanitizeRequestTarget(s string) string {
	s = strings.TrimLeftFunc(s, func(r rune) bool {
		return r == ' ' || r < 0x20
	})
	s = strings.ReplaceAll(s, "\t", "")
	s = strings.ReplaceAll(s, "\r", "")
	s = strings.ReplaceAll(s, "\n", "")
	return s
}

func cutScheme(s string) (scheme, rest string, ok bool) {
	i := strings.IndexByte(s, ':')
	if i <= 0 {
		return "", s, false
	}
	first := rune(s[0])
	if first > unicode.MaxASCII || !unicode.IsLetter(first) {
		return "", s, false
	}
	for _, c := range s[:i] {
		if !schemeChar(c) {
			return "", s, false
		}
	}
	return strings.ToLower(s[:i]), s[i+1:], true
}

func schemeChar(c rune) bool {
	return c < unicode.MaxASCII && (unicode.IsLetter(c) || unicode.IsDigit(c) || c == '+' || c == '-' || c == '.')
}

func splitNetloc(s string) (netloc, rest string) {
	rest = s
	delim := len(s)
	for _, c := range "/?#" {
		if i := strings.IndexRune(s[2:], c); i >= 0 && 2+i < delim {
			delim = 2 + i
		}
	}
	return s[2:delim], s[delim:]
}

func usesParams(scheme string) bool {
	switch scheme {
	case "", "ftp", "hdl", "prospero", "http", "imap", "https", "shttp",
		"rtsp", "rtsps", "rtspu", "sip", "sips", "mms", "sftp", "tel":
		return true
	default:
		return false
	}
}

// stripPathParams is urllib.parse._splitparams. The semicolon has to sit
// after the last slash; otherwise it is part of the path.
func stripPathParams(urlPath string) string {
	slash := strings.LastIndexByte(urlPath, '/')
	from := 0
	if slash >= 0 {
		from = slash
	}
	semi := strings.IndexByte(urlPath[from:], ';')
	if semi < 0 {
		return urlPath
	}
	return urlPath[:from+semi]
}
