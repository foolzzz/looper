package httpapi

import (
	"strings"
	"unicode/utf8"
)

// pythonQuery parses a raw query the way urllib.parse.parse_qsl does, then
// dict(), so the last value for a key wins. Blank values are dropped.
//
// net/url.URL.Query ignores ParseQuery's error. A pair that contains a raw
// semicolon, or a % that is not two hex digits, is left out of the map.
// Python splits on '&' only, keeps the semicolon inside the value, and
// leaves a bad % sequence in place. That is why "limit=1;" and
// "group=completed;foo=bar" are 400 instead of the default list.
func pythonQuery(raw string) map[string][]string {
	out := make(map[string][]string)
	if raw == "" {
		return out
	}
	for _, part := range strings.Split(raw, "&") {
		if part == "" {
			continue
		}
		name, value, ok := strings.Cut(part, "=")
		// keep_blank_values is false. A field with no '=' has an empty value.
		if !ok || value == "" {
			continue
		}
		key := pythonUnquotePlus(name)
		out[key] = append(out[key], pythonUnquotePlus(value))
	}
	return out
}

// pythonUnquotePlus is urllib.parse.unquote_plus: '+' becomes a space, then
// percent-decoding. Bytes that are not UTF-8 become U+FFFD with the same
// spans as Python's utf-8 codec (errors='replace').
func pythonUnquotePlus(s string) string {
	if strings.Contains(s, "+") {
		s = strings.ReplaceAll(s, "+", " ")
	}
	if !strings.Contains(s, "%") {
		return s
	}
	return pythonUTF8Replace(percentDecode([]byte(s)))
}

// percentDecode is urllib.parse._unquote_impl. A % followed by two hex
// digits is that byte. Anything else keeps the % and continues.
func percentDecode(b []byte) []byte {
	out := make([]byte, 0, len(b))
	for i := 0; i < len(b); {
		if b[i] != '%' || i+2 >= len(b) || !isHex(b[i+1]) || !isHex(b[i+2]) {
			out = append(out, b[i])
			i++
			continue
		}
		out = append(out, unhex(b[i+1])<<4|unhex(b[i+2]))
		i += 3
	}
	return out
}

func isHex(c byte) bool {
	return '0' <= c && c <= '9' || 'a' <= c && c <= 'f' || 'A' <= c && c <= 'F'
}

func unhex(c byte) byte {
	switch {
	case '0' <= c && c <= '9':
		return c - '0'
	case 'a' <= c && c <= 'f':
		return c - 'a' + 10
	default:
		return c - 'A' + 10
	}
}

// pythonUTF8Replace decodes b like bytes.decode('utf-8', 'replace').
// A well-formed sequence is that rune, including U+FFFD itself (EF BF BD).
// utf8.DecodeRune reports U+FFFD as RuneError with the full width, so a
// sequence is accepted by its width, not by comparing the rune with
// RuneError. An incomplete but well-formed prefix is one U+FFFD. A
// continuation that is outside the lead's range, or a byte that cannot
// start a sequence, is its own U+FFFD and the following bytes are decoded
// again.
func pythonUTF8Replace(b []byte) string {
	var out strings.Builder
	for i := 0; i < len(b); {
		c := b[i]
		if c < 0x80 {
			out.WriteByte(c)
			i++
			continue
		}
		need, lo, hi, ok := utf8Lead(c)
		if !ok {
			out.WriteRune(utf8.RuneError)
			i++
			continue
		}
		end := i + 1
		rejected := false
		for n := 1; n < need && end < len(b); n++ {
			cont := b[end]
			if cont < 0x80 || cont > 0xBF {
				break
			}
			if n == 1 && (int(cont) < lo || int(cont) > hi) {
				rejected = true
				break
			}
			end++
		}
		if rejected {
			out.WriteRune(utf8.RuneError)
			i++
			continue
		}
		if end-i == need {
			r, size := utf8.DecodeRune(b[i:end])
			if size == need {
				out.WriteRune(r)
				i = end
				continue
			}
			out.WriteRune(utf8.RuneError)
			i++
			continue
		}
		out.WriteRune(utf8.RuneError)
		i = end
	}
	return out.String()
}

// utf8Lead reports the length of a valid UTF-8 lead and the inclusive range
// of its first continuation byte. Leads that only encode overlongs,
// surrogates, or values past U+10FFFF are rejected so each of those bytes
// becomes its own replacement.
func utf8Lead(c byte) (need, lo, hi int, ok bool) {
	switch {
	case c >= 0xC2 && c <= 0xDF:
		return 2, 0x80, 0xBF, true
	case c == 0xE0:
		return 3, 0xA0, 0xBF, true
	case c >= 0xE1 && c <= 0xEC:
		return 3, 0x80, 0xBF, true
	case c == 0xED:
		return 3, 0x80, 0x9F, true
	case c >= 0xEE && c <= 0xEF:
		return 3, 0x80, 0xBF, true
	case c == 0xF0:
		return 4, 0x90, 0xBF, true
	case c >= 0xF1 && c <= 0xF3:
		return 4, 0x80, 0xBF, true
	case c == 0xF4:
		return 4, 0x80, 0x8F, true
	default:
		return 0, 0, 0, false
	}
}
