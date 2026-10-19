package parity

import (
	"bytes"
	"errors"
	"fmt"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// volatileToken replaces one JSON value span. The quotes are part of the
// token. The splice does not re-encode the document.
const volatileToken = `"<volatile>"`

var errPointerMissing = errors.New("not found")

// replaceJSONPointers splices volatileToken over each value addressed by a
// JSON Pointer (RFC 6901). Key order, spaces, and every other value stay as
// written. A missing path, an overlapping path, or a document that is not
// JSON is an error. paths empty returns body unchanged.
func replaceJSONPointers(body []byte, paths []string) ([]byte, error) {
	if len(paths) == 0 {
		return body, nil
	}
	type span struct{ start, end int }
	spans := make([]span, 0, len(paths))
	for _, p := range paths {
		tokens, err := parsePointer(p)
		if err != nil {
			return nil, err
		}
		start, end, err := findPath(body, 0, tokens)
		if err != nil {
			if errors.Is(err, errPointerMissing) {
				return nil, fmt.Errorf("json pointer %s: not found", p)
			}
			return nil, fmt.Errorf("json pointer %s: %w", p, err)
		}
		spans = append(spans, span{start, end})
	}
	for i := 1; i < len(spans); i++ {
		for j := 0; j < i; j++ {
			if spans[i].start < spans[j].end && spans[j].start < spans[i].end {
				return nil, fmt.Errorf("json pointer: overlapping paths")
			}
		}
	}
	// Apply from the left into a new buffer so later spans keep their offsets.
	order := make([]int, len(spans))
	for i := range order {
		order[i] = i
	}
	for i := 1; i < len(order); i++ {
		for j := i; j > 0 && spans[order[j]].start < spans[order[j-1]].start; j-- {
			order[j], order[j-1] = order[j-1], order[j]
		}
	}
	var buf []byte
	at := 0
	for _, idx := range order {
		sp := spans[idx]
		if sp.start < at || sp.end < sp.start || sp.end > len(body) {
			return nil, fmt.Errorf("json pointer: bad span")
		}
		buf = append(buf, body[at:sp.start]...)
		buf = append(buf, volatileToken...)
		at = sp.end
	}
	buf = append(buf, body[at:]...)
	return buf, nil
}

func parsePointer(p string) ([]string, error) {
	if p == "" {
		return nil, nil
	}
	if !strings.HasPrefix(p, "/") {
		return nil, fmt.Errorf("json pointer %q: must start with /", p)
	}
	parts := strings.Split(p, "/")[1:]
	for i, part := range parts {
		parts[i] = unescapePointer(part)
	}
	return parts, nil
}

// unescapePointer reverses RFC 6901 encoding. ~1 is slash and ~0 is tilde,
// read left to right so ~01 stays tilde + "1".
func unescapePointer(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '~' && i+1 < len(s) {
			switch s[i+1] {
			case '0':
				b.WriteByte('~')
				i++
				continue
			case '1':
				b.WriteByte('/')
				i++
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

func findPath(data []byte, i int, tokens []string) (int, int, error) {
	i = skipWS(data, i)
	if i >= len(data) {
		return 0, 0, fmt.Errorf("python json scan: truncated")
	}
	if len(tokens) == 0 {
		end, err := valueEnd(data, i)
		if err != nil {
			return 0, 0, err
		}
		return i, end, nil
	}
	switch data[i] {
	case '{':
		return findObject(data, i, tokens)
	case '[':
		return findArray(data, i, tokens)
	default:
		return 0, 0, fmt.Errorf("python json scan: cannot descend at %d", i)
	}
}

func findObject(data []byte, i int, tokens []string) (int, int, error) {
	i++
	i = skipWS(data, i)
	if i < len(data) && data[i] == '}' {
		return 0, 0, errPointerMissing
	}
	for {
		if i >= len(data) || data[i] != '"' {
			return 0, 0, fmt.Errorf("python json scan: want key")
		}
		key, keyEnd, err := scanString(data, i)
		if err != nil {
			return 0, 0, err
		}
		i = skipWS(data, keyEnd)
		if i >= len(data) || data[i] != ':' {
			return 0, 0, fmt.Errorf("python json scan: want colon")
		}
		i = skipWS(data, i+1)
		if key == tokens[0] {
			return findPath(data, i, tokens[1:])
		}
		end, err := valueEnd(data, i)
		if err != nil {
			return 0, 0, err
		}
		i = skipWS(data, end)
		if i >= len(data) {
			return 0, 0, fmt.Errorf("python json scan: truncated object")
		}
		if data[i] == '}' {
			return 0, 0, errPointerMissing
		}
		if data[i] != ',' {
			return 0, 0, fmt.Errorf("python json scan: want comma")
		}
		i = skipWS(data, i+1)
	}
}

func findArray(data []byte, i int, tokens []string) (int, int, error) {
	idx, ok := pointerIndex(tokens[0])
	if !ok {
		return 0, 0, errPointerMissing
	}
	i++
	i = skipWS(data, i)
	if i < len(data) && data[i] == ']' {
		return 0, 0, errPointerMissing
	}
	for n := 0; ; n++ {
		if i >= len(data) {
			return 0, 0, fmt.Errorf("python json scan: truncated array")
		}
		if n == idx {
			return findPath(data, i, tokens[1:])
		}
		end, err := valueEnd(data, i)
		if err != nil {
			return 0, 0, err
		}
		i = skipWS(data, end)
		if i >= len(data) {
			return 0, 0, fmt.Errorf("python json scan: truncated array")
		}
		if data[i] == ']' {
			return 0, 0, errPointerMissing
		}
		if data[i] != ',' {
			return 0, 0, fmt.Errorf("python json scan: want comma")
		}
		i = skipWS(data, i+1)
	}
}

// pointerIndex accepts "0" and a decimal index without a leading zero.
func pointerIndex(tok string) (int, bool) {
	if tok == "0" {
		return 0, true
	}
	if tok == "" || tok[0] < '1' || tok[0] > '9' {
		return 0, false
	}
	n := 0
	for _, c := range tok {
		if c < '0' || c > '9' {
			return 0, false
		}
		n = n*10 + int(c-'0')
	}
	return n, true
}

func valueEnd(data []byte, i int) (int, error) {
	i = skipWS(data, i)
	if i >= len(data) {
		return 0, fmt.Errorf("python json scan: truncated")
	}
	switch data[i] {
	case '"':
		_, end, err := scanString(data, i)
		return end, err
	case '{':
		return containerEnd(data, i, '}')
	case '[':
		return containerEnd(data, i, ']')
	case 't':
		return literalEnd(data, i, "true")
	case 'f':
		return literalEnd(data, i, "false")
	case 'n':
		return literalEnd(data, i, "null")
	case 'N':
		return literalEnd(data, i, "NaN")
	case 'I':
		return literalEnd(data, i, "Infinity")
	default:
		return numberEnd(data, i)
	}
}

func containerEnd(data []byte, i int, closing byte) (int, error) {
	i++
	i = skipWS(data, i)
	if i < len(data) && data[i] == closing {
		return i + 1, nil
	}
	for {
		if i >= len(data) {
			return 0, fmt.Errorf("python json scan: truncated container")
		}
		if closing == '}' {
			if data[i] != '"' {
				return 0, fmt.Errorf("python json scan: want key")
			}
			_, keyEnd, err := scanString(data, i)
			if err != nil {
				return 0, err
			}
			i = skipWS(data, keyEnd)
			if i >= len(data) || data[i] != ':' {
				return 0, fmt.Errorf("python json scan: want colon")
			}
			i = skipWS(data, i+1)
		}
		end, err := valueEnd(data, i)
		if err != nil {
			return 0, err
		}
		i = skipWS(data, end)
		if i >= len(data) {
			return 0, fmt.Errorf("python json scan: truncated container")
		}
		if data[i] == closing {
			return i + 1, nil
		}
		if data[i] != ',' {
			return 0, fmt.Errorf("python json scan: want comma")
		}
		i = skipWS(data, i+1)
	}
}

func literalEnd(data []byte, i int, lit string) (int, error) {
	if !bytes.HasPrefix(data[i:], []byte(lit)) {
		return 0, fmt.Errorf("python json scan: bad literal at %d", i)
	}
	return i + len(lit), nil
}

func numberEnd(data []byte, i int) (int, error) {
	start := i
	if bytes.HasPrefix(data[i:], []byte("-Infinity")) {
		return i + len("-Infinity"), nil
	}
	if i < len(data) && data[i] == '-' {
		i++
	}
	if i >= len(data) || data[i] < '0' || data[i] > '9' {
		return 0, fmt.Errorf("python json scan: bad number at %d", start)
	}
	for i < len(data) && data[i] >= '0' && data[i] <= '9' {
		i++
	}
	if i < len(data) && data[i] == '.' {
		i++
		if i >= len(data) || data[i] < '0' || data[i] > '9' {
			return 0, fmt.Errorf("python json scan: bad number at %d", start)
		}
		for i < len(data) && data[i] >= '0' && data[i] <= '9' {
			i++
		}
	}
	if i < len(data) && (data[i] == 'e' || data[i] == 'E') {
		i++
		if i < len(data) && (data[i] == '+' || data[i] == '-') {
			i++
		}
		if i >= len(data) || data[i] < '0' || data[i] > '9' {
			return 0, fmt.Errorf("python json scan: bad number at %d", start)
		}
		for i < len(data) && data[i] >= '0' && data[i] <= '9' {
			i++
		}
	}
	return i, nil
}

func scanString(data []byte, i int) (string, int, error) {
	if i >= len(data) || data[i] != '"' {
		return "", 0, fmt.Errorf("python json scan: want string")
	}
	var b strings.Builder
	j := i + 1
	for j < len(data) {
		c := data[j]
		if c == '"' {
			return b.String(), j + 1, nil
		}
		if c == '\\' {
			if j+1 >= len(data) {
				return "", 0, fmt.Errorf("python json scan: truncated escape")
			}
			switch data[j+1] {
			case '"', '\\', '/':
				b.WriteByte(data[j+1])
				j += 2
			case 'b':
				b.WriteByte('\b')
				j += 2
			case 'f':
				b.WriteByte('\f')
				j += 2
			case 'n':
				b.WriteByte('\n')
				j += 2
			case 'r':
				b.WriteByte('\r')
				j += 2
			case 't':
				b.WriteByte('\t')
				j += 2
			case 'u':
				r, next, err := scanU(data, j+2)
				if err != nil {
					return "", 0, err
				}
				b.WriteRune(r)
				j = next
			default:
				return "", 0, fmt.Errorf("python json scan: bad escape")
			}
			continue
		}
		if c < 0x20 {
			return "", 0, fmt.Errorf("python json scan: raw control")
		}
		r, size := utf8.DecodeRune(data[j:])
		if r == utf8.RuneError && size == 1 {
			return "", 0, fmt.Errorf("python json scan: bad utf-8")
		}
		b.WriteRune(r)
		j += size
	}
	return "", 0, fmt.Errorf("python json scan: truncated string")
}

func scanU(data []byte, j int) (rune, int, error) {
	r, next, err := hex4(data, j)
	if err != nil {
		return 0, 0, err
	}
	if utf16.IsSurrogate(r) && next+1 < len(data) && data[next] == '\\' && data[next+1] == 'u' {
		r2, next2, err2 := hex4(data, next+2)
		if err2 == nil && utf16.IsSurrogate(r2) {
			return utf16.DecodeRune(r, r2), next2, nil
		}
	}
	if utf16.IsSurrogate(r) {
		return utf8.RuneError, next, nil
	}
	return r, next, nil
}

func hex4(data []byte, j int) (rune, int, error) {
	if j+4 > len(data) {
		return 0, 0, fmt.Errorf("python json scan: truncated \\u")
	}
	var r rune
	for _, c := range data[j : j+4] {
		var d rune
		switch {
		case c >= '0' && c <= '9':
			d = rune(c - '0')
		case c >= 'a' && c <= 'f':
			d = rune(c-'a') + 10
		case c >= 'A' && c <= 'F':
			d = rune(c-'A') + 10
		default:
			return 0, 0, fmt.Errorf("python json scan: bad \\u")
		}
		r = r<<4 | d
	}
	return r, j + 4, nil
}

func skipWS(data []byte, i int) int {
	for i < len(data) {
		switch data[i] {
		case ' ', '\n', '\r', '\t':
			i++
		default:
			return i
		}
	}
	return i
}
