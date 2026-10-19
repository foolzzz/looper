package web

import (
	"context"
	"errors"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/foolzzz/looper/go/internal/pyjson"
)

type savedRow struct {
	session string
	message string
	role    string
	content string
}

type memSaver struct {
	mu          sync.Mutex
	rows        []savedRow
	err         error
	hasDeadline bool
	deadline    time.Duration
}

func (m *memSaver) SaveChatMessage(ctx context.Context, sessionID, messageID, role, content string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if deadline, ok := ctx.Deadline(); ok {
		m.hasDeadline = true
		m.deadline = time.Until(deadline)
	}
	if m.err != nil {
		return m.err
	}
	m.rows = append(m.rows, savedRow{sessionID, messageID, role, content})
	return nil
}

func (m *memSaver) calls() []savedRow {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]savedRow, len(m.rows))
	copy(out, m.rows)
	return out
}

func TestFormatMatchesPythonFrame(t *testing.T) {
	frame, err := Format(Event{Kind: "heartbeat", Payload: pyjson.Object{}})
	if err != nil {
		t.Fatal(err)
	}
	const heartbeat = "event: heartbeat\ndata: {}\n\n"
	if string(frame) != heartbeat {
		t.Fatalf("heartbeat %q", frame)
	}
	frame, err = Format(Event{Payload: pyjson.Object{}})
	if err != nil || !strings.HasPrefix(string(frame), "event: message\n") {
		t.Fatalf("default kind %q %v", frame, err)
	}

	text := "你好 \"quote\"\nnext"
	final := envelopeToSSE(Envelope{
		MessageType: AgentReply,
		Payload:     map[string]any{"text": text},
	})
	frame, err = Format(final)
	if err != nil {
		t.Fatal(err)
	}
	const wantFinal = "event: answer.final\ndata: {\"text\": \"你好 \\\"quote\\\"\\nnext\", \"response\": {\"message\": \"你好 \\\"quote\\\"\\nnext\"}}\n\n"
	if string(frame) != wantFinal {
		t.Fatalf("final %q", frame)
	}
	missing := envelopeToSSE(Envelope{MessageType: AgentReply, Payload: map[string]any{}})
	nullText := envelopeToSSE(Envelope{MessageType: AgentReply, Payload: map[string]any{"text": nil}})
	missingFrame, err := Format(missing)
	if err != nil {
		t.Fatal(err)
	}
	nullFrame, err := Format(nullText)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(missingFrame), `"text": ""`) || !strings.Contains(string(nullFrame), `"text": null`) {
		t.Fatalf("missing %s null %s", missingFrame, nullFrame)
	}
}

func TestEnvelopeKinds(t *testing.T) {
	text := "hello user"
	cases := []struct {
		name    string
		msgType string
		payload map[string]any
		kind    string
		snippet string
	}{
		{name: "reply", msgType: AgentReply, payload: map[string]any{"text": text}, kind: "answer.final", snippet: `"text": "hello user"`},
		{name: "clarify", msgType: ClarificationRequest, payload: map[string]any{"text": text}, kind: "answer.final", snippet: `"response": {"message": "hello user"}`},
		{name: "phase", msgType: StatusUpdate, payload: map[string]any{"text": "thinking..."}, kind: "agent.phase", snippet: `"label": "thinking..."`},
		{name: "delta true", msgType: StatusUpdate, payload: map[string]any{"text": "word", "delta": true}, kind: "answer.delta", snippet: `"text": "word"`},
		{name: "delta one", msgType: StatusUpdate, payload: map[string]any{"text": "word", "delta": float64(1)}, kind: "answer.delta", snippet: `"text": "word"`},
		{name: "delta yes", msgType: StatusUpdate, payload: map[string]any{"text": "word", "delta": "yes"}, kind: "answer.delta", snippet: `"text": "word"`},
		{name: "delta false", msgType: StatusUpdate, payload: map[string]any{"text": "word", "delta": false}, kind: "agent.phase", snippet: `"label": "word"`},
		{name: "delta zero", msgType: StatusUpdate, payload: map[string]any{"text": "word", "delta": float64(0)}, kind: "agent.phase", snippet: `"label": "word"`},
		{name: "delta empty", msgType: StatusUpdate, payload: map[string]any{"text": "word", "delta": ""}, kind: "agent.phase", snippet: `"label": "word"`},
		{name: "delta null", msgType: StatusUpdate, payload: map[string]any{"text": "word", "delta": nil}, kind: "agent.phase", snippet: `"label": "word"`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev := envelopeToSSE(Envelope{MessageType: tc.msgType, Payload: tc.payload})
			if ev.Kind != tc.kind {
				t.Fatalf("kind %s", ev.Kind)
			}
			frame, err := Format(ev)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(string(frame), tc.snippet) {
				t.Fatalf("frame %s", frame)
			}
		})
	}

	ev := envelopeToSSE(Envelope{
		MessageType: "user_message",
		Payload:     map[string]any{"text": "hi", "type": "custom"},
		PayloadKeys: []string{"text", "type"},
	})
	frame, err := Format(ev)
	if err != nil {
		t.Fatal(err)
	}
	const want = "event: system.event\ndata: {\"type\": \"custom\", \"text\": \"hi\"}\n\n"
	if string(frame) != want {
		t.Fatalf("system %q", frame)
	}
}

func TestDeliverSavesAndStreams(t *testing.T) {
	t.Run("unknown stream", func(t *testing.T) {
		a := New()
		if err := a.RunMessageEvents(context.Background(), "missing", time.Second, time.Second, nil); err != nil {
			t.Fatal(err)
		}
		if a.ActiveStreams() != 0 {
			t.Fatalf("streams %d", a.ActiveStreams())
		}
		receipt := a.Deliver(context.Background(), Envelope{CorrelationID: "missing", MessageType: AgentReply})
		if receipt.Success || receipt.Error != "no active SSE stream for correlation missing" {
			t.Fatalf("receipt %+v", receipt)
		}
	})

	t.Run("delta then final pops the stream", func(t *testing.T) {
		a := New()
		a.Track("c", "", "")
		a.Deliver(context.Background(), Envelope{
			CorrelationID: "c", MessageType: StatusUpdate,
			Payload: map[string]any{"text": "word", "delta": true},
		})
		a.Deliver(context.Background(), Envelope{
			CorrelationID: "c", MessageType: AgentReply,
			Payload: map[string]any{"text": "done"},
		})
		var kinds []string
		err := a.RunMessageEvents(context.Background(), "c", time.Second, time.Minute, func(ev Event) error {
			kinds = append(kinds, ev.Kind)
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
		if strings.Join(kinds, ",") != "answer.delta,answer.final" {
			t.Fatalf("kinds %v", kinds)
		}
		if a.HasStream("c") {
			t.Fatal("stream still open")
		}
		if err := a.RunMessageEvents(context.Background(), "c", time.Second, time.Second, func(Event) error {
			t.Fatal("unknown stream yielded")
			return nil
		}); err != nil {
			t.Fatal(err)
		}
	})

	t.Run("terminal kinds end the stream", func(t *testing.T) {
		for _, kind := range []string{"turn.completed", "turn.failed"} {
			a := New()
			a.Track("c", "", "")
			a.queues["c"] <- Event{Kind: kind, Payload: pyjson.Object{}}
			var got string
			if err := a.RunMessageEvents(context.Background(), "c", time.Second, time.Minute, func(ev Event) error {
				got = ev.Kind
				return nil
			}); err != nil {
				t.Fatal(err)
			}
			if got != kind || a.HasStream("c") {
				t.Fatalf("%s still %v", kind, a.HasStream("c"))
			}
		}
	})

	t.Run("answer is saved once under the reply id", func(t *testing.T) {
		for _, left := range []bool{false, true} {
			a := New()
			saver := &memSaver{}
			a.SetSaver(saver)
			sub := a.OpenChat("chat-1")
			a.Track("c", "chat-1", "m1")
			if left {
				a.CloseSSE("c")
			}
			answer := Envelope{
				CorrelationID: "c", MessageType: AgentReply,
				Timestamp: "2026-01-02T03:04:05+00:00",
				Payload:   map[string]any{"text": "a group of tasks"},
			}
			receipt := a.Deliver(context.Background(), answer)
			if !receipt.Success {
				t.Fatalf("left %v receipt %+v", left, receipt)
			}
			rows := saver.calls()
			if len(rows) != 1 || rows[0].message != "m1:reply" || rows[0].role != "assistant" || rows[0].content != "a group of tasks" {
				t.Fatalf("saved %+v", rows)
			}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			ev, end, timedOut, ok := sub.sub.Wait(ctx, time.Second)
			cancel()
			if !ok || end || timedOut {
				t.Fatalf("chat event end %v timeout %v ok %v", end, timedOut, ok)
			}
			chat, _ := ev.(Event)
			if chat.Kind != "chat.message" {
				t.Fatalf("kind %s", chat.Kind)
			}
			frame, err := Format(chat)
			if err != nil {
				t.Fatal(err)
			}
			const want = "event: chat.message\ndata: {\"message_id\": \"m1:reply\", \"role\": \"assistant\", \"content\": \"a group of tasks\", \"created_at\": \"2026-01-02T03:04:05+00:00\"}\n\n"
			if string(frame) != want {
				t.Fatalf("frame %q", frame)
			}
			if !left {
				if !a.HasStream("c") {
					t.Fatal("stream was closed")
				}
			}
			again := a.Deliver(context.Background(), answer)
			if len(saver.calls()) != 1 {
				t.Fatalf("saved again %+v", saver.calls())
			}
			if left {
				if again.Success || again.Error != "no active SSE stream for correlation c" {
					t.Fatalf("second receipt %+v", again)
				}
			}
			a.CloseChat("chat-1", sub)
			if a.ChatSubscribers("chat-1") != 0 {
				t.Fatal("subscriber left")
			}
		}
	})

	t.Run("session without a message id is not saved", func(t *testing.T) {
		a := New()
		saver := &memSaver{}
		a.SetSaver(saver)
		a.Track("c", "chat-1", "")
		receipt := a.Deliver(context.Background(), Envelope{
			CorrelationID: "c", MessageType: AgentReply, Payload: map[string]any{"text": "hello"},
		})
		if !receipt.Success || len(saver.calls()) != 0 {
			t.Fatalf("receipt %+v saved %d", receipt, len(saver.calls()))
		}
	})

	t.Run("clarification uses the reply id", func(t *testing.T) {
		a := New()
		saver := &memSaver{}
		a.SetSaver(saver)
		a.Track("c", "chat-1", "m1")
		a.CloseSSE("c")
		receipt := a.Deliver(context.Background(), Envelope{
			CorrelationID: "c", MessageType: ClarificationRequest, Payload: map[string]any{"text": "which one?"},
		})
		if !receipt.Success {
			t.Fatal(receipt.Error)
		}
		rows := saver.calls()
		if len(rows) != 1 || rows[0].message != "m1:reply" || rows[0].content != "which one?" {
			t.Fatalf("%+v", rows)
		}
	})

	t.Run("autonomous reply skips the message stream", func(t *testing.T) {
		a := New()
		saver := &memSaver{}
		a.SetSaver(saver)
		a.Track("c", "", "")
		watched := a.OpenChat("chat-1")
		other := a.OpenChat("chat-2")
		receipt := a.Deliver(context.Background(), Envelope{
			MessageID: "bg-1", CorrelationID: "c", Timestamp: "ts",
			MessageType: AgentReply,
			Payload:     map[string]any{"text": "the background job is done", "chat_session_id": "chat-1"},
		})
		if !receipt.Success {
			t.Fatal(receipt.Error)
		}
		rows := saver.calls()
		if len(rows) != 1 || rows[0].message != "bg-1" || rows[0].session != "chat-1" {
			t.Fatalf("%+v", rows)
		}
		var kinds []string
		err := a.RunMessageEvents(context.Background(), "c", 40*time.Millisecond, time.Minute, func(ev Event) error {
			kinds = append(kinds, ev.Kind)
			return nil
		})
		if err != nil || len(kinds) != 0 {
			t.Fatalf("stream %v %v", err, kinds)
		}
		ev, _, _, ok := watched.sub.Wait(context.Background(), time.Second)
		if !ok {
			t.Fatal("missing chat event")
		}
		if chat, _ := ev.(Event); chat.Kind != "chat.message" {
			t.Fatalf("kind %v", ev)
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_, _, timedOut, ok := other.sub.Wait(ctx, 30*time.Millisecond)
		if !ok || !timedOut {
			t.Fatal("other chat should be idle")
		}
		a.CloseChat("chat-1", watched)
		a.CloseChat("chat-2", other)
		if a.ChatSubscribers("chat-1") != 0 || a.ChatSubscribers("chat-2") != 0 {
			t.Fatal("subscribers remain")
		}
	})

	t.Run("non string chat session does not take the autonomous path", func(t *testing.T) {
		a := New()
		saver := &memSaver{}
		a.SetSaver(saver)
		receipt := a.Deliver(context.Background(), Envelope{
			MessageID: "m", MessageType: AgentReply,
			Payload: map[string]any{"text": "x", "chat_session_id": true},
		})
		if receipt.Success || len(saver.calls()) != 0 {
			t.Fatalf("receipt %+v saved %d", receipt, len(saver.calls()))
		}
	})

	t.Run("save failure still publishes", func(t *testing.T) {
		a := New()
		saver := &memSaver{err: errors.New("disk down")}
		a.SetSaver(saver)
		sub := a.OpenChat("chat-1")
		a.Track("c", "chat-1", "m1")
		queued := a.Deliver(context.Background(), Envelope{
			CorrelationID: "c", MessageType: AgentReply, Payload: map[string]any{"text": "hi"},
		})
		if !queued.Success {
			t.Fatalf("queued receipt %+v", queued)
		}
		a.CloseSSE("c")
		failed := a.Deliver(context.Background(), Envelope{
			CorrelationID: "other", MessageType: AgentReply,
			Payload: map[string]any{"text": "later", "chat_session_id": "chat-1"},
		})
		// The first deliver popped the awaiting entry. This one is autonomous.
		if failed.Success || !strings.Contains(failed.Error, "could not save to chat: disk down") {
			t.Fatalf("receipt %+v", failed)
		}
		ev, _, _, ok := sub.sub.Wait(context.Background(), time.Second)
		if !ok {
			t.Fatal("no event after a failed save")
		}
		if chat, _ := ev.(Event); chat.Kind != "chat.message" {
			t.Fatalf("kind %v", ev)
		}
		a.CloseChat("chat-1", sub)
	})

	t.Run("nil saver", func(t *testing.T) {
		a := New()
		sub := a.OpenChat("chat-1")
		receipt := a.Deliver(context.Background(), Envelope{
			MessageID: "m", MessageType: AgentReply,
			Payload: map[string]any{"text": "hi", "chat_session_id": "chat-1"},
		})
		if receipt.Success || !strings.Contains(receipt.Error, "no database") {
			t.Fatalf("%+v", receipt)
		}
		if _, _, _, ok := sub.sub.Wait(context.Background(), time.Second); !ok {
			t.Fatal("unpublished")
		}
		a.CloseChat("chat-1", sub)
	})

	t.Run("queue full", func(t *testing.T) {
		a := New()
		a.Track("c", "", "")
		for i := 0; i < sseQueueSize; i++ {
			receipt := a.Deliver(context.Background(), Envelope{
				CorrelationID: "c", MessageType: StatusUpdate, Payload: map[string]any{"text": "x"},
			})
			if !receipt.Success {
				t.Fatalf("event %d %+v", i, receipt)
			}
		}
		receipt := a.Deliver(context.Background(), Envelope{
			CorrelationID: "c", MessageType: StatusUpdate, Payload: map[string]any{"text": "x"},
		})
		if receipt.Success || receipt.Error != "SSE queue full" {
			t.Fatalf("%+v", receipt)
		}
	})

	t.Run("second track replaces the queue", func(t *testing.T) {
		a := New()
		a.Track("c", "", "")
		a.Deliver(context.Background(), Envelope{
			CorrelationID: "c", MessageType: StatusUpdate, Payload: map[string]any{"text": "one"},
		})
		a.Track("c", "", "")
		a.Deliver(context.Background(), Envelope{
			CorrelationID: "c", MessageType: StatusUpdate, Payload: map[string]any{"text": "two", "delta": true},
		})
		if a.ActiveStreams() != 1 {
			t.Fatalf("streams %d", a.ActiveStreams())
		}
		var texts []string
		if err := a.RunMessageEvents(context.Background(), "c", time.Second, time.Minute, func(ev Event) error {
			frame, err := Format(ev)
			if err != nil {
				return err
			}
			texts = append(texts, string(frame))
			return nil
		}); err != nil {
			t.Fatal(err)
		}
		if len(texts) != 1 || !strings.Contains(texts[0], `"text": "two"`) {
			t.Fatalf("%v", texts)
		}
	})

	t.Run("awaiting evicts the oldest and an update stays put", func(t *testing.T) {
		a := New()
		saver := &memSaver{}
		a.SetSaver(saver)
		for i := 0; i < AwaitingMax; i++ {
			a.Track(id(i), "s", id(i))
		}
		a.Track(id(0), "s", "moved-if-bug")
		if a.AwaitingCount() != AwaitingMax {
			t.Fatalf("count %d", a.AwaitingCount())
		}
		a.Track(id(AwaitingMax), "s", "newest")
		if a.AwaitingCount() != AwaitingMax {
			t.Fatalf("count after evict %d", a.AwaitingCount())
		}
		a.CloseSSE(id(0))
		a.CloseSSE(id(1))
		a.Deliver(context.Background(), Envelope{CorrelationID: id(0), MessageType: AgentReply, Payload: map[string]any{"text": "old"}})
		a.Deliver(context.Background(), Envelope{CorrelationID: id(1), MessageType: AgentReply, Payload: map[string]any{"text": "kept"}})
		rows := saver.calls()
		if len(rows) != 1 || rows[0].message != id(1)+":reply" || rows[0].content != "kept" {
			t.Fatalf("%+v", rows)
		}
	})

	t.Run("falsy text is empty and true is True", func(t *testing.T) {
		a := New()
		saver := &memSaver{}
		a.SetSaver(saver)
		for i, text := range []any{nil, false, float64(0), "", true} {
			corr := id(i)
			a.Track(corr, "s", corr)
			a.CloseSSE(corr)
			a.Deliver(context.Background(), Envelope{
				CorrelationID: corr, MessageType: AgentReply, Payload: map[string]any{"text": text},
			})
		}
		rows := saver.calls()
		if len(rows) != 5 {
			t.Fatalf("%+v", rows)
		}
		for i := 0; i < 4; i++ {
			if rows[i].content != "" {
				t.Fatalf("row %d %q", i, rows[i].content)
			}
		}
		if rows[4].content != "True" {
			t.Fatalf("bool %q", rows[4].content)
		}
	})

	t.Run("save keeps the caller deadline", func(t *testing.T) {
		a := New()
		saver := &memSaver{}
		a.SetSaver(saver)
		a.Track("c", "s", "m")
		a.CloseSSE("c")
		parent, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		a.Deliver(parent, Envelope{CorrelationID: "c", MessageType: AgentReply, Payload: map[string]any{"text": "x"}})
		saver.mu.Lock()
		defer saver.mu.Unlock()
		if !saver.hasDeadline || saver.deadline > 2*time.Second || saver.deadline < time.Second {
			t.Fatalf("deadline %v has %v", saver.deadline, saver.hasDeadline)
		}
	})
}

func TestChatFanoutAndHeartbeats(t *testing.T) {
	a := New()
	saver := &memSaver{}
	a.SetSaver(saver)
	first := a.OpenChat("chat-1")
	second := a.OpenChat("chat-1")
	a.Deliver(context.Background(), Envelope{
		MessageID: "m-1", Timestamp: "ts", MessageType: AgentReply,
		Payload: map[string]any{"text": "one", "chat_session_id": "chat-1"},
	})
	for _, sub := range []*ChatSub{first, second} {
		ev, _, _, ok := sub.sub.Wait(context.Background(), time.Second)
		if !ok {
			t.Fatal("missing")
		}
		chat, _ := ev.(Event)
		frame, err := Format(chat)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(string(frame), `"message_id": "m-1"`) {
			t.Fatalf("%s", frame)
		}
	}
	a.CloseChat("chat-1", first)
	if a.ChatSubscribers("chat-1") != 1 {
		t.Fatalf("subscribers %d", a.ChatSubscribers("chat-1"))
	}
	a.Deliver(context.Background(), Envelope{
		MessageID: "m-2", MessageType: AgentReply,
		Payload: map[string]any{"text": "two", "chat_session_id": "chat-1"},
	})
	ev, _, _, ok := second.sub.Wait(context.Background(), time.Second)
	if !ok {
		t.Fatal("second missing")
	}
	if chat, _ := ev.(Event); chat.Kind != "chat.message" {
		t.Fatalf("kind %v", ev)
	}
	quiet, quietCancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	_, _, timedOut, still := first.sub.Wait(quiet, 20*time.Millisecond)
	quietCancel()
	if still && !timedOut {
		t.Fatal("closed subscriber got an event")
	}
	a.CloseChat("chat-1", second)
	if a.ChatSubscribers("chat-1") != 0 {
		t.Fatal("still subscribed")
	}

	a.Track("hb", "", "")
	ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
	defer cancel()
	beats := 0
	err := a.RunMessageEvents(ctx, "hb", time.Second, 50*time.Millisecond, func(ev Event) error {
		if ev.Kind != "heartbeat" {
			t.Errorf("kind %s", ev.Kind)
		}
		beats++
		return nil
	})
	if err == nil || beats < 2 {
		t.Fatalf("beats %d err %v", beats, err)
	}
	if !a.HasStream("hb") {
		t.Fatal("cancel removed the stream")
	}
	a.CloseSSE("hb")
}

func TestSessionIdleHeartbeatResets(t *testing.T) {
	a := New()
	saver := &memSaver{}
	a.SetSaver(saver)
	sub := a.OpenChat("chat-1")
	defer a.CloseChat("chat-1", sub)
	a.Deliver(context.Background(), Envelope{
		MessageID: "m", MessageType: AgentReply,
		Payload: map[string]any{"text": "now", "chat_session_id": "chat-1"},
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	got := make(chan Event, 4)
	go func() {
		_ = a.ReadChat(ctx, sub, 80*time.Millisecond, func(ev Event) error {
			got <- ev
			return nil
		})
	}()
	select {
	case ev := <-got:
		if ev.Kind != "chat.message" {
			t.Fatalf("first %s", ev.Kind)
		}
	case <-time.After(time.Second):
		t.Fatal("event was not waiting")
	}
	select {
	case ev := <-got:
		t.Fatalf("idle timer was not reset: %s", ev.Kind)
	case <-time.After(40 * time.Millisecond):
	}
	select {
	case ev := <-got:
		if ev.Kind != "heartbeat" {
			t.Fatalf("second %s", ev.Kind)
		}
	case <-time.After(time.Second):
		t.Fatal("no heartbeat")
	}
}

func id(n int) string {
	return "c" + strconv.Itoa(n)
}
