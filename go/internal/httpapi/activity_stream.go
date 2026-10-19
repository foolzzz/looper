package httpapi

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"github.com/foolzzz/looper/go/internal/agents/broadcast"
	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/pyjson"
)

const (
	// activityStreamPage is list_activity's limit inside the SSE replay loop.
	activityStreamPage int64 = 500
	// activityPing is asyncio.wait_for(..., timeout=20.0) on the subscriber.
	activityPing = 20 * time.Second
	// activityReplayQuery bounds one list_activity page. The live stream's
	// context has no deadline, and the pool allows eight connections. A stuck
	// page must release its connection while this stream stays open.
	activityReplayQuery = 30 * time.Second

	// activitySSEHeader is _handle_task_activity_stream's response head.
	// It matches the open-chat stream, including X-Accel-Buffering.
	activitySSEHeader = "HTTP/1.1 200 OK\r\n" +
		"Content-Type: text/event-stream\r\n" +
		"Cache-Control: no-cache\r\n" +
		"Connection: keep-alive\r\n" +
		"X-Accel-Buffering: no\r\n" +
		"\r\n"
	// activityPingFrame is the idle comment. It is not an event.
	activityPingFrame = ": ping\n\n"
	// activityDoneFrame is what Close writes. The payload is the literal
	// "{}" from the Python handler, not a json.dumps of a dict with spaces.
	activityDoneFrame = "event: done\ndata: {}\n\n"
)

// activityColumns is list_activity's SELECT order. asyncpg's dict keeps it,
// and json.dumps writes it. Replay frames use this order.
var activityColumns = []string{
	"seq", "task_id", "worker", "kind", "summary", "metadata",
	"input_tokens", "output_tokens", "created_at",
}

var activityStreamPattern = regexp.MustCompile(`^/api/tasks/(task-[a-zA-Z0-9]+)/activity/stream$`)

func (s *Server) handleActivityStream(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	match := activityStreamPattern.FindStringSubmatch(r.URL.EscapedPath())
	if match == nil {
		notFound(w)
		return
	}
	// int() runs before the SSE headers. ValueError is the request loop's 500.
	sinceRaw := queryValue(pythonQuery(r.URL.RawQuery), "since")
	if sinceRaw == "" {
		sinceRaw = "0"
	}
	since, overflow, ok := pythonInt(sinceRaw)
	if !ok {
		internalServerError(w, errors.New("since is not an integer"))
		return
	}
	// A Python int that does not fit int64 cannot be a query argument. The
	// database rejects it inside the replay try, after the headers, and the
	// live stream still starts. Skip the query and do the same.
	querySince := overflow == 0

	conn, rw, hijacked := hijackStream(w, "activity stream")
	if !hijacked {
		return
	}
	defer conn.Close()
	ctx, cancel := context.WithCancel(contextOrBackground(r.Context()))
	defer cancel()
	release := s.trackStream(conn, cancel)
	defer release()
	go watchPeer(rw.Reader, cancel)
	if err := writeRaw(rw, activitySSEHeader); err != nil {
		slog.Debug("activity stream write", "err", err)
		return
	}
	if err := s.replayActivity(ctx, rw, match[1], since, querySince); err != nil {
		return
	}
	if err := ctx.Err(); err != nil {
		return
	}

	bus := s.TaskActivity()
	sub := bus.Subscribe(match[1])
	defer bus.Unsubscribe(match[1], sub)
	for {
		event, end, timedOut, ok := s.waitActivity(ctx, sub)
		if !ok {
			return
		}
		if timedOut {
			if err := writeRaw(rw, activityPingFrame); err != nil {
				slog.Debug("activity stream write", "err", err)
				return
			}
			continue
		}
		if end {
			if err := writeRaw(rw, activityDoneFrame); err != nil {
				slog.Debug("activity stream write", "err", err)
			}
			return
		}
		frame, err := formatActivityLive(event)
		if err != nil {
			// A value that is not an ordered object cannot match Python's
			// dict order. Skip it. The next event still goes out.
			slog.Error("activity stream", "err", err)
			continue
		}
		if err := writeBytes(rw, frame); err != nil {
			slog.Debug("activity stream write", "err", err)
			return
		}
	}
}

// TaskActivity is the broadcaster activity streams subscribe to.
// Set Activity before the server starts so publishers share it. A nil
// Activity uses one broadcaster created on the first call.
// Publish an ordered pyjson.Object. Close ends every stream for that task
// with the done frame.
func (s *Server) TaskActivity() *broadcast.Broadcaster {
	if s == nil {
		return nil
	}
	if s.Activity != nil {
		return s.Activity
	}
	s.activityOnce.Do(func() {
		s.activityLazy = broadcast.New()
	})
	return s.activityLazy
}

// replayActivity writes history after since, one page of 500 at a time.
// query is false when since does not fit in int64. A database error is
// logged and replay stops; the caller still opens the live stream.
// A write error is returned so the caller does not subscribe.
func (s *Server) replayActivity(ctx context.Context, rw *bufio.ReadWriter, taskID string, since int64, query bool) error {
	if !query {
		slog.Error("activity stream", "err", "since does not fit")
		return nil
	}
	if s == nil || s.Reads == nil {
		slog.Error("activity stream", "err", "no database")
		return nil
	}
	cursor := since
	for {
		if err := ctx.Err(); err != nil {
			return nil
		}
		rows, err := s.listActivityPage(ctx, taskID, cursor)
		if err != nil {
			// A deadline on the page context is a stuck query: the client is
			// still connected, so log it and keep going. Parent cancellation
			// is the client leaving. Either way replay stops and, when the
			// client is still there, the live stream starts. That is Python's
			// except Exception around the replay loop.
			level := slog.LevelError
			if ctx.Err() != nil {
				level = slog.LevelDebug
			}
			slog.Log(ctx, level, "activity stream", "err", db.LogText(err))
			return nil
		}
		if len(rows) == 0 {
			return nil
		}
		for _, row := range rows {
			frame, next, ok := formatActivityReplay(row)
			if frame == nil {
				slog.Error("activity stream", "err", "activity row is not JSON")
				return nil
			}
			if err := writeBytes(rw, frame); err != nil {
				return fmt.Errorf("write activity frame: %w", err)
			}
			if !ok || next <= cursor {
				slog.Error("activity stream", "err", "activity page did not advance")
				return nil
			}
			cursor = next
		}
	}
}

// listActivityPage reads one page on a child of ctx. The child has its own
// deadline. Canceling ctx cancels the page. The deadline does not cancel ctx,
// so a slow page cannot pin a pool connection for the life of the stream.
func (s *Server) listActivityPage(ctx context.Context, taskID string, cursor int64) ([]map[string]any, error) {
	qctx, cancel := s.replayQueryContext(ctx)
	defer cancel()
	return s.Reads.ListActivity(qctx, taskID, cursor, activityStreamPage)
}

func (s *Server) replayQueryContext(ctx context.Context) (context.Context, context.CancelFunc) {
	limit := activityReplayQuery
	if s != nil && s.activityQueryLimit > 0 {
		limit = s.activityQueryLimit
	}
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithTimeout(ctx, limit)
}

func (s *Server) waitActivity(ctx context.Context, sub *broadcast.Sub) (event any, end, timedOut, ok bool) {
	timeout, stop := s.activityTimeout()
	defer stop()
	return sub.NextUntil(ctx, timeout)
}

func (s *Server) activityTimeout() (<-chan time.Time, func()) {
	if s != nil && s.activityAfter != nil {
		return s.activityAfter(activityPing), func() {}
	}
	timer := time.NewTimer(activityPing)
	return timer.C, func() { stopActivityTimer(timer) }
}

func stopActivityTimer(timer *time.Timer) {
	if timer == nil || timer.Stop() {
		return
	}
	select {
	case <-timer.C:
	default:
	}
}

// formatActivityReplay is one history frame. The id is row["seq"], or 0
// when the key is absent, matching row.get("seq", 0). ok is false when
// that seq cannot be the next cursor.
func formatActivityReplay(row map[string]any) (frame []byte, next int64, ok bool) {
	body, err := pyjson.Marshal(activityObject(row))
	if err != nil {
		return nil, 0, false
	}
	frame = activityFrame(replayID(row), body)
	next, ok = replayCursor(row)
	return frame, next, ok
}

// formatActivityLive is one published event. A missing seq is an empty id,
// matching event.get("seq", "").
func formatActivityLive(event any) ([]byte, error) {
	obj, ok := event.(pyjson.Object)
	if !ok {
		return nil, fmt.Errorf("activity event %T", event)
	}
	body, err := pyjson.Marshal(obj)
	if err != nil {
		return nil, fmt.Errorf("activity event: %w", err)
	}
	return activityFrame(liveID(obj), body), nil
}

func activityObject(row map[string]any) pyjson.Object {
	if row == nil {
		return pyjson.Object{}
	}
	obj := make(pyjson.Object, 0, len(activityColumns))
	for _, key := range activityColumns {
		v, exists := row[key]
		if !exists {
			continue
		}
		obj = append(obj, pyjson.Field{Key: key, Value: activityJSON(v)})
	}
	return obj
}

func activityJSON(v any) any {
	if b, ok := v.([]byte); ok {
		return string(b)
	}
	return v
}

func activityFrame(id string, data []byte) []byte {
	frame := make([]byte, 0, len(id)+len(data)+32)
	frame = append(frame, "id: "...)
	frame = append(frame, id...)
	frame = append(frame, "\nevent: activity\ndata: "...)
	frame = append(frame, data...)
	frame = append(frame, "\n\n"...)
	return frame
}

func replayID(row map[string]any) string {
	v, exists := row["seq"]
	if !exists {
		return "0"
	}
	return pythonScalarID(v)
}

func replayCursor(row map[string]any) (int64, bool) {
	v, exists := row["seq"]
	if !exists {
		return 0, true
	}
	return activityInt(v)
}

func liveID(obj pyjson.Object) string {
	for _, field := range obj {
		if field.Key == "seq" {
			return pythonScalarID(field.Value)
		}
	}
	return ""
}

func activityInt(v any) (int64, bool) {
	switch n := v.(type) {
	case int:
		return int64(n), true
	case int8:
		return int64(n), true
	case int16:
		return int64(n), true
	case int32:
		return int64(n), true
	case int64:
		return n, true
	case uint8:
		return int64(n), true
	case uint16:
		return int64(n), true
	case uint32:
		return int64(n), true
	case uint64:
		if n > uint64(^uint64(0)>>1) {
			return 0, false
		}
		return int64(n), true
	default:
		return 0, false
	}
}

// pythonScalarID is an f-string of a JSON scalar. None is the word None.
// Integers have no decimal point. True and False use Python's spelling.
func pythonScalarID(v any) string {
	if v == nil {
		return "None"
	}
	switch n := v.(type) {
	case int:
		return strconv.Itoa(n)
	case int8:
		return strconv.FormatInt(int64(n), 10)
	case int16:
		return strconv.FormatInt(int64(n), 10)
	case int32:
		return strconv.FormatInt(int64(n), 10)
	case int64:
		return strconv.FormatInt(n, 10)
	case uint8:
		return strconv.FormatUint(uint64(n), 10)
	case uint16:
		return strconv.FormatUint(uint64(n), 10)
	case uint32:
		return strconv.FormatUint(uint64(n), 10)
	case uint64:
		return strconv.FormatUint(n, 10)
	case string:
		return n
	case bool:
		if n {
			return "True"
		}
		return "False"
	default:
		return fmt.Sprint(n)
	}
}

func writeBytes(w *bufio.ReadWriter, p []byte) error {
	if _, err := w.Write(p); err != nil {
		return err
	}
	return w.Flush()
}
