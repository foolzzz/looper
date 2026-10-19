package db

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestSaveChatMessageNilStore(t *testing.T) {
	var store *Store
	err := store.SaveChatMessage(context.Background(), "s", "m", "user", "hi")
	if err == nil || !strings.Contains(err.Error(), "unreachable") {
		t.Fatalf("err %v", err)
	}
}

func TestSaveChatMessageRoundTrip(t *testing.T) {
	dsn := withDisposableDB(t, "loopgo_chat_save")
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	if err := EnsureSchema(ctx, dsn); err != nil {
		t.Fatal(err)
	}
	sqlDB, err := Open(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { sqlDB.Close() })
	store := NewStore(sqlDB)

	created := time.Date(2026, 1, 2, 3, 4, 5, 123000, time.UTC)
	failure := "send_failed"
	reason := "timeout"
	if err := store.SaveChatRecord(ctx, ChatRecord{
		SessionID: "sess-1", MessageID: "m1", Role: "user", Content: "old",
		Channel: "telegram", CreatedAt: created,
		DeliveryFailure: &failure, DeliveryError: &reason,
	}); err != nil {
		t.Fatal(err)
	}
	rows, err := store.ListChatMessages(ctx, "sess-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("rows %d", len(rows))
	}
	if rows[0]["role"] != "user" || rows[0]["content"] != "old" || rows[0]["channel"] != "telegram" {
		t.Fatalf("insert %#v", rows[0])
	}
	if rows[0]["delivery_failure"] != "send_failed" || rows[0]["delivery_error"] != "timeout" {
		t.Fatalf("delivery %#v", rows[0])
	}
	if rows[0]["created_at"] != "2026-01-02T03:04:05.000123+00:00" {
		t.Fatalf("created %v", rows[0]["created_at"])
	}

	if err := store.SaveChatMessage(ctx, "sess-1", "m1", "assistant", "new 你好"); err != nil {
		t.Fatal(err)
	}
	rows, err = store.ListChatMessages(ctx, "sess-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("rows %d", len(rows))
	}
	row := rows[0]
	if row["role"] != "user" || row["content"] != "new 你好" || row["channel"] != "telegram" {
		t.Fatalf("conflict %#v", row)
	}
	if row["delivery_failure"] != nil || row["delivery_error"] != nil {
		t.Fatalf("delivery not cleared %#v", row)
	}
	if row["created_at"] != "2026-01-02T03:04:05.000123+00:00" {
		t.Fatalf("created changed %v", row["created_at"])
	}

	bad := "nope"
	err = store.SaveChatRecord(ctx, ChatRecord{
		SessionID: "sess-1", MessageID: "m2", Role: "user", Content: "x",
		Channel: "web", DeliveryFailure: &bad,
	})
	if err == nil || !strings.Contains(err.Error(), "save chat message") {
		t.Fatalf("check %v", err)
	}
	rows, err = store.ListChatMessages(ctx, "sess-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("bad row was stored, rows %d", len(rows))
	}

	gone, stop := context.WithCancel(ctx)
	stop()
	if err := store.SaveChatMessage(gone, "sess-1", "m3", "user", "nope"); err == nil {
		t.Fatal("cancelled save returned nil")
	}
	rows, err = store.ListChatMessages(ctx, "sess-1")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0]["message_id"] != "m1" {
		t.Fatalf("after cancel %#v", rows)
	}
}
