package httpapi

import "testing"

func TestPythonQuery(t *testing.T) {
	tests := []struct {
		raw  string
		key  string
		want string
		miss bool
	}{
		{raw: "limit=1;", key: "limit", want: "1;"},
		{raw: "group=completed;foo=bar", key: "group", want: "completed;foo=bar"},
		{raw: "limit=1;&group=needs_you", key: "limit", want: "1;"},
		{raw: "limit=%", key: "limit", want: "%"},
		{raw: "limit=%ZZ", key: "limit", want: "%ZZ"},
		{raw: "limit=%2", key: "limit", want: "%2"},
		{raw: "limit=%2G", key: "limit", want: "%2G"},
		{raw: "limit=%%41", key: "limit", want: "%A"},
		{raw: "limit=%2%41", key: "limit", want: "%2A"},
		{raw: "limit=%FF", key: "limit", want: "\uFFFD"},
		{raw: "limit=%FF%FF", key: "limit", want: "\uFFFD\uFFFD"},
		{raw: "before=%EF%BF%BD", key: "before", want: "\uFFFD"},
		{raw: "before=%EF%BF%BDX", key: "before", want: "\uFFFDX"},
		{raw: "limit=%C3%A9", key: "limit", want: "é"},
		{raw: "limit=%E2%82%AC", key: "limit", want: "€"},
		{raw: "limit=%E2%82", key: "limit", want: "\uFFFD"},
		{raw: "limit=+", key: "limit", want: " "},
		{raw: "limit=+1", key: "limit", want: " 1"},
		{raw: "limit=%2b1", key: "limit", want: "+1"},
		{raw: "limit=1%3B", key: "limit", want: "1;"},
		{raw: "limit=", key: "limit", miss: true},
		{raw: "group=ready&group=", key: "group", want: "ready"},
		{raw: "limit=1&limit=2", key: "limit", want: "2"},
	}
	for _, tt := range tests {
		t.Run(tt.raw, func(t *testing.T) {
			got := queryValue(pythonQuery(tt.raw), tt.key)
			if tt.miss {
				if got != "" {
					t.Fatalf("got %q, want the key dropped", got)
				}
				return
			}
			if got != tt.want {
				t.Fatalf("got %q, want %q", got, tt.want)
			}
		})
	}
	both := pythonQuery("limit=1;&group=needs_you")
	if queryValue(both, "group") != "needs_you" {
		t.Fatalf("group %q", queryValue(both, "group"))
	}
}

func TestPythonUTF8Replace(t *testing.T) {
	tests := []struct{ in, want string }{
		{"\xff", "\uFFFD"},
		{"\xff\xff", "\uFFFD\uFFFD"},
		{"\xe2\x82", "\uFFFD"},
		{"\xe2\x82A", "\uFFFDA"},
		{"\xe2A", "\uFFFDA"},
		{"\xe2\x82\xac", "€"},
		{"\xc3", "\uFFFD"},
		{"\xc3(", "\uFFFD("},
		{"\xc3\xa9", "é"},
		{"\x80", "\uFFFD"},
		{"\x80\x80", "\uFFFD\uFFFD"},
		{"\xc0\x80", "\uFFFD\uFFFD"},
		{"\xed\xa0\x80", "\uFFFD\uFFFD\uFFFD"},
		{"\xed\x80", "\uFFFD"},
		{"\xf0\x9f\x98", "\uFFFD"},
		{"\xf0\x9f\x98\x80", "😀"},
		{"\xf0(", "\uFFFD("},
		{"\xf0\x9f(", "\uFFFD("},
		{"\xf0\x9f\x98(", "\uFFFD("},
		{"\xe0\x80\x80", "\uFFFD\uFFFD\uFFFD"},
		{"\xf5\x80\x80\x80", "\uFFFD\uFFFD\uFFFD\uFFFD"},
		{"\xed\xa0", "\uFFFD\uFFFD"},
		{"\xf4\x90\x80\x80", "\uFFFD\uFFFD\uFFFD\uFFFD"},
		{"\xe0\xa0\x80", "\u0800"},
		{"a\xe2\x82b", "a\uFFFDb"},
		{"\xe2\x82\x82\x82", "\u2082\uFFFD"},
		// EF BF BD is U+FFFD, one rune. Rejecting it as RuneError and
		// resuming at the next byte turns it into three replacements.
		{"\xef\xbf\xbd", "\uFFFD"},
		{"\xef\xbf\xbd\xef\xbf\xbd", "\uFFFD\uFFFD"},
		{"\xef\xbf\xbd\xff", "\uFFFD\uFFFD"},
		{"\xef\xbf\xbdX", "\uFFFDX"},
	}
	for _, tt := range tests {
		t.Run(tt.want, func(t *testing.T) {
			if got := pythonUTF8Replace([]byte(tt.in)); got != tt.want {
				t.Fatalf("got %q, want %q", got, tt.want)
			}
		})
	}
}
