package db

import (
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
	s = strings.TrimFunc(s, unicode.IsSpace)
	if s == "" {
		return 0, 0, false
	}
	sign := 1
	switch s[0] {
	case '+':
		s = s[1:]
		if s == "" {
			return 0, 0, false
		}
	case '-':
		sign = -1
		s = s[1:]
		if s == "" {
			return 0, 0, false
		}
	}
	if !pythonIntDigits(s) {
		return 0, 0, false
	}
	if pythonIntDigitCount(s) > pythonIntMaxDigits {
		return 0, 0, false
	}
	mag := new(big.Int)
	if _, ok := mag.SetString(pythonIntASCII(strings.ReplaceAll(s, "_", "")), 10); !ok {
		return 0, 0, false
	}
	if sign < 0 {
		mag.Neg(mag)
	}
	if !mag.IsInt64() {
		return 0, sign, true
	}
	return mag.Int64(), 0, true
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
