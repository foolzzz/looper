package db

import (
	"reflect"
	"testing"
	"time"
)

func TestRoomMembers(t *testing.T) {
	when := time.Date(2020, 5, 1, 0, 0, 0, 0, time.UTC)
	steps := []map[string]any{
		{"id": "step-1", "kind": "work", "step_key": "s1", "status": "verified", "role_key": "backend", "worker_id": "worker-1", "review": true, "reviewer_id": "reviewer-1", "review_stage": "builtin"},
		{"id": "step-2", "kind": "work", "step_key": "s2", "status": "running", "worker_id": "worker-2"},
		{"id": "step-g", "kind": "gate", "step_key": "g1", "status": "waiting_user"},
		{"id": "step-4", "kind": "work", "step_key": "s4", "status": "failed"},
		{"id": "step-5", "kind": "work", "step_key": "s5", "status": "skipped", "role_key": "backend"},
	}
	got := memberViews(roomMembers(steps, nil, nil))
	want := []map[string]any{
		{"seat": "backend@s1", "role": "backend", "kind": "work", "step": "s1", "step_status": "verified", "presence": "offline"},
		{"seat": "reviewer@s1", "role": "reviewer", "kind": "review", "step": "s1", "step_status": "verified", "presence": "offline"},
		{"seat": "worker@s2", "role": "worker", "kind": "work", "step": "s2", "step_status": "running", "presence": "offline"},
		{"seat": "worker@s4", "role": "worker", "kind": "work", "step": "s4", "step_status": "failed", "presence": "planned"},
		{"seat": "backend@s5", "role": "backend", "kind": "work", "step": "s5", "step_status": "skipped", "presence": "dropped"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%#v", got)
	}

	roleSteps := []map[string]any{{
		"id": "step-1", "kind": "work", "step_key": "s1", "status": "verified", "role_key": "backend",
		"worker_id": "worker-1", "review": true, "reviewer_id": "reviewer-1", "review_stage": "role",
		"review_role_key": "qa", "result": map[string]any{"role_review": true},
	}}
	runs := map[string]roomRun{"reviewer-1": {State: "pending", Since: when}}
	got = memberViews(roomMembers(roleSteps, runs, map[string]bool{"backend@s1": true}))
	want = []map[string]any{
		{"seat": "backend@s1", "role": "backend", "kind": "work", "step": "s1", "step_status": "verified", "presence": "consulting"},
		{"seat": "reviewer@s1", "role": "reviewer", "kind": "review", "step": "s1", "step_status": "verified", "presence": "offline"},
		{"seat": "qa@s1", "role": "qa", "kind": "review", "step": "s1", "step_status": "verified", "presence": "waiting", "since": "2020-05-01T00:00:00+00:00"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("role review %#v", got)
	}

	online := map[string]roomRun{"reviewer-1": {State: "running", Since: when}}
	builtin := []map[string]any{{
		"id": "step-1", "kind": "work", "step_key": "s1", "status": "verified", "role_key": "",
		"review": true, "reviewer_id": "reviewer-1", "review_stage": "builtin", "review_role_key": "qa",
		"result": "not-a-map",
	}}
	got = memberViews(roomMembers(builtin, online, map[string]bool{"worker@s1": true}))
	want = []map[string]any{
		{"seat": "worker@s1", "role": "worker", "kind": "work", "step": "s1", "step_status": "verified", "presence": "planned"},
		{"seat": "reviewer@s1", "role": "reviewer", "kind": "review", "step": "s1", "step_status": "verified", "presence": "online", "since": "2020-05-01T00:00:00+00:00"},
		{"seat": "qa@s1", "role": "qa", "kind": "review", "step": "s1", "step_status": "verified", "presence": "planned"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("builtin review %#v", got)
	}
}

func TestRoomTaskViewFilters(t *testing.T) {
	messages := []map[string]any{
		{"id": int64(1), "sender_seat": "backend@s1", "audience": []any{"worker@s2"}, "kind": "question", "body": "port?", "via": "agent", "created_at": "2020-05-01T00:00:00+00:00"},
		{"id": int64(2), "sender_seat": "worker@s2", "audience": []any{"backend@s1"}, "kind": "answer", "body": "8080", "via": "agent", "reply_to": int64(1), "created_at": "2020-05-01T00:00:01+00:00"},
		{"id": int64(3), "sender_seat": "backend@s1", "audience": []any{"worker@s4"}, "kind": "question", "body": "schema?", "via": "consult", "reason": "watch", "audience_all": false, "cc_planner": true, "refs": []any{map[string]any{"file": "a.go"}, map[string]any{"commit": "abcd"}}, "created_at": "2020-05-01T00:00:02+00:00"},
	}
	steps := []map[string]any{
		{"id": "step-1", "kind": "work", "step_key": "s1", "status": "verified", "role_key": "backend", "worker_id": "w1"},
		{"id": "step-2", "kind": "work", "step_key": "s2", "status": "running", "worker_id": "w2"},
		{"id": "step-4", "kind": "work", "step_key": "s4", "status": "failed"},
		{"id": "step-5", "kind": "work", "step_key": "s5", "status": "skipped", "role_key": "backend"},
	}
	consults := []map[string]any{
		{"seat": "worker@s2", "questions": []int64{}, "state": "answered", "read_only": "runtime", "denied_writes": int32(0), "cost_usd": 0.125, "created_at": "2020-05-03T00:00:00+00:00"},
		{"seat": "backend@s1", "questions": []int64{1}, "state": "fallback", "fallback_reason": "no_session", "denied_writes": nil, "cost_usd": 0.25, "created_at": "2020-05-03T00:00:02+00:00"},
		{"seat": "worker@s4", "questions": []int64{}, "state": "failed", "read_only": "snapshot", "cost_usd": nil, "created_at": nil},
	}
	full := roomTaskView(map[string]any{"agent_room": "on"}, steps, messages, consults, nil, RoomQuery{Limit: 500})
	if full["enabled"] != true || full["more"] != false {
		t.Fatalf("enabled %v more %v", full["enabled"], full["more"])
	}
	if got := len(full["messages"].([]any)); got != 3 {
		t.Fatalf("messages %d", got)
	}
	counts := full["counts"].(map[string]any)
	if counts["messages"] != 3 || counts["questions"] != 2 || counts["unanswered"] != 1 {
		t.Fatalf("counts %#v", counts)
	}
	byKind := counts["by_kind"].(map[string]int)
	if byKind["question"] != 2 || byKind["answer"] != 1 {
		t.Fatalf("by_kind %#v", byKind)
	}
	third := full["messages"].([]any)[2].(map[string]any)
	if third["answered"] != false || third["cc_planner"] != true || third["via"] != "consult" || third["reason"] != "watch" {
		t.Fatalf("third %#v", third)
	}
	if _, ok := third["reply_to"]; ok {
		t.Fatal("question has reply_to")
	}
	if !reflect.DeepEqual(third["files"], []any{"a.go"}) || !reflect.DeepEqual(third["commits"], []any{"abcd"}) {
		t.Fatalf("refs %#v %#v", third["files"], third["commits"])
	}
	second := full["messages"].([]any)[1].(map[string]any)
	if second["reply_to"] != int64(1) {
		t.Fatalf("reply_to %#v", second["reply_to"])
	}
	if _, ok := second["via"]; ok {
		t.Fatal("agent via was kept")
	}
	byStep := full["by_step"].(map[string]any)
	if _, ok := byStep["s5"]; ok {
		t.Fatal("empty step kept")
	}
	if !reflect.DeepEqual(byStep["s1"], map[string]any{"sent": 2, "received": 1, "unanswered": 1}) {
		t.Fatalf("s1 %#v", byStep["s1"])
	}
	consult := full["consults"].(map[string]any)
	if consult["total"] != 3 || consult["answered"] != 1 || consult["cost_usd"] != 0.375 {
		t.Fatalf("consults %#v", consult)
	}
	if !reflect.DeepEqual(consult["by_reason"], map[string]any{"no_session": 1}) {
		t.Fatalf("by_reason %#v", consult["by_reason"])
	}
	list := consult["list"].([]any)
	if !reflect.DeepEqual(list[0].(map[string]any)["questions"], []int{}) || list[2].(map[string]any)["cost_usd"] != nil {
		t.Fatalf("list %#v", list)
	}
	if list[1].(map[string]any)["denied_writes"] != 0 {
		t.Fatalf("denied %#v", list[1])
	}

	kind := roomTaskView(nil, steps, messages, nil, nil, RoomQuery{Kind: "nope", Limit: 500})
	if len(kind["messages"].([]any)) != 0 || kind["counts"].(map[string]any)["messages"] != 3 {
		t.Fatalf("empty kind still counts %#v", kind["counts"])
	}
	if len(kind["seats"].([]any)) != 4 {
		t.Fatalf("seats %#v", kind["seats"])
	}
	if kind["enabled"] != true {
		t.Fatal("missing project disabled the room")
	}

	filtered := roomTaskView(map[string]any{"agent_room": "off"}, steps, messages, nil, nil, RoomQuery{
		Kind: "question", Sender: "backend", Unanswered: true, After: 1, Limit: 1,
	})
	if filtered["enabled"] != false || filtered["more"] != false {
		t.Fatalf("filtered flags %#v %#v", filtered["enabled"], filtered["more"])
	}
	only := filtered["messages"].([]any)
	if len(only) != 1 || only[0].(map[string]any)["id"] != int64(3) {
		t.Fatalf("filtered %#v", only)
	}
	if filtered["counts"].(map[string]any)["messages"] != 3 {
		t.Fatal("filter changed the counts")
	}

	window := roomTaskView(nil, nil, messages, nil, nil, RoomQuery{Limit: 1})
	if window["more"] != true || window["messages"].([]any)[0].(map[string]any)["id"] != int64(3) {
		t.Fatalf("window %#v", window["messages"])
	}
	clamped := roomTaskView(nil, nil, messages, nil, nil, RoomQuery{Limit: 0})
	if clamped["more"] != true || len(clamped["messages"].([]any)) != 1 {
		t.Fatalf("limit 0 %#v more %v", clamped["messages"], clamped["more"])
	}
	none := roomTaskView(nil, nil, messages, nil, nil, RoomQuery{AfterTooBig: true, Limit: 0})
	if none["more"] != false || len(none["messages"].([]any)) != 0 {
		t.Fatalf("too big %#v more %v", none["messages"], none["more"])
	}
	broadcast := roomMessageView(map[string]any{
		"id": int64(9), "sender_seat": "planner", "audience_all": true, "kind": "announce", "body": "hi",
		"answers_ids": []int64{1, 2}, "via": "looper",
	})
	if broadcast["to"] != "all" || !reflect.DeepEqual(broadcast["answers_ids"], []int{1, 2}) || broadcast["via"] != "looper" {
		t.Fatalf("broadcast %#v", broadcast)
	}
}

func memberViews(members []roomMember) []map[string]any {
	out := make([]map[string]any, 0, len(members))
	for _, member := range members {
		out = append(out, member.view())
	}
	return out
}
