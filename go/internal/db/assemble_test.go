package db

import (
	"math"
	"strconv"
	"testing"
)

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

// These are CPython round(v, n). 1/32 is the contract-violation case
// math.Round turns into 0.0313. 1/160 still differs after scaling and
// rounding half to even. 2.675 is the float that is not the decimal it
// looks like.
func TestRoundN(t *testing.T) {
	tests := []struct {
		name string
		v    float64
		n    int
		want string
	}{
		{name: "one of 32", v: 1.0 / 32, n: 4, want: "0.0312"},
		{name: "three of 32", v: 3.0 / 32, n: 4, want: "0.0938"},
		{name: "seventeen of 32", v: 17.0 / 32, n: 4, want: "0.5312"},
		{name: "one of 160", v: 1.0 / 160, n: 4, want: "0.0063"},
		{name: "three of 160", v: 3.0 / 160, n: 4, want: "0.0187"},
		{name: "two thirds", v: 2.0 / 3, n: 4, want: "0.6667"},
		{name: "one third", v: 1.0 / 3, n: 4, want: "0.3333"},
		{name: "zero", v: 0, n: 4, want: "0.0000"},
		{name: "one", v: 1, n: 4, want: "1.0000"},
		{name: "negative one of 32", v: -1.0 / 32, n: 4, want: "-0.0312"},
		{name: "half millionth", v: 5e-7, n: 6, want: "0.000000"},
		{name: "one and a half millionths", v: 1.5e-6, n: 6, want: "0.000002"},
		{name: "two and a half millionths", v: 2.5e-6, n: 6, want: "0.000003"},
		{name: "two point six seven five", v: 2.675, n: 2, want: "2.67"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := roundN(tt.v, tt.n)
			want, err := strconv.ParseFloat(tt.want, 64)
			if err != nil {
				t.Fatal(err)
			}
			if got != want || strconv.FormatFloat(got, 'f', tt.n, 64) != tt.want {
				t.Fatalf("roundN(%v, %d) = %v (%s), want %s", tt.v, tt.n, got, strconv.FormatFloat(got, 'f', tt.n, 64), tt.want)
			}
		})
	}
	if !math.IsNaN(roundN(math.NaN(), 4)) || !math.IsInf(roundN(math.Inf(1), 4), 1) {
		t.Fatal("non-finite input was changed")
	}
}

func TestRoleRateBoundaries(t *testing.T) {
	tests := []struct {
		name                    string
		violations, finished    int
		passed, notPassed       int
		wantViolation, wantPass string
	}{
		{name: "one of 32", violations: 1, finished: 32, passed: 1, notPassed: 31, wantViolation: "0.0312", wantPass: "0.0312"},
		{name: "three of 32", violations: 3, finished: 32, passed: 3, notPassed: 29, wantViolation: "0.0938", wantPass: "0.0938"},
		{name: "seventeen of 32 reviews", violations: 0, finished: 1, passed: 17, notPassed: 15, wantViolation: "0.0000", wantPass: "0.5312"},
		{name: "none finished", violations: 0, finished: 0, passed: 0, notPassed: 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var runs []map[string]any
			if tt.violations > 0 {
				runs = append(runs, map[string]any{"outcome": "contract", "runs": tt.violations, "running": 0, "cost_usd": 0.0})
			}
			if rest := tt.finished - tt.violations; rest > 0 {
				runs = append(runs, map[string]any{"outcome": "ok", "runs": rest, "running": 0, "cost_usd": 0.0})
			}
			var reviews []map[string]any
			if tt.passed > 0 {
				reviews = append(reviews, map[string]any{"outcome": "passed", "runs": tt.passed, "running": 0})
			}
			if tt.notPassed > 0 {
				reviews = append(reviews, map[string]any{"outcome": "not_passed", "runs": tt.notPassed, "running": 0})
			}
			got := roleStats(runs, nil, reviews)
			assertRate(t, "contract_violation_rate", got["contract_violation_rate"], tt.wantViolation)
			assertRate(t, "review_pass_rate", got["review_pass_rate"], tt.wantPass)
		})
	}
}

func assertRate(t *testing.T, name string, got any, want string) {
	t.Helper()
	if want == "" {
		if got != nil {
			t.Fatalf("%s = %#v, want null", name, got)
		}
		return
	}
	wantN, err := strconv.ParseFloat(want, 64)
	if err != nil {
		t.Fatal(err)
	}
	gotN, ok := got.(float64)
	if !ok || gotN != wantN {
		t.Fatalf("%s = %#v, want %s", name, got, want)
	}
}
