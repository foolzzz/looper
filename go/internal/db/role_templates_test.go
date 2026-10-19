package db

import "testing"

func TestOneLineCollapsesPythonSpace(t *testing.T) {
	// str.split treats U+001C–U+001F as whitespace. strings.Fields does not,
	// so a description "a\u001cb" used to stay "a\u001cb" instead of "a b".
	tests := []struct {
		name  string
		in    any
		limit int
		want  string
	}{
		{name: "file separator", in: "a\u001cb", limit: 200, want: "a b"},
		{name: "all four controls", in: "a\u001cb\u001dc\u001ed\u001fe", limit: 200, want: "a b c d e"},
		{name: "ordinary space", in: "a  b\tc", limit: 200, want: "a b c"},
		{name: "rune limit after collapse", in: "a\u001cbbbb", limit: 3, want: "a b"},
		{name: "nil", in: nil, limit: 200, want: ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := oneLine(tt.in, tt.limit); got != tt.want {
				t.Fatalf("%q", got)
			}
		})
	}
}
