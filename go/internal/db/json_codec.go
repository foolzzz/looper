package db

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math/big"

	"github.com/jackc/pgx/v5/pgtype"
)

// registerPythonJSON decodes json and jsonb the way Python's json.loads does
// for numbers. An integer token stays an integer. A token with a fraction or
// an exponent stays a float. The default codec uses encoding/json, which
// makes both a float64.
func registerPythonJSON(m *pgtype.Map) {
	m.RegisterType(&pgtype.Type{
		Name:  "json",
		OID:   pgtype.JSONOID,
		Codec: &pgtype.JSONCodec{Marshal: json.Marshal, Unmarshal: unmarshalPythonJSON},
	})
	m.RegisterType(&pgtype.Type{
		Name:  "jsonb",
		OID:   pgtype.JSONBOID,
		Codec: &pgtype.JSONBCodec{Marshal: json.Marshal, Unmarshal: unmarshalPythonJSON},
	})
}

// unmarshalPythonJSON is the codec Unmarshal. rows.Values decodes into any.
// A concrete destination keeps encoding/json, which fills float64 and int fields.
func unmarshalPythonJSON(data []byte, v any) error {
	target, ok := v.(*any)
	if !ok {
		return json.Unmarshal(data, v)
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := dec.Decode(target); err != nil {
		return err
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		if err == nil {
			return fmt.Errorf("json: extra data")
		}
		return err
	}
	*target = rewritePythonJSON(*target)
	return nil
}

func rewritePythonJSON(v any) any {
	switch t := v.(type) {
	case map[string]any:
		for key, val := range t {
			t[key] = rewritePythonJSON(val)
		}
		return t
	case []any:
		for i, val := range t {
			t[i] = rewritePythonJSON(val)
		}
		return t
	case json.Number:
		return pythonJSONNumber(t)
	default:
		return v
	}
}

// pythonJSONNumber is one JSON number. An integer token that fits in int64 is
// an int64. A bigger integer is a json.Number of its canonical decimal, which
// encoding/json writes as a bare number. Anything else is a float64 when it
// fits, and the original token when it does not.
func pythonJSONNumber(n json.Number) any {
	text := string(n)
	if jsonIntegerLiteral(text) {
		mag, ok := new(big.Int).SetString(text, 10)
		if !ok {
			return n
		}
		if mag.IsInt64() {
			return mag.Int64()
		}
		return json.Number(mag.Text(10))
	}
	f, err := n.Float64()
	if err != nil {
		return n
	}
	return f
}

// jsonIntegerLiteral reports whether s is a JSON integer token: an optional
// minus and one or more digits. 123.0 and 1e2 are floats in Python.
func jsonIntegerLiteral(s string) bool {
	if s == "" {
		return false
	}
	i := 0
	if s[0] == '-' {
		if len(s) == 1 {
			return false
		}
		i = 1
	}
	for ; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

// jsonIntegerText is the decimal Python str(int) would produce for a JSON
// integer token. A float token is not an integer.
func jsonIntegerText(n json.Number) (string, bool) {
	text := string(n)
	if !jsonIntegerLiteral(text) {
		return "", false
	}
	mag, ok := new(big.Int).SetString(text, 10)
	if !ok {
		return "", false
	}
	return mag.Text(10), true
}
