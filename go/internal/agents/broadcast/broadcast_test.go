package broadcast

import (
	"context"
	"testing"
	"time"
)

func TestPublishReachesSubscribersInOrder(t *testing.T) {
	b := New()
	first := b.Subscribe("task-1")
	second := b.Subscribe("task-1")
	other := b.Subscribe("task-2")
	b.Publish("task-1", "a")
	b.Publish("task-1", "b")
	if b.Count("task-1") != 2 || b.Count("task-2") != 1 {
		t.Fatalf("counts %d %d", b.Count("task-1"), b.Count("task-2"))
	}
	for _, sub := range []*Sub{first, second} {
		for _, want := range []string{"a", "b"} {
			got, end, ok := sub.Next(nil)
			if !ok || end || got != want {
				t.Fatalf("event %v end %v ok %v", got, end, ok)
			}
		}
	}
	b.Publish("missing", "nope")
	select {
	case n := <-other.ch:
		t.Fatalf("other subscriber got %v", n.event)
	default:
	}
}

func TestFullSubscriberDropsAndDoesNotBlock(t *testing.T) {
	b := New()
	sub := b.Subscribe("task-1")
	fresh := b.Subscribe("task-1")
	start := time.Now()
	for i := 0; i < bufferSize+20; i++ {
		b.Publish("task-1", i)
	}
	if time.Since(start) > time.Second {
		t.Fatal("publish blocked on a full subscriber")
	}
	n := 0
	for {
		select {
		case <-sub.ch:
			n++
		default:
			if n != bufferSize {
				t.Fatalf("buffered %d, want %d", n, bufferSize)
			}
			// The second subscriber also kept only the first buffer.
			m := 0
			for {
				select {
				case <-fresh.ch:
					m++
				default:
					if m != bufferSize {
						t.Fatalf("second buffered %d", m)
					}
					return
				}
			}
		}
	}
}

func TestNextUntil(t *testing.T) {
	t.Run("queued event beats a ready timeout", func(t *testing.T) {
		b := New()
		sub := b.Subscribe("t")
		b.Publish("t", "e")
		timeout := make(chan time.Time, 1)
		timeout <- time.Unix(0, 0)
		got, end, timedOut, ok := sub.NextUntil(context.Background(), timeout)
		if !ok || end || timedOut || got != "e" {
			t.Fatalf("event %v end %v timedOut %v ok %v", got, end, timedOut, ok)
		}
	})
	t.Run("timeout", func(t *testing.T) {
		b := New()
		sub := b.Subscribe("t")
		timeout := make(chan time.Time, 1)
		timeout <- time.Unix(0, 0)
		_, end, timedOut, ok := sub.NextUntil(context.Background(), timeout)
		if !ok || end || !timedOut {
			t.Fatalf("end %v timedOut %v ok %v", end, timedOut, ok)
		}
	})
	t.Run("canceled", func(t *testing.T) {
		b := New()
		sub := b.Subscribe("t")
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		_, end, timedOut, ok := sub.NextUntil(ctx, nil)
		if ok || end || timedOut {
			t.Fatalf("end %v timedOut %v ok %v", end, timedOut, ok)
		}
	})
	t.Run("close", func(t *testing.T) {
		b := New()
		sub := b.Subscribe("t")
		b.Close("t")
		_, end, timedOut, ok := sub.NextUntil(context.Background(), nil)
		if !ok || !end || timedOut {
			t.Fatalf("end %v timedOut %v ok %v", end, timedOut, ok)
		}
	})
}

func TestUnsubscribeAndClose(t *testing.T) {
	b := New()
	keep := b.Subscribe("task-1")
	drop := b.Subscribe("task-1")
	b.Unsubscribe("task-1", drop)
	if b.Count("task-1") != 1 {
		t.Fatalf("count %d", b.Count("task-1"))
	}
	b.Publish("task-1", "still")
	got, end, ok := keep.Next(nil)
	if !ok || end || got != "still" {
		t.Fatalf("kept %v end %v", got, end)
	}
	select {
	case n := <-drop.ch:
		t.Fatalf("removed subscriber got %v", n.event)
	default:
	}
	b.Unsubscribe("task-1", keep)
	if b.Count("task-1") != 0 {
		t.Fatalf("count after last %d", b.Count("task-1"))
	}
	b.Publish("task-1", "nobody")

	again := b.Subscribe("task-1")
	b.Close("task-1")
	if b.Count("task-1") != 0 {
		t.Fatalf("count after close %d", b.Count("task-1"))
	}
	_, end, ok = again.Next(nil)
	if !ok || !end {
		t.Fatalf("end %v ok %v", end, ok)
	}
	b.Publish("task-1", "after")
	select {
	case n := <-again.ch:
		t.Fatalf("closed subscriber got %v", n.event)
	default:
	}
}
