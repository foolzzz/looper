package db

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestStartProblem(t *testing.T) {
	emptyTelegram := "its sender allowlist is empty; list the Telegram user IDs that may talk to the Manager, which can run any command on this machine"
	emptyLark := "its sender allowlist is empty; list the Lark open_ids that may talk to the Manager, which can run any command on this machine"
	tests := []struct {
		name, channel string
		config        map[string]any
		want          any
	}{
		{name: "telegram ids", channel: "telegram", config: map[string]any{"allowed_user_ids": []any{"123456789"}}, want: nil},
		{name: "telegram empty", channel: "telegram", config: map[string]any{"allowed_user_ids": []any{}}, want: emptyTelegram},
		{name: "telegram nil config", channel: "telegram", want: emptyTelegram},
		{name: "lark empty", channel: "lark", config: map[string]any{}, want: emptyLark},
		{name: "web", channel: "web", config: map[string]any{}, want: nil},
		{
			name: "apostrophe", channel: "telegram",
			config: map[string]any{"allowed_user_ids": []any{"bad'id"}},
			want:   `its sender allowlist is invalid: "bad'id" is not a Telegram user ID; use the sender's numeric Telegram user ID, such as 123456789 (a negative number is a group or channel, not a sender)`,
		},
		{
			name: "backslash", channel: "telegram",
			config: map[string]any{"allowed_user_ids": []any{`a\b`}},
			want:   `its sender allowlist is invalid: 'a\\b' is not a Telegram user ID; use the sender's numeric Telegram user ID, such as 123456789 (a negative number is a group or channel, not a sender)`,
		},
		{
			name: "not a list", channel: "telegram",
			config: map[string]any{"allowed_user_ids": 1},
			want:   "its sender allowlist is invalid: allowed_user_ids must be a list of Telegram user IDs",
		},
		{
			name: "bool entry", channel: "lark",
			config: map[string]any{"allowed_open_ids": []any{true}},
			want:   "its sender allowlist is invalid: allowed_open_ids: each entry is a Lark open_id written as a string",
		},
		{
			name: "telegram whole float", channel: "telegram",
			config: map[string]any{"allowed_user_ids": []any{123.0}},
			want:   "its sender allowlist is invalid: allowed_user_ids: each entry is a Telegram user ID written as a string",
		},
		{
			name: "telegram json float", channel: "telegram",
			config: map[string]any{"allowed_user_ids": []any{json.Number("123.0")}},
			want:   "its sender allowlist is invalid: allowed_user_ids: each entry is a Telegram user ID written as a string",
		},
		{
			name: "telegram int64 past 2^53", channel: "telegram",
			config: map[string]any{"allowed_user_ids": []any{int64(9007199254740993)}},
			want:   nil,
		},
		{
			name: "telegram json integer past int64", channel: "telegram",
			config: map[string]any{"allowed_user_ids": []any{json.Number("9223372036854775808")}},
			want:   nil,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := startProblem(tt.channel, tt.config)
			if got != tt.want {
				t.Fatalf("got %#v\nwant %#v", got, tt.want)
			}
		})
	}
}

func TestDashboardTargetProblem(t *testing.T) {
	tests := []struct {
		name string
		chat gateChat
		want string
	}{
		{
			name: "telegram",
			chat: gateChat{channel: "telegram", kind: "chat", id: "123"},
			want: "Telegram 123 is no longer on the Telegram channel's sender allowlist: the gates of dashboard tasks go to the task page only",
		},
		{
			name: "lark user",
			chat: gateChat{channel: "lark", kind: "user", id: "ou_abcdefgh"},
			want: "Lark ou_abcdefgh is no longer on the Lark channel's sender allowlist: the gates of dashboard tasks go to the task page only",
		},
		{
			name: "lark group",
			chat: gateChat{channel: "lark", kind: "chat", id: "oc_groupid1"},
			want: "Lark group oc_groupid1 is no longer on the Lark channel's sender allowlist: the gates of dashboard tasks go to the task page only",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := dashboardTargetProblem(tt.chat); got != tt.want {
				t.Fatalf("got %q", got)
			}
		})
	}
}

func TestNotifyTargetViews(t *testing.T) {
	rows := []map[string]any{
		{
			"channel_type": "lark", "enabled": false,
			"config": map[string]any{"allowed_open_ids": []any{"ou_abcdefgh"}},
		},
		{
			"channel_type": "telegram", "enabled": true,
			"config": map[string]any{"allowed_user_ids": []any{"123456789", "bad"}},
		},
		{"channel_type": "web", "enabled": true, "config": map[string]any{}},
		{"channel_type": "telegram", "enabled": true, "config": ""},
		{"channel_type": "telegram", "enabled": true, "config": false},
		{"channel_type": "telegram", "enabled": true, "config": 0},
		{"channel_type": "telegram", "enabled": true, "config": []any{}},
		{"channel_type": "telegram", "enabled": true, "config": nil},
	}
	got, err := notifyTargetViews(rows)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("targets %#v", got)
	}
	view, ok := got[0].(map[string]any)
	if !ok {
		t.Fatalf("view %#v", got[0])
	}
	if view["channel"] != "lark" || view["chat"] != "ou_abcdefgh" || view["channel_enabled"] != false {
		t.Fatalf("view %#v", view)
	}
	label, _ := view["label"].(string)
	if label != "Lark · ou_abcdefgh" || !strings.Contains(label, "·") {
		t.Fatalf("label %q", label)
	}
}

func TestNotifyTargetViewsTruthyConfig(t *testing.T) {
	tests := []struct {
		name   string
		config any
	}{
		{name: "string", config: "not-a-map"},
		{name: "number", config: 1},
		{name: "true", config: true},
		{name: "list", config: []any{"123456789"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			rows := []map[string]any{{
				"channel_type": "telegram", "enabled": true, "config": tt.config,
			}}
			if _, err := notifyTargetViews(rows); err == nil {
				t.Fatal("truthy non-object config was an empty allowlist")
			}
		})
	}
}

func TestChannelJSONNumbersFromDatabase(t *testing.T) {
	const (
		exact = "9007199254740993"
		round = "9007199254740992"
		plain = "123"
	)
	dsn := withDisposableDB(t, "loopgo_g1efix_sender")
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
	inserts := []struct {
		id, config string
	}{
		{"tg-exact", `{"allowed_user_ids":[9007199254740993]}`},
		{"tg-float", `{"allowed_user_ids":[123.0]}`},
		{"tg-int", `{"allowed_user_ids":[123]}`},
	}
	for _, row := range inserts {
		if _, err := sqlDB.ExecContext(ctx, `INSERT INTO channel_configs (channel_id, channel_type, config) VALUES ($1, 'telegram', $2::jsonb)`, row.id, row.config); err != nil {
			t.Fatal(err)
		}
	}
	store := NewStore(sqlDB)
	rows, err := store.ListChannelConfigs(ctx)
	if err != nil {
		t.Fatal(err)
	}
	wantProblem := map[string]any{
		"tg-exact": nil,
		"tg-float": "its sender allowlist is invalid: allowed_user_ids: each entry is a Telegram user ID written as a string",
		"tg-int":   nil,
	}
	if len(rows) != len(wantProblem) {
		t.Fatalf("channels %#v", rows)
	}
	for _, row := range rows {
		id := asString(row["channel_id"])
		if row["start_problem"] != wantProblem[id] {
			t.Fatalf("%s start_problem %#v", id, row["start_problem"])
		}
	}
	settings, err := store.GateSettings(ctx)
	if err != nil {
		t.Fatal(err)
	}
	targets, _ := settings["targets"].([]any)
	var chats []string
	for _, item := range targets {
		view, _ := item.(map[string]any)
		chat := asString(view["chat"])
		chats = append(chats, chat)
		if strings.Contains(asString(view["label"]), round) || chat == round {
			t.Fatalf("rounded sender %#v", view)
		}
	}
	if len(chats) != 2 || chats[0] != exact || chats[1] != plain {
		t.Fatalf("targets %#v", targets)
	}
}
