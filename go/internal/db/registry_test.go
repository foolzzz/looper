package db

import (
	"strings"
	"testing"
)

func TestCostWarnings(t *testing.T) {
	cases := []struct {
		name   string
		row    map[string]any
		want   int
		part   string
		absent string
	}{
		{
			name: "claude oauth priced by runtime",
			row: map[string]any{
				"runtime_type": "claude-code", "provider_auth": "oauth", "provider_name": "claude-subscription",
				"provider_oauth_runtime_type": "claude-code", "provider_base_url": "", "model": "opus",
			},
		},
		{
			name: "claude official api is runtime",
			row: map[string]any{
				"runtime_type": "claude-code", "provider_auth": "api_key", "provider_name": "anthropic",
				"provider_base_url": "https://api.anthropic.com", "model": "claude-opus-5-5",
			},
		},
		{
			name: "claude proxy unpriced",
			row: map[string]any{
				"runtime_type": "claude-code", "provider_auth": "api_key", "provider_name": "masked-key",
				"provider_base_url": "https://example.invalid/v1", "model": "mystery-model",
			},
			want: 1, part: "model 'mystery-model' has no full price with provider 'masked-key'",
		},
		{
			name: "grok runtime has no per-run cap",
			row: map[string]any{
				"runtime_type": "grok", "provider_auth": "oauth", "provider_name": "grok-login",
				"provider_oauth_runtime_type": "grok", "model": "grok-4.7",
			},
			want: 1, part: "Grok reports a run's cost only when the run ends",
			absent: "has no full price",
		},
		{
			name: "codex prices the provider",
			row: map[string]any{
				"runtime_type": "codex", "provider_auth": "api_key", "provider_name": "masked-key",
				"provider_base_url": "https://example.invalid/v1", "model": "mystery-model",
				"provider_models": []any{map[string]any{"name": "mystery-model", "context_window": int64(1000)}},
			},
			want: 1, part: "mystery-model", absent: "reports a run's cost",
		},
		{
			name: "unknown runtime",
			row:  map[string]any{"runtime_type": "custom", "provider_auth": "api_key", "provider_name": "x", "model": "m"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := costWarnings(tc.row)
			if len(got) != tc.want {
				t.Fatalf("warnings %d %v", len(got), got)
			}
			text := strings.Join(got, "\n")
			if tc.part != "" && !strings.Contains(text, tc.part) {
				t.Fatalf("missing %q in %s", tc.part, text)
			}
			if tc.absent != "" && strings.Contains(text, tc.absent) {
				t.Fatalf("unexpected %q in %s", tc.absent, text)
			}
		})
	}
}
