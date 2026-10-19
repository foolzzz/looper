// Package web is the in-process web chat: one SSE queue per message, and a
// session fan-out for replies that are not on that message's stream.
package web

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/foolzzz/looper/go/internal/agents/broadcast"
	"github.com/foolzzz/looper/go/internal/db"
	"github.com/foolzzz/looper/go/internal/pyjson"
)

const (
	// AgentReply, ClarificationRequest, and StatusUpdate are the message
	// types deliver turns into answer and phase events. Any other type is a
	// system event whose type is that string.
	AgentReply           = "agent_reply"
	ClarificationRequest = "clarification_request"
	StatusUpdate         = "status_update"

	// AwaitingMax is how many messages wait for an answer to be saved.
	// The oldest is forgotten past it.
	AwaitingMax = 1000

	sseQueueSize         = 1000
	defaultStreamTimeout = 300 * time.Second
	defaultHeartbeat     = 15 * time.Second
	cidPoll              = time.Second
	saveTimeout          = 30 * time.Second
)

// errNoDatabase is a deliver with no saver. The chat event is still published.
var errNoDatabase = errors.New("no database")

// Envelope is the part of a bus message deliver and the SSE conversion use.
// PayloadKeys is the insertion order of Payload for a system event. An empty
// list sorts the keys. The "type" member stays first either way.
type Envelope struct {
	MessageID     string
	CorrelationID string
	Timestamp     string
	MessageType   string
	Payload       map[string]any
	PayloadKeys   []string
}

// Event is one SSE event. Payload is a pyjson.Object when the field order
// matters. A nil Payload is an empty object.
type Event struct {
	Kind    string
	Payload any
}

// Receipt is the result of Deliver.
type Receipt struct {
	Channel string
	Success bool
	Error   string
}

// Saver stores one chat row. The HTTP server passes the database store.
type Saver interface {
	SaveChatMessage(ctx context.Context, sessionID, messageID, role, content string) error
}

// ChatSub is one open chat page.
type ChatSub struct {
	sub *broadcast.Sub
}

type awaiting struct {
	corr    string
	session string
	message string
}

// Adapter holds the message streams and the open chat pages.
type Adapter struct {
	mu       sync.Mutex
	queues   map[string]chan Event
	awaiting []awaiting
	awaitIdx map[string]int
	chats    *broadcast.Broadcaster
	saver    Saver
}

// New returns an adapter with no streams and no saver.
func New() *Adapter {
	return &Adapter{
		queues:   map[string]chan Event{},
		awaitIdx: map[string]int{},
		chats:    broadcast.New(),
	}
}

// SetSaver replaces the store used when a reply is added to a chat.
// A nil saver still publishes the chat event and reports the save failure.
func (a *Adapter) SetSaver(saver Saver) {
	if a == nil {
		return
	}
	a.mu.Lock()
	a.saver = saver
	a.mu.Unlock()
}

// ReplyMessageID is the chat id of the answer to messageID.
func ReplyMessageID(messageID string) string { return messageID + ":reply" }

// Track opens the message stream for correlationID.
// A second call replaces the queue. The previous queue is left unread.
// sessionID and messageID are both required to remember where the answer
// is saved. Remembering an id that is already waiting updates it in place.
func (a *Adapter) Track(correlationID, sessionID, messageID string) {
	if a == nil || correlationID == "" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.ensureLocked()
	a.queues[correlationID] = make(chan Event, sseQueueSize)
	if sessionID != "" && messageID != "" {
		a.rememberLocked(correlationID, sessionID, messageID)
	}
}

// CloseSSE drops the message stream. The answer can still be saved.
func (a *Adapter) CloseSSE(correlationID string) {
	if a == nil {
		return
	}
	a.mu.Lock()
	delete(a.queues, correlationID)
	a.mu.Unlock()
}

// HasStream reports whether correlationID has a message stream.
func (a *Adapter) HasStream(correlationID string) bool {
	if a == nil {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	_, ok := a.queues[correlationID]
	return ok
}

// ActiveStreams is the number of message streams.
func (a *Adapter) ActiveStreams() int {
	if a == nil {
		return 0
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.queues)
}

// AwaitingCount is how many messages are waiting for an answer to be saved.
func (a *Adapter) AwaitingCount() int {
	if a == nil {
		return 0
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.awaiting)
}

// Deliver saves an answer and, when a message stream is open, enqueues its
// SSE event. A reply that names a chat session is saved and published on
// that session and is not put on a message stream.
func (a *Adapter) Deliver(ctx context.Context, env Envelope) Receipt {
	if a == nil {
		return Receipt{
			Channel: "web",
			Success: false,
			Error:   "no active SSE stream for correlation " + env.CorrelationID,
		}
	}
	if env.MessageType == AgentReply {
		if session, ok := chatSessionID(env.Payload); ok {
			return a.addToChat(ctx, session, env, "")
		}
	}

	var saved *Receipt
	if env.MessageType == AgentReply || env.MessageType == ClarificationRequest {
		a.mu.Lock()
		session, message, ok := a.popAwaitingLocked(env.CorrelationID)
		a.mu.Unlock()
		if ok {
			receipt := a.addToChat(ctx, session, env, ReplyMessageID(message))
			saved = &receipt
		}
	}

	a.mu.Lock()
	q := a.queues[env.CorrelationID]
	a.mu.Unlock()
	if q == nil {
		if saved != nil {
			return *saved
		}
		return Receipt{
			Channel: "web",
			Success: false,
			Error:   "no active SSE stream for correlation " + env.CorrelationID,
		}
	}
	select {
	case q <- envelopeToSSE(env):
		return Receipt{Channel: "web", Success: true}
	default:
		return Receipt{Channel: "web", Success: false, Error: "SSE queue full"}
	}
}

// OpenChat subscribes to sessionID. Subscribe happens at this call, so a
// reply published after it is queued. CloseChat releases it.
func (a *Adapter) OpenChat(sessionID string) *ChatSub {
	if a == nil {
		return &ChatSub{}
	}
	a.mu.Lock()
	a.ensureLocked()
	chats := a.chats
	a.mu.Unlock()
	return &ChatSub{sub: chats.Subscribe(sessionID)}
}

// CloseChat removes sub from sessionID. The last subscriber drops the key.
func (a *Adapter) CloseChat(sessionID string, sub *ChatSub) {
	if a == nil || sub == nil || sub.sub == nil {
		return
	}
	a.mu.Lock()
	chats := a.chats
	a.mu.Unlock()
	if chats != nil {
		chats.Unsubscribe(sessionID, sub.sub)
	}
}

// ChatSubscribers is how many pages have sessionID open.
func (a *Adapter) ChatSubscribers(sessionID string) int {
	if a == nil {
		return 0
	}
	a.mu.Lock()
	chats := a.chats
	a.mu.Unlock()
	if chats == nil {
		return 0
	}
	return chats.Count(sessionID)
}

// ReadChat yields events for one subscription until ctx is done, emit
// returns an error, or the subscription is closed. heartbeat is an idle
// timer: an event resets it.
func (a *Adapter) ReadChat(ctx context.Context, sub *ChatSub, heartbeat time.Duration, emit func(Event) error) error {
	if sub == nil || sub.sub == nil {
		return nil
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if heartbeat <= 0 {
		heartbeat = defaultHeartbeat
	}
	if emit == nil {
		emit = func(Event) error { return nil }
	}
	for {
		event, end, timedOut, ok := sub.sub.Wait(ctx, heartbeat)
		if !ok {
			return ctx.Err()
		}
		if end {
			return nil
		}
		ev := heartbeatEvent
		if !timedOut {
			typed, isEvent := event.(Event)
			if !isEvent {
				continue
			}
			ev = typed
		}
		if err := emit(ev); err != nil {
			return err
		}
	}
}

// RunMessageEvents reads the stream Track opened.
// A missing stream returns immediately and does not remove anything.
// timeout is the stream deadline. heartbeat is measured from the start and
// from the previous heartbeat, not from an event.
// The stream is removed when the deadline passes or a terminal event was
// emitted. A cancelled ctx or an emit error leaves the stream in place so
// the caller can close it.
func (a *Adapter) RunMessageEvents(ctx context.Context, correlationID string, timeout, heartbeat time.Duration, emit func(Event) error) error {
	if a == nil {
		return nil
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if timeout <= 0 {
		timeout = defaultStreamTimeout
	}
	if heartbeat <= 0 {
		heartbeat = defaultHeartbeat
	}
	if emit == nil {
		emit = func(Event) error { return nil }
	}
	a.mu.Lock()
	q := a.queues[correlationID]
	a.mu.Unlock()
	if q == nil {
		return nil
	}

	deadline := time.Now().Add(timeout)
	lastBeat := time.Now()
	for time.Now().Before(deadline) {
		ev, got, err := waitMessage(ctx, q, deadline, lastBeat, heartbeat)
		if err != nil {
			return err
		}
		if !got {
			if time.Since(lastBeat) >= heartbeat {
				ev = heartbeatEvent
				lastBeat = time.Now()
				got = true
			}
		}
		if !got {
			continue
		}
		if err := emit(ev); err != nil {
			return err
		}
		if terminalKind(ev.Kind) {
			break
		}
	}
	a.mu.Lock()
	delete(a.queues, correlationID)
	a.mu.Unlock()
	return nil
}

func (a *Adapter) ensureLocked() {
	if a.queues == nil {
		a.queues = map[string]chan Event{}
	}
	if a.awaitIdx == nil {
		a.awaitIdx = map[string]int{}
	}
	if a.chats == nil {
		a.chats = broadcast.New()
	}
}

func (a *Adapter) rememberLocked(corr, session, message string) {
	if i, ok := a.awaitIdx[corr]; ok {
		a.awaiting[i].session = session
		a.awaiting[i].message = message
		return
	}
	a.awaiting = append(a.awaiting, awaiting{corr: corr, session: session, message: message})
	a.awaitIdx[corr] = len(a.awaiting) - 1
	for len(a.awaiting) > AwaitingMax {
		a.dropOldestLocked()
	}
}

func (a *Adapter) dropOldestLocked() {
	if len(a.awaiting) == 0 {
		return
	}
	delete(a.awaitIdx, a.awaiting[0].corr)
	a.awaiting = a.awaiting[1:]
	for i := range a.awaiting {
		a.awaitIdx[a.awaiting[i].corr] = i
	}
}

func (a *Adapter) popAwaitingLocked(corr string) (string, string, bool) {
	i, ok := a.awaitIdx[corr]
	if !ok {
		return "", "", false
	}
	item := a.awaiting[i]
	a.awaiting = append(a.awaiting[:i], a.awaiting[i+1:]...)
	delete(a.awaitIdx, corr)
	for j := i; j < len(a.awaiting); j++ {
		a.awaitIdx[a.awaiting[j].corr] = j
	}
	return item.session, item.message, true
}

func (a *Adapter) addToChat(ctx context.Context, sessionID string, env Envelope, messageID string) Receipt {
	text := chatText(env.Payload)
	if messageID == "" {
		messageID = env.MessageID
	}
	saveCtx := ctx
	if saveCtx == nil {
		saveCtx = context.Background()
	}
	if _, ok := saveCtx.Deadline(); !ok {
		var cancel context.CancelFunc
		saveCtx, cancel = context.WithTimeout(saveCtx, saveTimeout)
		defer cancel()
	}
	a.mu.Lock()
	saver := a.saver
	a.ensureLocked()
	chats := a.chats
	a.mu.Unlock()

	receipt := Receipt{Channel: "web", Success: true}
	var err error
	if saver == nil {
		err = errNoDatabase
	} else {
		err = saver.SaveChatMessage(saveCtx, sessionID, messageID, "assistant", text)
	}
	if err != nil {
		slog.Warn("web: could not add a reply to chat", "session_id", sessionID, "err", db.LogText(err))
		receipt = Receipt{Channel: "web", Success: false, Error: "could not save to chat: " + err.Error()}
	}
	chats.Publish(sessionID, Event{
		Kind: "chat.message",
		Payload: pyjson.Object{
			{Key: "message_id", Value: messageID},
			{Key: "role", Value: "assistant"},
			{Key: "content", Value: text},
			{Key: "created_at", Value: env.Timestamp},
		},
	})
	return receipt
}

func waitMessage(ctx context.Context, q <-chan Event, deadline, lastBeat time.Time, heartbeat time.Duration) (Event, bool, error) {
	select {
	case <-ctx.Done():
		return Event{}, false, ctx.Err()
	case ev := <-q:
		return ev, true, nil
	default:
	}
	wait := time.Until(deadline)
	if wait > cidPoll {
		wait = cidPoll
	}
	if untilBeat := heartbeat - time.Since(lastBeat); untilBeat < wait {
		wait = untilBeat
	}
	if wait < 0 {
		wait = 0
	}
	timer := time.NewTimer(wait)
	defer stopTimer(timer)
	select {
	case <-ctx.Done():
		return Event{}, false, ctx.Err()
	case ev := <-q:
		return ev, true, nil
	case <-timer.C:
		select {
		case ev := <-q:
			return ev, true, nil
		default:
			return Event{}, false, nil
		}
	}
}

func stopTimer(timer *time.Timer) {
	if timer.Stop() {
		return
	}
	select {
	case <-timer.C:
	default:
	}
}

func terminalKind(kind string) bool {
	switch kind {
	case "answer.final", "turn.completed", "turn.failed":
		return true
	default:
		return false
	}
}

func chatSessionID(payload map[string]any) (string, bool) {
	v, ok := payload["chat_session_id"]
	if !ok {
		return "", false
	}
	session, ok := v.(string)
	return session, ok && session != ""
}

func chatText(payload map[string]any) string {
	v, ok := payload["text"]
	if !ok || !pythonTruthy(v) {
		return ""
	}
	return pythonStr(v)
}

func payloadGet(payload map[string]any, key string, missing any) any {
	v, ok := payload[key]
	if !ok {
		return missing
	}
	return v
}

func pythonTruthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case string:
		return t != ""
	case bool:
		return t
	case float64:
		return t != 0
	case float32:
		return t != 0
	case int:
		return t != 0
	case int32:
		return t != 0
	case int64:
		return t != 0
	case []any:
		return len(t) > 0
	case map[string]any:
		return len(t) > 0
	default:
		return true
	}
}

func pythonStr(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	case int:
		return strconv.Itoa(t)
	case int32:
		return strconv.FormatInt(int64(t), 10)
	case int64:
		return strconv.FormatInt(t, 10)
	case float32:
		return pythonStr(float64(t))
	case float64:
		if !math.IsNaN(t) && !math.IsInf(t, 0) && t == math.Trunc(t) && t >= math.MinInt64 && t <= math.MaxInt64 {
			return strconv.FormatInt(int64(t), 10)
		}
		return strconv.FormatFloat(t, 'f', -1, 64)
	default:
		return fmt.Sprint(t)
	}
}

var heartbeatEvent = Event{Kind: "heartbeat", Payload: pyjson.Object{}}

func envelopeToSSE(env Envelope) Event {
	text := payloadGet(env.Payload, "text", "")
	switch env.MessageType {
	case AgentReply, ClarificationRequest:
		return Event{
			Kind: "answer.final",
			Payload: pyjson.Object{
				{Key: "text", Value: jsonSafe(text)},
				{Key: "response", Value: pyjson.Object{
					{Key: "message", Value: jsonSafe(text)},
				}},
			},
		}
	case StatusUpdate:
		if pythonTruthy(payloadGet(env.Payload, "delta", nil)) {
			return Event{Kind: "answer.delta", Payload: pyjson.Object{
				{Key: "text", Value: jsonSafe(text)},
			}}
		}
		return Event{Kind: "agent.phase", Payload: pyjson.Object{
			{Key: "label", Value: jsonSafe(text)},
		}}
	default:
		return Event{Kind: "system.event", Payload: systemPayload(env)}
	}
}

func systemPayload(env Envelope) pyjson.Object {
	obj := pyjson.Object{{Key: "type", Value: env.MessageType}}
	keys := env.PayloadKeys
	if keys == nil {
		keys = make([]string, 0, len(env.Payload))
		for key := range env.Payload {
			keys = append(keys, key)
		}
		sort.Strings(keys)
	}
	for _, key := range keys {
		value, ok := env.Payload[key]
		if !ok {
			continue
		}
		if key == "type" {
			obj[0].Value = jsonSafe(value)
			continue
		}
		obj = append(obj, pyjson.Field{Key: key, Value: jsonSafe(value)})
	}
	return obj
}

func jsonSafe(v any) any {
	switch t := v.(type) {
	case nil, string, bool, float64, float32, int, int32, int64, json.Number, pyjson.Object:
		return v
	case map[string]any:
		keys := make([]string, 0, len(t))
		for key := range t {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		obj := make(pyjson.Object, 0, len(keys))
		for _, key := range keys {
			obj = append(obj, pyjson.Field{Key: key, Value: jsonSafe(t[key])})
		}
		return obj
	case []any:
		out := make([]any, len(t))
		for i, item := range t {
			out[i] = jsonSafe(item)
		}
		return out
	default:
		return pythonStr(v)
	}
}

// Format is one SSE frame: the event line, the data line, and a blank line.
// Kind defaults to message. A nil payload is {}.
func Format(ev Event) ([]byte, error) {
	kind := ev.Kind
	if kind == "" {
		kind = "message"
	}
	payload := ev.Payload
	if payload == nil {
		payload = pyjson.Object{}
	}
	body, err := pyjson.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("sse frame: %w", err)
	}
	frame := make([]byte, 0, len(kind)+len(body)+16)
	frame = append(frame, "event: "...)
	frame = append(frame, kind...)
	frame = append(frame, "\ndata: "...)
	frame = append(frame, body...)
	frame = append(frame, "\n\n"...)
	return frame, nil
}
