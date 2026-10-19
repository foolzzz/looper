package db

import (
	"context"
	"fmt"
)

const channelConfigSQL = `SELECT c.*, s.channel_id IS NOT NULL AS has_bot_token ` +
	`FROM channel_configs c LEFT JOIN channel_secrets s ON s.channel_id = c.channel_id`

// ListChannelConfigs is channel_configs.list_configs plus allowlist.channel_view.
func (s *Store) ListChannelConfigs(ctx context.Context) ([]map[string]any, error) {
	rows, err := s.query(ctx, "channel configs", channelConfigSQL+` ORDER BY c.channel_id`)
	if err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		out = append(out, channelView(row))
	}
	return out, nil
}

// GetChannelConfig is channel_configs.get_config plus channel_view.
// A missing channel is nil, nil.
func (s *Store) GetChannelConfig(ctx context.Context, id string) (map[string]any, error) {
	row, err := s.one(ctx, "channel config", channelConfigSQL+` WHERE c.channel_id = $1`, id)
	if err != nil || row == nil {
		return nil, err
	}
	return channelView(row), nil
}

// GateSettings is gate_service.gate_settings. The saved chat stays in the
// body when it is no longer on an allowlist; dashboard_target_problem says so.
func (s *Store) GateSettings(ctx context.Context) (map[string]any, error) {
	rows, err := s.query(ctx, "channel configs", channelConfigSQL+` ORDER BY c.channel_id`)
	if err != nil {
		return nil, err
	}
	stored, err := s.one(ctx, "dashboard target", `SELECT value FROM system_settings WHERE key = $1`, dashboardTargetKey)
	if err != nil {
		return nil, err
	}
	raw := ""
	if stored != nil {
		raw = asString(stored["value"])
	}
	targets, err := notifyTargetViews(rows)
	if err != nil {
		return nil, fmt.Errorf("gate settings: %w", err)
	}
	var dashboard, problem any
	if chat, ok := dashboardChat(raw); ok {
		dashboard = map[string]any{"channel": chat.channel, "chat": chat.id}
		if !viewOffered(chat, targets) {
			problem = dashboardTargetProblem(chat)
		}
	}
	return map[string]any{
		"dashboard_target":         dashboard,
		"dashboard_target_problem": problem,
		"targets":                  targets,
	}, nil
}

func channelView(row map[string]any) map[string]any {
	out := make(map[string]any, len(row)+1)
	for key, value := range row {
		out[key] = value
	}
	var config map[string]any
	if mapped, ok := row["config"].(map[string]any); ok {
		config = mapped
	}
	out["start_problem"] = startProblem(asString(row["channel_type"]), config)
	return out
}

// startProblem is allowlist.start_problem. nil means the channel may start.
func startProblem(channelType string, config map[string]any) any {
	spec, ok := senderFormat[channelType]
	if !ok {
		return nil
	}
	var raw any
	if config != nil {
		raw = config[spec.field]
	}
	ids, err := senderIDs(channelType, raw)
	if err != nil {
		return "its sender allowlist is invalid: " + err.Error()
	}
	if len(ids) == 0 {
		return fmt.Sprintf("its sender allowlist is empty; list the %ss that may talk to the Manager, which can run any command on this machine", spec.label)
	}
	return nil
}

// notifyTargetViews is schedules.notify_targets. An unknown channel type or an
// allowlist ValueError is skipped. The label uses the middle dot U+00B7.
// A truthy config that is not an object is an error: Python evaluates
// (config or {}).get(field), a non-mapping has no get, and the exception
// makes GET /api/gate-settings return 500. A falsy config is an empty list.
func notifyTargetViews(rows []map[string]any) ([]any, error) {
	out := []any{}
	for _, row := range rows {
		channel := asString(row["channel_type"])
		spec, known := senderFormat[channel]
		if !known {
			continue
		}
		raw, err := notifyAllowlist(row["config"], spec.field)
		if err != nil {
			return nil, err
		}
		ids, err := senderIDs(channel, raw)
		if err != nil {
			continue
		}
		word := "Lark"
		if channel == "telegram" {
			word = "Telegram"
		}
		for _, id := range ids {
			out = append(out, map[string]any{
				"channel":         channel,
				"chat":            id,
				"label":           word + " · " + id,
				"channel_enabled": asBool(row["enabled"]),
			})
		}
	}
	return out, nil
}

// notifyAllowlist is (config or {}).get(field). A falsy config is empty.
// A truthy non-object cannot be read that way.
func notifyAllowlist(config any, field string) (any, error) {
	if !pythonTruthy(config) {
		return nil, nil
	}
	mapped, ok := config.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("channel config is %T, not an object", config)
	}
	return mapped[field], nil
}

// pythonTruthy is Python's truth test for the values a JSON config can hold.
func pythonTruthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case int:
		return t != 0
	case int32:
		return t != 0
	case int64:
		return t != 0
	case float32:
		return t != 0
	case float64:
		return t != 0
	case []any:
		return len(t) > 0
	case map[string]any:
		return len(t) > 0
	default:
		return true
	}
}

func viewOffered(chat gateChat, targets []any) bool {
	for _, item := range targets {
		row, ok := item.(map[string]any)
		if ok && asString(row["channel"]) == chat.channel && asString(row["chat"]) == chat.id {
			return true
		}
	}
	return false
}

func dashboardTargetProblem(chat gateChat) string {
	word := "Telegram"
	if chat.channel == "lark" {
		word = "Lark"
	}
	return chat.label() + " is no longer on the " + word + " channel's sender allowlist: the gates of dashboard tasks go to the task page only"
}
