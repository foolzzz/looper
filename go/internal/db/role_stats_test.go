package db

import (
	"testing"
)

func TestAggregateProjectRolesSumsBeforeTheRate(t *testing.T) {
	version := []map[string]any{{"role_key": "backend", "kind": "work"}}
	body, err := aggregateProjectRoles("proj-x", []taskRoleSource{
		{versions: version, runs: []map[string]any{roleRun("worker", "backend", "contract", 1)}},
		{versions: version, runs: []map[string]any{roleRun("worker", "backend", "passed", 3)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	roles, ok := body["roles"].([]any)
	if !ok || len(roles) != 1 {
		t.Fatalf("roles %#v", body["roles"])
	}
	role := roles[0].(map[string]any)
	if role["tasks"] != 2 {
		t.Fatalf("tasks %#v", role["tasks"])
	}
	stats := role["stats"].(map[string]any)
	if stats["runs"] != 4 || stats["contract_violations"] != 1 || stats["contract_violation_rate"] != 0.25 {
		t.Fatalf("stats %#v", stats)
	}
}

func TestAggregateProjectRolesShape(t *testing.T) {
	body, err := aggregateProjectRoles("proj-x", []taskRoleSource{
		{versions: []map[string]any{
			{"role_key": "a", "kind": "work", "template_key": "from-v1"},
			{"role_key": "a", "kind": "review"},
		}},
		{versions: []map[string]any{
			{"role_key": "a", "kind": "review"},
			{"role_key": "m", "kind": "work", "template_key": ""},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if body["tasks"] != 2 {
		t.Fatalf("tasks %#v", body["tasks"])
	}
	roles := body["roles"].([]any)
	if len(roles) != 2 {
		t.Fatalf("roles %#v", roles)
	}
	first := roles[0].(map[string]any)
	second := roles[1].(map[string]any)
	if first["id"] != "a" || first["tasks"] != 2 || second["id"] != "m" || second["tasks"] != 1 {
		t.Fatalf("order %#v then %#v", first, second)
	}
	// The current version's kind only. The earlier version still contributes its template.
	if kinds := first["kinds"].([]any); len(kinds) != 1 || kinds[0] != "review" {
		t.Fatalf("kinds %#v", first["kinds"])
	}
	if templates := first["templates"].([]any); len(templates) != 1 || templates[0] != "from-v1" {
		t.Fatalf("templates %#v", first["templates"])
	}
	if templates := second["templates"].([]any); templates == nil || len(templates) != 0 {
		t.Fatalf("empty templates %#v", second["templates"])
	}
	builtin := body["builtin"].(map[string]any)
	for _, name := range []string{"worker", "reviewer"} {
		stats := builtin[name].(map[string]any)
		if stats["runs"] != 0 || stats["cost_usd"] != 0.0 || stats["contract_violation_rate"] != nil {
			t.Fatalf("%s %#v", name, stats)
		}
	}
}

func TestAggregateProjectRolesEmpty(t *testing.T) {
	body, err := aggregateProjectRoles("proj-x", nil)
	if err != nil {
		t.Fatal(err)
	}
	if body["tasks"] != 0 {
		t.Fatalf("tasks %#v", body["tasks"])
	}
	roles, ok := body["roles"].([]any)
	if !ok || roles == nil || len(roles) != 0 {
		t.Fatalf("roles %#v", body["roles"])
	}
	builtin := body["builtin"].(map[string]any)
	if _, ok := builtin["worker"].(map[string]any); !ok {
		t.Fatalf("builtin %#v", builtin)
	}
	if _, ok := builtin["reviewer"].(map[string]any); !ok {
		t.Fatalf("builtin %#v", builtin)
	}
}

func TestAggregateProjectRolesRejectsKind(t *testing.T) {
	_, err := aggregateProjectRoles("proj-x", []taskRoleSource{{
		versions: []map[string]any{{"role_key": "backend", "kind": "nope"}},
	}})
	if err == nil {
		t.Fatal("accepted kind nope")
	}
}

func roleRun(role, taskRole, outcome string, runs int) map[string]any {
	return map[string]any{
		"role": role, "task_role": taskRole, "outcome": outcome,
		"runs": runs, "running": 0, "cost_usd": 0.0,
	}
}
