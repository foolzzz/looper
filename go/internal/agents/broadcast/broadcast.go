// Package broadcast fans events out to in-process subscribers.
// Publishing does not block. A full subscriber buffer drops that event.
package broadcast

import (
	"context"
	"log/slog"
	"sync"
	"time"
)

// bufferSize is the subscriber queue. A slower reader loses events.
const bufferSize = 256

// note is one queued value. end tells the reader the stream is finished.
type note struct {
	event any
	end   bool
}

// Sub is one subscriber queue. It is not safe to close.
type Sub struct {
	ch chan note
}

// Broadcaster is a set of subscriber queues keyed by id.
type Broadcaster struct {
	mu   sync.Mutex
	subs map[string][]*Sub
}

// New returns an empty broadcaster.
func New() *Broadcaster {
	return &Broadcaster{subs: map[string][]*Sub{}}
}

// Subscribe adds a subscriber. The same id may have many.
// Order is the order of Subscribe calls.
func (b *Broadcaster) Subscribe(id string) *Sub {
	sub := &Sub{ch: make(chan note, bufferSize)}
	b.mu.Lock()
	b.subs[id] = append(b.subs[id], sub)
	b.mu.Unlock()
	return sub
}

// Unsubscribe removes sub. The last subscriber drops the key.
func (b *Broadcaster) Unsubscribe(id string, sub *Sub) {
	if sub == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	list := b.subs[id]
	for i, cur := range list {
		if cur != sub {
			continue
		}
		b.subs[id] = append(list[:i], list[i+1:]...)
		break
	}
	if len(b.subs[id]) == 0 {
		delete(b.subs, id)
	}
}

// Count is the number of subscribers for id.
func (b *Broadcaster) Count(id string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.subs[id])
}

// Publish sends event to each current subscriber.
// A subscriber whose buffer is full loses this event. Publish does not wait.
func (b *Broadcaster) Publish(id string, event any) {
	b.mu.Lock()
	list := append([]*Sub(nil), b.subs[id]...)
	b.mu.Unlock()
	if len(list) == 0 {
		return
	}
	for _, sub := range list {
		select {
		case sub.ch <- note{event: event}:
		default:
			slog.Debug("dropping event for slow subscriber", "task_id", id)
		}
	}
}

// Close wakes every subscriber for id with an end mark and drops the key.
// A full buffer loses the mark; that subscriber is still removed.
func (b *Broadcaster) Close(id string) {
	b.mu.Lock()
	list := b.subs[id]
	delete(b.subs, id)
	b.mu.Unlock()
	for _, sub := range list {
		select {
		case sub.ch <- note{end: true}:
		default:
		}
	}
}

// Next waits for one event. end is true when Close has marked this subscriber.
// The boolean ok is false when ctxDone is closed. A nil ctxDone only waits
// for an event.
func (s *Sub) Next(ctxDone <-chan struct{}) (event any, end bool, ok bool) {
	if s == nil {
		return nil, true, true
	}
	select {
	case <-ctxDone:
		return nil, false, false
	case n := <-s.ch:
		return n.event, n.end, true
	}
}

// Wait reads one event, an end mark, or a timeout.
// ok is false when ctx is done. timedOut is set when timeout elapses first.
func (s *Sub) Wait(ctx context.Context, timeout time.Duration) (event any, end, timedOut, ok bool) {
	if s == nil {
		return nil, true, false, true
	}
	if ctx == nil {
		ctx = context.Background()
	}
	timer := time.NewTimer(timeout)
	defer stopTimer(timer)
	select {
	case <-ctx.Done():
		return nil, false, false, false
	case <-timer.C:
		return nil, false, true, true
	case n := <-s.ch:
		return n.event, n.end, false, true
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
