package db

import "testing"

func TestTaskName(t *testing.T) {
	tests := []struct {
		name, title, goal, requirement, id, want string
	}{
		{name: "title words", title: "  Ship   the patch  ", id: "id", want: "Ship the patch"},
		{
			name: "file separators between words", title: "\x1c  File\x1dseparator  \x1ename \x1f",
			requirement: "should not win", id: "id", want: "File separator name",
		},
		{name: "goal separators between words", goal: "a\x1cb", id: "id", want: "a b"},
		{
			name: "separators only fall through", title: "\x1c\x1d\x1e\x1f", goal: " \x1c ",
			requirement: "\x1cfallback line\x1f\nsecond line", id: "id", want: "fallback line",
		},
		{name: "title separators do not hide a goal", title: "\x1c", goal: "kept", id: "id", want: "kept"},
		{name: "goal when title blank", goal: "  goal   alpha  ", requirement: "ignored", id: "id", want: "goal alpha"},
		{name: "requirement first line", requirement: "Plan the\nsecond line stays out", id: "id", want: "Plan the"},
		{name: "id", id: "task-1", want: "task-1"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := taskName(tt.title, tt.goal, tt.requirement, tt.id)
			if got != tt.want {
				t.Fatalf("got %q, want %q", got, tt.want)
			}
		})
	}
}
