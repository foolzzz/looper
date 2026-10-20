package httpapi

import (
	"bufio"
	"bytes"
	"crypto/subtle"
	"errors"
	"fmt"
	"html"
	"io"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/foolzzz/looper/go/internal/operatorauth"
)

const signInCSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"

const agentRefusal = "this endpoint is the Manager's: a valid token is required"

// refusalCloseDelay is how long a hijacked refusal waits before the final
// close. Closing at once, while the client still owes a body, can RST and
// drop the response the peer has not read yet. net/http uses the same pause
// when it gives up on a large unread body.
const refusalCloseDelay = 500 * time.Millisecond

// defaultRefusalWriteTimeout bounds a hijacked refusal write. A client that
// stops reading a large reflected Origin would otherwise block the handler
// until the process exits. The close timer starts only after this write
// returns. Shutdown closes the socket sooner, through the same registration
// as a hijacked chat stream.
const defaultRefusalWriteTimeout = 10 * time.Second

const loginPageNoPassword = "<p>No password is set. On the server's machine run <code>looper auth set-password</code>, " +
	"or open the dashboard there.</p>"

const loginPagePrefix = "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">" +
	"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Looper</title>" +
	"<style>:root{color-scheme:light dark;font:16px system-ui;background:#f4f5f7;color:#202124}" +
	"body{max-width:24rem;margin:12vh auto;padding:2rem}form,label{display:grid;gap:.6rem}" +
	"form{gap:1.2rem}input,button{font:inherit;padding:.7rem;border:1px solid #888;border-radius:.4rem}" +
	".error{color:#b3261e}@media(prefers-color-scheme:dark){:root{background:#18191b;color:#eee}" +
	".error{color:#ffb4ab}}</style><h1>Looper</h1>"

func (s *Server) operatorAuthOn() bool {
	return s != nil && (s.OperatorAuth || s.OperatorToken != "")
}

// finishBeforeBody applies the operator, agent, and sign-in gates.
// Operator and agent refusals return before the body is read. True means
// the response is finished.
func (s *Server) finishBeforeBody(w http.ResponseWriter, r *http.Request, path string, area operatorauth.Access) bool {
	switch area {
	case operatorauth.AccessOperator:
		if s.operatorAuthOn() && !s.isOperatorRequest(r) {
			s.writeOperatorRefusal(w, r, path)
			return true
		}
	case operatorauth.AccessAgent:
		s.handleAgentAPI(w, r, path)
		return true
	case operatorauth.AccessSignIn:
		if s.operatorAuthOn() {
			s.handleSignIn(w, r, path)
			return true
		}
	}
	return false
}

func (s *Server) isOperatorRequest(r *http.Request) bool {
	return operatorauth.IsOperator(s.OperatorToken, r.Header.Get("Authorization"), r.Header.Get("Cookie"), s.Port)
}

// browserOperator ignores Authorization. A page load signs in from the cookie
// or from loopback navigation, not from a bearer a page might have forwarded.
func (s *Server) browserOperator(r *http.Request) bool {
	return operatorauth.IsOperator(s.OperatorToken, "", r.Header.Get("Cookie"), s.Port)
}

func (s *Server) writeOperatorRefusal(w http.ResponseWriter, r *http.Request, path string) {
	credential, present := operatorauth.Presented(r.Header.Get("Authorization"), r.Header.Get("Cookie"), s.Port)
	reason := operatorauth.Refusal
	detail := "no operator credential was presented"
	if present && credential != "" {
		reason = operatorauth.Wrong
		detail = "no operator credential matched (one was presented)"
	}
	slog.Warn(fmt.Sprintf("refused %s %s from %s: %s", r.Method, path, peerHost(r.RemoteAddr), detail))
	s.writeWithoutDraining(w, func(w http.ResponseWriter) {
		w.Header().Set("WWW-Authenticate", `Bearer realm="looper"`)
		writePythonJSON(w, http.StatusUnauthorized, errorBody{Error: reason})
	})
}

func (s *Server) handleAgentAPI(w http.ResponseWriter, r *http.Request, path string) {
	// Every agent response is a refusal or a 404. None of them read the body.
	// GET /api/agent/v1/tools stays 404 until the Manager, Planner, or room
	// service exists. An empty tools list would claim that service is there.
	s.writeWithoutDraining(w, func(w http.ResponseWriter) {
		header := ""
		if r != nil {
			header = r.Header.Get("Authorization")
		}
		if !managerAuthorized(s.ManagerToken, header) {
			writePythonJSON(w, http.StatusUnauthorized, errorBody{Error: agentRefusal})
			return
		}
		method := ""
		if r != nil {
			method = r.Method
		}
		writePythonJSON(w, http.StatusNotFound, errorBody{Error: "no " + method + " " + path})
	})
}

// managerAuthorized matches ManagerOps.authorized: the scheme is folded, one
// space separates it from the token, and spaces around the token are ignored.
// An empty server token matches nothing, including an empty header.
func managerAuthorized(token, header string) bool {
	if token == "" {
		return false
	}
	scheme, rest, found := strings.Cut(header, " ")
	if !found || !strings.EqualFold(scheme, "bearer") {
		return false
	}
	value := strings.TrimSpace(rest)
	if len(value) != len(token) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(value), []byte(token)) == 1
}

func (s *Server) handleSignIn(w http.ResponseWriter, r *http.Request, path string) {
	// Password POST and logout are a later card. Anything that is not
	// GET /login is refused here, still without reading the body.
	s.writeWithoutDraining(w, func(w http.ResponseWriter) {
		if r.Method == http.MethodGet && path == "/login" {
			var cookies []string
			signedIn := s.browserOperator(r)
			if !signedIn && s.OperatorToken != "" && requestSignsIn(r) {
				signedIn = true
				cookies = []string{operatorauth.SetCookie(s.OperatorToken, s.Port)}
			}
			if signedIn {
				writeSeeOther(w, "/", cookies)
				return
			}
			writeLoginPage(w, http.StatusOK, "")
			return
		}
		writeSignInHTML(w, http.StatusMethodNotAllowed, "", nil)
	})
}

func requestSignsIn(r *http.Request) bool {
	_, forwarded := r.Header["Forwarded"]
	_, xff := r.Header["X-Forwarded-For"]
	return operatorauth.SignsIn(
		r.Header.Get("Sec-Fetch-Mode"),
		r.Header.Get("Sec-Fetch-Dest"),
		r.Host,
		peerHost(r.RemoteAddr),
		forwarded,
		xff,
	)
}

func peerHost(remoteAddr string) string {
	if remoteAddr == "" {
		return ""
	}
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		return remoteAddr
	}
	return host
}

func writeNoContent(w http.ResponseWriter) {
	w.Header().Set("Content-Length", "0")
	w.WriteHeader(http.StatusNoContent)
}

func setSignInHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Cache-Control", "no-store")
	h.Set("Referrer-Policy", "same-origin")
	h.Set("Content-Security-Policy", signInCSP)
}

func writeSeeOther(w http.ResponseWriter, location string, cookies []string) {
	w.Header().Set("Location", location)
	writeSignInHTML(w, http.StatusSeeOther, "", cookies)
}

func writeLoginPage(w http.ResponseWriter, code int, errorText string) {
	writeSignInHTML(w, code, loginPageHTML(errorText), nil)
}

func writeSignInHTML(w http.ResponseWriter, code int, body string, cookies []string) {
	h := w.Header()
	h.Set("Cache-Control", "no-store")
	h.Set("Referrer-Policy", "same-origin")
	h.Set("Content-Security-Policy", signInCSP)
	h.Set("Content-Type", "text/html; charset=utf-8")
	h.Set("Content-Length", strconv.Itoa(len(body)))
	for _, cookie := range cookies {
		h.Add("Set-Cookie", cookie)
	}
	w.WriteHeader(code)
	if body != "" {
		_, _ = io.WriteString(w, body)
	}
}

func loginPageHTML(errorText string) string {
	errHTML := ""
	if errorText != "" {
		errHTML = `<p class="error">` + html.EscapeString(errorText) + `</p>`
	}
	return loginPagePrefix + errHTML + loginPageNoPassword + "</html>"
}

func (s *Server) serveAuthedStatic(w http.ResponseWriter, r *http.Request) {
	urlPath := pythonURLPath(requestTarget(r))
	f, name, fellBack, err := openStatic(s.WebDir, urlPath)
	if err != nil {
		if errors.Is(err, errNotFound) {
			writeJSON(w, http.StatusNotFound, errorBody{Error: "not found"})
			return
		}
		slog.Error("static file", "err", err)
		writeJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return
	}
	defer f.Close()
	page := urlPath == "/" || urlPath == "/index.html" || fellBack
	if page && !s.browserOperator(r) && !(s.OperatorToken != "" && requestSignsIn(r)) {
		s.writeWithoutDraining(w, func(w http.ResponseWriter) {
			writeLoginPage(w, http.StatusOK, "")
		})
		return
	}
	data, err := io.ReadAll(f)
	if err != nil {
		slog.Error("read web file", "err", err)
		writeJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return
	}
	header := w.Header()
	header.Set("Content-Type", staticContentType(name))
	if page {
		header.Set("Cache-Control", "no-store")
		header.Set("Referrer-Policy", "same-origin")
	} else {
		header.Set("Cache-Control", "public, max-age=3600")
	}
	if page && s.OperatorToken != "" && requestSignsIn(r) {
		header.Set("Set-Cookie", operatorauth.SetCookie(s.OperatorToken, s.Port))
	}
	header.Set("Content-Length", strconv.Itoa(len(data)))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

func staticContentType(name string) string {
	// Python mimetypes.guess_type returns the media type and no charset.
	// mime.TypeByExtension adds "; charset=utf-8" for text types.
	ctype := mime.TypeByExtension(filepath.Ext(name))
	if ctype == "" {
		return "application/octet-stream"
	}
	media, _, err := mime.ParseMediaType(ctype)
	if err != nil || media == "" {
		return "application/octet-stream"
	}
	return media
}

// writeWithoutDraining writes a refusal and does not let net/http read the
// request body. Go 1.24 drains an unread body smaller than 256 KiB before it
// transmits the response, so a client that withholds Content-Length: 1 or a
// chunked body stalls the refusal. Hijack writes the bytes and closes.
// A ResponseWriter that cannot be hijacked, such as httptest.ResponseRecorder,
// never drains; the render runs on it directly.
//
// The hijacked socket is registered before the write. http.Server drops it
// at Hijack, so Shutdown and Close would not otherwise interrupt a peer that
// stops reading. The write itself has a deadline for the same stall.
func (s *Server) writeWithoutDraining(w http.ResponseWriter, render func(http.ResponseWriter)) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		render(w)
		return
	}
	conn, rw, err := hj.Hijack()
	if err != nil {
		slog.Error("hijack refusal", "err", err)
		w.Header().Set("Connection", "close")
		_ = http.NewResponseController(w).EnableFullDuplex()
		render(w)
		return
	}
	release := s.trackStream(conn, func() {})
	handedOff := false
	defer func() {
		if !handedOff {
			release()
		}
	}()
	if err := conn.SetWriteDeadline(time.Now().Add(s.refusalWriteLimit())); err != nil {
		slog.Error("refusal write deadline", "err", err)
	}
	buf := &bufferedResponse{header: make(http.Header)}
	render(buf)
	if err := buf.writeWire(rw); err != nil {
		slog.Error("write refusal", "err", err)
	}
	if err := rw.Flush(); err != nil {
		slog.Error("flush refusal", "err", err)
	}
	// A deadline that already fired must not fail the later half-close.
	_ = conn.SetWriteDeadline(time.Time{})
	handedOff = true
	closeRefusedConn(conn, release)
}

func (s *Server) refusalWriteLimit() time.Duration {
	if s != nil && s.refusalWriteTimeout > 0 {
		return s.refusalWriteTimeout
	}
	return defaultRefusalWriteTimeout
}

type bufferedResponse struct {
	header http.Header
	code   int
	body   bytes.Buffer
	wrote  bool
}

func (b *bufferedResponse) Header() http.Header {
	if b.header == nil {
		b.header = make(http.Header)
	}
	return b.header
}

func (b *bufferedResponse) WriteHeader(code int) {
	if b.wrote {
		return
	}
	b.wrote = true
	b.code = code
}

func (b *bufferedResponse) Write(p []byte) (int, error) {
	if !b.wrote {
		b.WriteHeader(http.StatusOK)
	}
	return b.body.Write(p)
}

func (b *bufferedResponse) writeWire(w *bufio.ReadWriter) error {
	if !b.wrote {
		b.code = http.StatusOK
	}
	if _, err := fmt.Fprintf(w, "HTTP/1.1 %d %s\r\n", b.code, http.StatusText(b.code)); err != nil {
		return err
	}
	if err := b.header.Write(w); err != nil {
		return err
	}
	if _, err := io.WriteString(w, "\r\n"); err != nil {
		return err
	}
	_, err := w.Write(b.body.Bytes())
	return err
}

func closeRefusedConn(conn net.Conn, release func()) {
	if release == nil {
		release = func() {}
	}
	if conn == nil {
		release()
		return
	}
	finish := func() {
		_ = conn.Close()
		release()
	}
	if tcp, ok := conn.(interface{ CloseWrite() error }); ok {
		if err := tcp.CloseWrite(); err != nil {
			finish()
			return
		}
	}
	// The handler returns now. The peer can read the refusal while the
	// final close waits out the RST window in the background. The socket
	// stays registered until that close, so shutdown still drops it.
	time.AfterFunc(refusalCloseDelay, finish)
}
