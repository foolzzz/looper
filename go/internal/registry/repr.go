package registry

import (
	"fmt"
	"strconv"
	"strings"
)

// pyRepr is CPython repr for a str. cost_warnings quotes the model and the
// provider with it. This is the same spelling as db.pyRepr.
func pyRepr(s string) string {
	quote := '\''
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteRune(quote)
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r == quote {
				b.WriteByte('\\')
				b.WriteRune(quote)
				continue
			}
			if r < 32 || r == 127 || (r < 0x100 && !strconv.IsPrint(r)) {
				fmt.Fprintf(&b, `\x%02x`, r)
				continue
			}
			if strconv.IsPrint(r) {
				b.WriteRune(r)
				continue
			}
			if r <= 0xFFFF {
				fmt.Fprintf(&b, `\u%04x`, r)
				continue
			}
			fmt.Fprintf(&b, `\U%08x`, r)
		}
	}
	b.WriteRune(quote)
	return b.String()
}
