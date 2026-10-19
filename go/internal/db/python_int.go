package db

import (
	"encoding/json"
	"math"
	"math/big"
	"strings"
	"unicode"
)

// pythonIntMaxDigits is sys.get_int_max_str_digits(), the default limit of
// Python's int() on a str. Digits past it are ValueError. Leading zeros count.
// The sign and surrounding whitespace do not. Underscores between digits do not.
const pythonIntMaxDigits = 4300

// parsePythonInt parses s the way Python int() parses a str. overflow is 1
// when the value is above the int64 range, -1 when it is below, and 0 when n
// is the value. ok is false on ValueError: blank, a non-ASCII sign, a trailing
// or doubled underscore, a non-digit, or more than pythonIntMaxDigits digits.
// Unicode decimal digits are accepted. The whitespace int() strips is
// Unicode White_Space. U+001C–U+001F are str.isspace and are not stripped.
func parsePythonInt(s string) (n int64, overflow int, ok bool) {
	mag, ok := parsePythonBigInt(s)
	if !ok {
		return 0, 0, false
	}
	if !mag.IsInt64() {
		return 0, mag.Sign(), true
	}
	return mag.Int64(), 0, true
}

// parsePythonBigInt is parsePythonInt without the int64 cut. The result is the
// canonical value: no leading zeros, and a leading minus when it is negative.
func parsePythonBigInt(s string) (*big.Int, bool) {
	s = strings.TrimFunc(s, unicode.IsSpace)
	if s == "" {
		return nil, false
	}
	sign := 1
	switch s[0] {
	case '+':
		s = s[1:]
		if s == "" {
			return nil, false
		}
	case '-':
		sign = -1
		s = s[1:]
		if s == "" {
			return nil, false
		}
	}
	if !pythonIntDigits(s) || pythonIntDigitCount(s) > pythonIntMaxDigits {
		return nil, false
	}
	mag := new(big.Int)
	if _, ok := mag.SetString(pythonIntASCII(strings.ReplaceAll(s, "_", "")), 10); !ok {
		return nil, false
	}
	if sign < 0 {
		mag.Neg(mag)
	}
	return mag, true
}

// RoleVersion is Python int() for a role version (task_roles.run_role).
// A value that fits in int is an int. A bigger integer, still within 4,300
// digits, is a json.Number so encoding/json emits a bare number. Past that
// digit limit, or a non-integer string, is dropped. An int64 that does not
// fit in int is dropped. A JSON integer token uses the string rule, so a
// bare 40-digit version read from JSONB is kept. A finite float, including a
// JSON number with a fraction or an exponent, is truncated toward zero the
// way int() truncates the float json.loads would produce. That integer is
// kept when it does not fit in a machine int, so a 40-digit token with a
// trailing ".0" keeps the role.
func RoleVersion(v any) (any, bool) {
	switch t := v.(type) {
	case bool:
		if t {
			return 1, true
		}
		return 0, true
	case int:
		return t, true
	case int32:
		return int(t), true
	case int64:
		if int64(int(t)) != t {
			return nil, false
		}
		return int(t), true
	case float64:
		return pythonIntFromFloat(t)
	case json.Number:
		if jsonIntegerLiteral(string(t)) {
			return RoleVersion(string(t))
		}
		f, err := t.Float64()
		if err != nil {
			return nil, false
		}
		return pythonIntFromFloat(f)
	case string:
		mag, ok := parsePythonBigInt(t)
		if !ok {
			return nil, false
		}
		return pythonIntMagnitude(mag), true
	default:
		return nil, false
	}
}

// pythonIntFromFloat is int() of a float. NaN and infinity are rejected.
// The result is the exact integer of the truncated float.
func pythonIntFromFloat(f float64) (any, bool) {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return nil, false
	}
	mag, _ := new(big.Float).SetPrec(64).SetMode(big.ToZero).SetFloat64(f).Int(nil)
	return pythonIntMagnitude(mag), true
}

func pythonIntMagnitude(mag *big.Int) any {
	if mag.IsInt64() {
		n := mag.Int64()
		if int64(int(n)) == n {
			return int(n)
		}
	}
	return json.Number(mag.Text(10))
}

func pythonIntDigitCount(s string) int {
	n := 0
	for _, r := range s {
		if r != '_' {
			n++
		}
	}
	return n
}

func pythonIntDigits(s string) bool {
	prevDigit := false
	sawDigit := false
	for _, r := range s {
		if r == '_' {
			if !prevDigit {
				return false
			}
			prevDigit = false
			continue
		}
		if _, ok := pythonDecimalDigit(r); !ok {
			return false
		}
		sawDigit = true
		prevDigit = true
	}
	return sawDigit && prevDigit
}

func pythonIntASCII(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		d, _ := pythonDecimalDigit(r)
		b.WriteByte(byte('0' + d))
	}
	return b.String()
}

// pythonDecimalDigit is the value of a Unicode decimal digit. Nd runs are
// groups of ten starting at zero; two groups can sit next to each other, so
// the value is the distance from the start of the contiguous run, modulo 10.
func pythonDecimalDigit(r rune) (int, bool) {
	if !unicode.IsDigit(r) {
		return 0, false
	}
	start := r
	for start > 0 && unicode.IsDigit(start-1) {
		start--
	}
	return int(r-start) % 10, true
}
