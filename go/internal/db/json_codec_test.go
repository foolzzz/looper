package db

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestUnmarshalPythonJSON(t *testing.T) {
	const exact = "9007199254740993"
	big := strings.Repeat("9", 40)
	var raw any
	in := []byte(`{"ids":[` + exact + `,123.0,123,-0],"big":` + big + `}`)
	if err := unmarshalPythonJSON(in, &raw); err != nil {
		t.Fatal(err)
	}
	obj, _ := raw.(map[string]any)
	ids, _ := obj["ids"].([]any)
	if len(ids) != 4 {
		t.Fatalf("ids %#v", ids)
	}
	if ids[0] != int64(9007199254740993) {
		t.Fatalf("exact %T %#v", ids[0], ids[0])
	}
	floated, ok := ids[1].(float64)
	if !ok || floated != 123 {
		t.Fatalf("float %T %#v", ids[1], ids[1])
	}
	if ids[2] != int64(123) || ids[3] != int64(0) {
		t.Fatalf("small %#v %#v", ids[2], ids[3])
	}
	number, ok := obj["big"].(json.Number)
	if !ok || string(number) != big {
		t.Fatalf("big %T %#v", obj["big"], obj["big"])
	}
	var concrete map[string]float64
	if err := unmarshalPythonJSON([]byte(`{"a":1.5}`), &concrete); err != nil || concrete["a"] != 1.5 {
		t.Fatalf("concrete %#v err %v", concrete, err)
	}
}
