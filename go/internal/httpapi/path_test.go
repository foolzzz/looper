package httpapi

import "testing"

func TestPythonURLPath(t *testing.T) {
	tests := []struct {
		in, want string
	}{
		{in: "/api/tasks/task-rich00001;foo", want: "/api/tasks/task-rich00001"},
		{in: "/api/tasks/task-rich00001;foo?x=1", want: "/api/tasks/task-rich00001"},
		{in: "/api/tasks/task-rich00001;foo/bar", want: "/api/tasks/task-rich00001;foo/bar"},
		{in: "/api/tasks/task-rich00001;./foo", want: "/api/tasks/task-rich00001;./foo"},
		{in: "/api/./tasks/task-rich00001", want: "/api/./tasks/task-rich00001"},
		{in: "/api//tasks/task-rich00001", want: "/api//tasks/task-rich00001"},
		{in: "/api/tasks/../tasks/task-rich00001", want: "/api/tasks/../tasks/task-rich00001"},
		{in: "//api/status", want: "/status"},
		{in: "///api/status", want: "/api/status"},
		{in: "http://127.0.0.1/api/status", want: "/api/status"},
		{in: "/api/status;ignored", want: "/api/status"},
		{in: "/api/tasks;foo?limit=1", want: "/api/tasks"},
		{in: "/api/tasks/task-rich0000%31;foo", want: "/api/tasks/task-rich0000%31"},
		{in: "/api/tasks/task-rich00001/", want: "/api/tasks/task-rich00001/"},
		{in: "/api/tasks/task-rich00001;foo#frag", want: "/api/tasks/task-rich00001"},
		{in: "/a?b#c", want: "/a"},
		{in: "/a#b?c", want: "/a"},
	}
	for _, tt := range tests {
		t.Run(tt.in, func(t *testing.T) {
			if got := pythonURLPath(tt.in); got != tt.want {
				t.Fatalf("path %q, want %q", got, tt.want)
			}
		})
	}
}

func TestCanonicalPath(t *testing.T) {
	tests := []struct {
		in, want string
	}{
		{in: "/api/tasks/task-rich00001", want: "/api/tasks/task-rich00001"},
		{in: "/api/./tasks/task-rich00001", want: "/api/tasks/task-rich00001"},
		{in: "/api//tasks/task-rich00001", want: "/api/tasks/task-rich00001"},
		{in: "/api/tasks/../tasks/task-rich00001", want: "/api/tasks/task-rich00001"},
		{in: "/api/tasks/", want: "/api/tasks/"},
		{in: "/api/tasks/task-rich0000%31", want: "/api/tasks/task-rich0000%31"},
	}
	for _, tt := range tests {
		t.Run(tt.in, func(t *testing.T) {
			if got := canonicalPath(tt.in); got != tt.want {
				t.Fatalf("canonical %q, want %q", got, tt.want)
			}
		})
	}
}
