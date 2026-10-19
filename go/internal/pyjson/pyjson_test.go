package pyjson

import (
	"encoding/json"
	"math"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestMarshalMatchesPythonShape(t *testing.T) {
	type nested struct {
		Channel string `json:"channel"`
		Healthy bool   `json:"healthy"`
		Streams int    `json:"active_sse_streams"`
	}
	type status struct {
		Status string         `json:"status"`
		Agents map[string]any `json:"agents"`
		Tasks  struct {
			Total     int `json:"total"`
			Running   int `json:"running"`
			Completed int `json:"completed"`
		} `json:"tasks"`
		Channels struct {
			Web nested `json:"web"`
		} `json:"channels"`
		Checks []any `json:"checks"`
		Hidden int   `json:"-"`
		skip   int
	}
	body := status{
		Status: "running",
		Agents: map[string]any{},
		Checks: []any{},
	}
	body.Channels.Web = nested{Channel: "web", Healthy: false, Streams: 0}
	got, err := Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"status": "running", "agents": {}, "tasks": {"total": 0, "running": 0, "completed": 0}, "channels": {"web": {"channel": "web", "healthy": false, "active_sse_streams": 0}}, "checks": []}`
	if string(got) != want {
		t.Fatalf("body %s", got)
	}
	if len(got) > 0 && got[len(got)-1] == '\n' {
		t.Fatal("trailing newline")
	}
}

func TestMarshalTable(t *testing.T) {
	type ordered struct {
		Z    int `json:"z"`
		A    int `json:"a,omitempty"`
		Skip int `json:"-"`
		skip int
	}
	type cycle struct {
		Next *cycle
	}
	loop := &cycle{}
	loop.Next = loop
	sliceLoop := make([]any, 1)
	sliceLoop[0] = sliceLoop
	objLoop := make(Object, 1)
	objLoop[0] = Field{Key: "self", Value: objLoop}
	cross := make([]any, 1)
	crossObj := make(Object, 1)
	crossObj[0] = Field{Key: "up", Value: cross}
	cross[0] = crossObj
	nestedSub := make([]any, 3)
	nestedSub[0] = 1
	nestedSub[1] = 2
	nestedSub[2] = nestedSub[:2]
	shared := Object{{Key: "a", Value: 1}}
	num := json.Number("1.0")
	var nilMap map[string]any
	var nilSlice []any
	type hold struct {
		Obj   Object        `json:"obj"`
		N     json.Number   `json:"n"`
		Inner []json.Number `json:"inner"`
	}
	tests := []struct {
		name    string
		v       any
		want    string
		wantErr string
	}{
		{name: "nil", v: nil, want: "null"},
		{name: "bool", v: false, want: "false"},
		{name: "int", v: 2, want: "2"},
		{name: "uint64", v: uint64(1<<64 - 1), want: "18446744073709551615"},
		{name: "empty object", v: Object(nil), want: "{}"},
		{name: "object order", v: Object{{"z", 1}, {"a", 2}}, want: `{"z": 1, "a": 2}`},
		{name: "struct order", v: ordered{Z: 1, A: 0, Skip: 9, skip: 9}, want: `{"z": 1, "a": 0}`},
		{name: "empty map", v: map[string]any{}, want: "{}"},
		{name: "nil map", v: nilMap, want: "null"},
		{name: "filled map", v: map[string]any{"b": 1, "a": 2}, wantErr: "no insertion order"},
		{name: "empty slice", v: []any{}, want: "[]"},
		{name: "nil slice", v: nilSlice, want: "null"},
		{name: "slice", v: []any{int(1), "a<b>", true, nil}, want: `[1, "a<b>", true, null]`},
		{name: "bytes", v: []byte("hi"), wantErr: "[]uint8 is not encoded"},
		{name: "time", v: time.Unix(0, 0).UTC(), wantErr: "time.Time"},
		{name: "number spelling", v: json.Number("1.0"), want: "1.0"},
		{name: "bad number", v: json.Number("1.2.3"), wantErr: "json.Number"},
		{name: "number in slice", v: []any{json.Number("1.0")}, want: `[1.0]`},
		{name: "number slice", v: []json.Number{json.Number("1.0"), json.Number("2.5")}, want: `[1.0, 2.5]`},
		{name: "number pointer in slice", v: []any{&num}, want: `[1.0]`},
		{name: "bad number in slice", v: []any{json.Number("1.2.3")}, wantErr: "json.Number"},
		{name: "object in slice", v: []any{Object{{Key: "z", Value: json.Number("1.0")}}}, want: `[{"z": 1.0}]`},
		{name: "object slice", v: []Object{{{"a", 1}}}, want: `[{"a": 1}]`},
		{name: "nil object in slice", v: []any{Object(nil)}, want: `[{}]`},
		{
			name: "struct fields",
			v: hold{
				Obj:   Object{{Key: "k", Value: json.Number("2.0")}},
				N:     json.Number("1.0"),
				Inner: []json.Number{json.Number("1.0")},
			},
			want: `{"obj": {"k": 2.0}, "n": 1.0, "inner": [1.0]}`,
		},
		{name: "slice cycle", v: sliceLoop, wantErr: "cycle"},
		{name: "object cycle", v: objLoop, wantErr: "cycle"},
		{name: "slice object cycle", v: cross, wantErr: "cycle"},
		{name: "shorter subslice", v: nestedSub, want: `[1, 2, [1, 2]]`},
		{name: "shared object", v: Object{{Key: "x", Value: shared}, {Key: "y", Value: shared}}, want: `{"x": {"a": 1}, "y": {"a": 1}}`},
		{name: "nan", v: math.NaN(), want: "NaN"},
		{name: "inf", v: math.Inf(1), want: "Infinity"},
		{name: "neginf", v: math.Inf(-1), want: "-Infinity"},
		{name: "neg zero", v: math.Copysign(0, -1), want: "-0.0"},
		{name: "pointer nil", v: (*int)(nil), want: "null"},
		{name: "cycle", v: loop, wantErr: "cycle"},
		{name: "channel", v: make(chan int), wantErr: "unsupported type"},
		{
			name: "string escapes",
			v:    "café\u2028\u2029<>&\u007f\u0001a\"b\\c\b\f\n\r\t/",
			want: "\"café\u2028\u2029<>&\u007f\\u0001a\\\"b\\\\c\\b\\f\\n\\r\\t/\"",
		},
		{name: "invalid utf8", v: string([]byte{0xff}), want: "\"\uFFFD\""},
		{name: "apostrophe", v: "it's", want: `"it's"`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := Marshal(tt.v)
			if tt.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
					t.Fatalf("err %v, want substring %q", err, tt.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != tt.want {
				t.Fatalf("got %q\nwant %q", got, tt.want)
			}
			if strings.HasSuffix(string(got), "\n") {
				t.Fatal("trailing newline")
			}
		})
	}
}

func TestFloatFixture(t *testing.T) {
	raw, err := os.ReadFile("testdata/floats.txt")
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimRight(string(raw), "\n"), "\n")
	if len(lines) < 50 {
		t.Fatalf("fixture has %d lines", len(lines))
	}
	for _, line := range lines {
		hex, text, ok := strings.Cut(line, " ")
		if !ok {
			t.Fatalf("bad line %q", line)
		}
		bits, err := strconv.ParseUint(hex, 16, 64)
		if err != nil {
			t.Fatal(err)
		}
		got, err := Marshal(math.Float64frombits(bits))
		if err != nil {
			t.Fatalf("%s: %v", hex, err)
		}
		if string(got) != text {
			t.Fatalf("bits %s got %s want %s", hex, got, text)
		}
	}
}
