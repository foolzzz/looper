package db

import "testing"

func TestOverviewProjectsSortsByNameOrID(t *testing.T) {
	rows := []map[string]any{
		{"project_id": "proj-blank0001", "project_name": "", "status": "init", "count": 1},
		{"project_id": "proj-alpha0001", "project_name": "Alpha", "status": "init", "count": 1},
		{"project_id": "aaa-empty", "project_name": "", "status": "init", "count": 1},
		{"project_id": "proj-ws", "project_name": "  ", "status": "init", "count": 1},
		{"project_id": "zzz-null", "project_name": nil, "status": "init", "count": 1},
		{"project_id": "proj-beta00002", "project_name": "Beta", "status": "completed", "count": 1},
	}
	got := overviewProjects(rows)
	want := []struct {
		id, name string
	}{
		{"proj-ws", "  "},
		{"aaa-empty", "aaa-empty"},
		{"proj-alpha0001", "Alpha"},
		{"proj-blank0001", "proj-blank0001"},
		{"zzz-null", "zzz-null"},
	}
	if len(got) != len(want) {
		t.Fatalf("len %d, want %d: %#v", len(got), len(want), got)
	}
	for i, w := range want {
		item, ok := got[i].(map[string]any)
		if !ok {
			t.Fatalf("row %d %#v", i, got[i])
		}
		if item["project_id"] != w.id || item["name"] != w.name {
			t.Fatalf("row %d id %v name %v, want %s %q", i, item["project_id"], item["name"], w.id, w.name)
		}
		tasks, ok := item["tasks"].(map[string]any)
		if !ok || tasks["ready"] != 1 || tasks["running"] != 0 {
			t.Fatalf("row %d tasks %#v", i, item["tasks"])
		}
	}
}
