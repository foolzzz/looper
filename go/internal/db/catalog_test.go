package db

import "testing"

func TestCatalogQualifiedName(t *testing.T) {
	for _, tc := range []struct{ kind, origin, name, want string }{
		{"plugin", "machine", "tools@market", "tools"},
		{"plugin", "git", "tools", "tools"},
		{"mcp", "ui", "tools@market", "tools@market"},
		{"skill", "project", "check", "project:check"},
		{"skill", "machine", "check", "personal:check"},
		{"skill", "upload", "check", "personal:check"},
		{"skill", "git", "check", "personal:check"},
		{"skill", "ui", "check", "personal:check"},
	} {
		t.Run(tc.kind+"/"+tc.origin, func(t *testing.T) {
			if got := catalogQualifiedName(map[string]any{"kind": tc.kind, "origin": tc.origin, "name": tc.name}); got != tc.want {
				t.Fatalf("qualified name %q, want %q", got, tc.want)
			}
		})
	}
}

func TestCatalogSkillBody(t *testing.T) {
	for _, tc := range []struct{ name, text, want string }{
		{"plain", "\n# Plain\n", "\n# Plain\n"},
		{"frontmatter", "---\nname: check\n---\n\n \t\n# Body\n", "# Body\n"},
		{"bom-crlf", "\ufeff \t---\r\nname: check\r\n --- \r\n \t\r\n# Body\r\n", "# Body\r\n"},
		{"unclosed", "\ufeff---\nname: check\nbody", "\ufeff---\nname: check\nbody"},
		{"empty", "---\n---\n", ""},
		{"indented-body", "---\n---\n\n  Body\n", "  Body\n"},
		{"unicode-body", "---\n---\n\u00a0\nBody\n", "\u00a0\nBody\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := catalogSkillBody(tc.text); got != tc.want {
				t.Fatalf("body %q, want %q", got, tc.want)
			}
		})
	}
}
