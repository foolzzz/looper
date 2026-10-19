package httpapi

import (
	"bufio"
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/foolzzz/looper/go/internal/agents/web"
)

const (
	// Python's iter_sse_events and iter_chat_events defaults. The HTTP
	// handlers do not shorten them.
	sseStreamTimeout = 300 * time.Second
	sseHeartbeat     = 15 * time.Second

	// Python writes these bytes itself. net/http would add Date and
	// Transfer-Encoding: chunked, so the handler hijacks the connection.
	sseCIDHeader = "HTTP/1.1 200 OK\r\n" +
		"Content-Type: text/event-stream\r\n" +
		"Cache-Control: no-cache\r\n" +
		"Connection: keep-alive\r\n" +
		"\r\n"
	sseSessionHeader = "HTTP/1.1 200 OK\r\n" +
		"Content-Type: text/event-stream\r\n" +
		"Cache-Control: no-cache\r\n" +
		"Connection: keep-alive\r\n" +
		"X-Accel-Buffering: no\r\n" +
		"\r\n"
)

func (s *Server) handleChatEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/chat/events" {
		notFound(w)
		return
	}
	query := pythonQuery(r.URL.RawQuery)
	cid := queryValue(query, "cid")
	session := queryValue(query, "session")
	// A cid wins. A blank cid is absent, so a session can still be selected.
	if cid == "" && session != "" {
		s.streamChatSession(w, r, session)
		return
	}
	if cid == "" {
		writePythonJSON(w, http.StatusBadRequest, errorBody{Error: "cid or session is required"})
		return
	}
	s.streamMessage(w, r, cid)
}

func (s *Server) streamMessage(w http.ResponseWriter, r *http.Request, cid string) {
	conn, rw, ok := hijackSSE(w)
	if !ok {
		return
	}
	defer conn.Close()
	ctx, cancel := context.WithCancel(contextOrBackground(r.Context()))
	defer cancel()
	// Register before the headers so a shutdown during the write closes
	// this socket and cancels the stream context.
	release := s.trackStream(conn, cancel)
	defer release()
	go watchPeer(rw.Reader, cancel)
	// A failed header write must not drop the stream. The iterator is what
	// removes it, and it has not started.
	if err := writeRaw(rw, sseCIDHeader); err != nil {
		return
	}

	adapter := s.chatAdapter()
	err := adapter.RunMessageEvents(ctx, cid, sseStreamTimeout, sseHeartbeat, func(ev web.Event) error {
		return writeFrame(rw, ev)
	})
	if err != nil {
		adapter.CloseSSE(cid)
	}
}

func (s *Server) streamChatSession(w http.ResponseWriter, r *http.Request, sessionID string) {
	adapter := s.chatAdapter()
	// Subscribe before the headers so a reply published while they are
	// written is queued. Close runs on every return, including a failed hijack.
	sub := adapter.OpenChat(sessionID)
	defer adapter.CloseChat(sessionID, sub)

	conn, rw, ok := hijackSSE(w)
	if !ok {
		return
	}
	defer conn.Close()
	ctx, cancel := context.WithCancel(contextOrBackground(r.Context()))
	defer cancel()
	release := s.trackStream(conn, cancel)
	defer release()
	if err := writeRaw(rw, sseSessionHeader); err != nil {
		return
	}
	go watchPeer(rw.Reader, cancel)
	_ = adapter.ReadChat(ctx, sub, sseHeartbeat, func(ev web.Event) error {
		return writeFrame(rw, ev)
	})
}

// chatStreams is the set of hijacked chat sockets. http.Server removes a
// connection from its active set at Hijack, and then Shutdown and Close
// both return while the handler is still blocked in the stream. stopStreams
// closes those sockets and cancels the stream contexts, which are children
// of the request contexts, so the session subscription is released.
type chatStreams struct {
	mu      sync.Mutex
	conns   map[net.Conn]context.CancelFunc
	stopped bool
}

// ShutdownBinder is implemented by Handler. serve calls BindShutdown so
// Shutdown and Close drop hijacked chat sockets.
type ShutdownBinder interface {
	BindShutdown(hs *http.Server, ln net.Listener) net.Listener
}

// BindShutdown registers stream cleanup on hs and returns the listener hs
// must serve. Shutdown runs RegisterOnShutdown. Close does not, and both
// close the listener, so the wrapper covers Close.
func (s *Server) BindShutdown(hs *http.Server, ln net.Listener) net.Listener {
	if hs != nil {
		hs.RegisterOnShutdown(s.stopStreams)
	}
	return &stopListener{Listener: ln, stop: s.stopStreams}
}

// StopStreams closes hijacked chat sockets and cancels their stream
// contexts. A second call is a no-op.
func (s *Server) StopStreams() {
	s.stopStreams()
}

func (s *Server) stopStreams() {
	if s == nil {
		return
	}
	s.chatStreams.mu.Lock()
	s.chatStreams.stopped = true
	conns := s.chatStreams.conns
	s.chatStreams.conns = nil
	s.chatStreams.mu.Unlock()
	for conn, cancel := range conns {
		cancel()
		_ = conn.Close()
	}
}

// trackStream records conn until the returned release runs. A server that
// has already stopped closes conn and cancels immediately.
func (s *Server) trackStream(conn net.Conn, cancel context.CancelFunc) func() {
	if cancel == nil {
		cancel = func() {}
	}
	s.chatStreams.mu.Lock()
	stopped := s.chatStreams.stopped
	if !stopped {
		if s.chatStreams.conns == nil {
			s.chatStreams.conns = make(map[net.Conn]context.CancelFunc)
		}
		s.chatStreams.conns[conn] = cancel
	}
	s.chatStreams.mu.Unlock()
	if stopped {
		cancel()
		_ = conn.Close()
		return func() {}
	}
	return func() {
		s.chatStreams.mu.Lock()
		delete(s.chatStreams.conns, conn)
		s.chatStreams.mu.Unlock()
	}
}

// stopListener runs stop when http.Server closes the listener during
// Shutdown or Close, before either method returns.
type stopListener struct {
	net.Listener
	stop func()
	once sync.Once
}

func (l *stopListener) Close() error {
	l.once.Do(func() {
		if l.stop != nil {
			l.stop()
		}
	})
	return l.Listener.Close()
}

// chatSaver is the write half of chat persistence. It is not part of Reader:
// a read-only fake still answers the save route, and the save error is logged.
type chatSaver interface {
	SaveChatMessage(ctx context.Context, sessionID, messageID, role, content string) error
}

func (s *Server) chatAdapter() *web.Adapter {
	if s.Web != nil {
		s.attachSaver(s.Web)
		return s.Web
	}
	s.webOnce.Do(func() {
		s.webLazy = web.New()
	})
	s.attachSaver(s.webLazy)
	return s.webLazy
}

func (s *Server) attachSaver(adapter *web.Adapter) {
	if saver, ok := s.Reads.(chatSaver); ok && saver != nil {
		adapter.SetSaver(saver)
	}
}

func hijackSSE(w http.ResponseWriter) (net.Conn, *bufio.ReadWriter, bool) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		writePythonJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return nil, nil, false
	}
	conn, rw, err := hj.Hijack()
	if err != nil {
		slog.Error("chat events", "err", err)
		writePythonJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
		return nil, nil, false
	}
	return conn, rw, true
}

func writeRaw(w *bufio.ReadWriter, raw string) error {
	if _, err := io.WriteString(w, raw); err != nil {
		return err
	}
	return w.Flush()
}

func writeFrame(w *bufio.ReadWriter, ev web.Event) error {
	frame, err := web.Format(ev)
	if err != nil {
		return err
	}
	if _, err := w.Write(frame); err != nil {
		return err
	}
	return w.Flush()
}

// watchPeer cancels when the peer goes away. After Hijack, a closed client
// does not cancel the request context, and the next event may be a long way
// off. One blocked read is enough: the HTTP request has already been consumed.
func watchPeer(r io.Reader, cancel context.CancelFunc) {
	if r == nil {
		return
	}
	var buf [1]byte
	_, _ = r.Read(buf[:])
	cancel()
}

func contextOrBackground(ctx context.Context) context.Context {
	if ctx == nil {
		return context.Background()
	}
	return ctx
}
