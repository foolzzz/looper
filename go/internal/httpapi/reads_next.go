package httpapi

import (
	"errors"
	"math/big"
	"net/http"
	"regexp"
	"unicode"
	"unicode/utf8"

	"github.com/foolzzz/looper/go/internal/db"
)

// These paths are the Python router's. ServeMux decodes a percent-encoded
// segment before matching, so each handler compares EscapedPath with the
// same expression. A segment that matches the mux and not the expression is
// 404 and does not read the database. POST on either path is 404: the gate
// message write and the room write are not this card.
var (
	reGateMessages = regexp.MustCompile(`^/api/tasks/(task-[a-zA-Z0-9]+)/gates/([A-Za-z0-9][A-Za-z0-9_.-]{0,31})/messages$`)
	reTaskRoom     = regexp.MustCompile(`^/api/tasks/(task-[a-zA-Z0-9]+)/room$`)
)

func (s *Server) handleGateMessages(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	match := reGateMessages.FindStringSubmatch(r.URL.EscapedPath())
	if match == nil {
		notFound(w)
		return
	}
	if s.Reads == nil {
		requestFailed(w, "gate messages", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.GateMessages(ctx, match[1], match[2])
	if errors.Is(err, db.ErrGateNotFound) {
		writeJSON(w, http.StatusNotFound, errorBody{Error: "task " + match[1] + " has no gate " + match[2]})
		return
	}
	if err != nil {
		requestFailed(w, "gate messages", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleTaskRoom(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	match := reTaskRoom.FindStringSubmatch(r.URL.EscapedPath())
	if match == nil {
		notFound(w)
		return
	}
	// str.isdigit then int() run before the task is loaded. A non-digit is
	// 400 even when the task is missing and even when there is no database.
	// isdigit accepts a superscript or a circled digit. int() raises
	// ValueError for those, and for more than 4,300 decimal digits. The
	// request loop answers 500, and the digit text is not logged.
	q, status, err := roomQuery(pythonQuery(r.URL.RawQuery))
	if status == http.StatusBadRequest {
		writeJSON(w, status, errorBody{Error: err.Error()})
		return
	}
	if status != 0 {
		internalServerError(w, err)
		return
	}
	if s.Reads == nil {
		requestFailed(w, "task room", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.TaskRoom(ctx, match[1], q)
	if errors.Is(err, db.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, errorBody{Error: "task not found"})
		return
	}
	if err != nil {
		requestFailed(w, "task room", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

// roomQuery is _task_room's number() plus the kind, from, and unanswered
// filters. Blank values are already gone. The last value of a key wins.
func roomQuery(values map[string][]string) (db.RoomQuery, int, error) {
	q := db.RoomQuery{Limit: db.RoomViewLimit}
	if raw := queryValue(values, "after"); raw != "" {
		n, tooBig, status, err := roomNumber("after", raw)
		if status != 0 {
			return db.RoomQuery{}, status, err
		}
		q.After = n
		q.AfterTooBig = tooBig
	}
	if raw := queryValue(values, "limit"); raw != "" {
		n, tooBig, status, err := roomNumber("limit", raw)
		if status != 0 {
			return db.RoomQuery{}, status, err
		}
		if tooBig || n > int64(db.RoomViewLimit) {
			q.Limit = db.RoomViewLimit
		} else if n < 1 {
			q.Limit = 1
		} else {
			q.Limit = int(n)
		}
	}
	q.Kind = queryValue(values, "kind")
	q.Sender = queryValue(values, "from")
	switch queryValue(values, "unanswered") {
	case "1", "true":
		q.Unanswered = true
	}
	return q, 0, nil
}

// roomNumber is str.isdigit and then int(). A sign, an underscore, a space,
// or a number such as ⑩ fails isdigit and is 400. A superscript or a circled
// digit passes isdigit. int() accepts only decimal digits, so those raise
// ValueError and the request loop answers 500. Past 4,300 decimal digits,
// int() raises the same way. The raw text stays out of the log.
func roomNumber(name, raw string) (n int64, tooBig bool, status int, err error) {
	if !pythonIsDigit(raw) {
		return 0, false, http.StatusBadRequest, errors.New(name + " must be a whole number")
	}
	if !pythonDecimalDigits(raw) {
		return 0, false, http.StatusInternalServerError, errors.New(name + " is not a decimal integer")
	}
	if utf8.RuneCountInString(raw) > pythonIntMaxStrDigits {
		return 0, false, http.StatusInternalServerError, errors.New(name + " exceeds the digit limit")
	}
	mag := new(big.Int)
	if _, ok := mag.SetString(asciiDigits(raw), 10); !ok {
		return 0, false, http.StatusInternalServerError, errors.New(name + " is not a decimal integer")
	}
	if !mag.IsInt64() {
		return 0, true, 0, nil
	}
	return mag.Int64(), false, 0, nil
}

// pythonIsDigit is str.isdigit: every character is Numeric_Type Decimal or
// Digit, and the string is not empty. Decimal is Unicode 14 Nd, the set
// CPython 3.11 uses. Digit is the compatibility numbers in
// pythonNumericDigits, which int() rejects.
func pythonIsDigit(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !pythonDecimalRune(r) && !unicode.Is(pythonNumericDigits, r) {
			return false
		}
	}
	return true
}

// pythonDecimalDigits is the character set int() accepts after isdigit has
// passed: Unicode 14 decimal digits only, with no sign and no underscore.
func pythonDecimalDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if !pythonDecimalRune(r) {
			return false
		}
	}
	return true
}

// pythonDecimalRune is a decimal digit in Unicode 14.0, which is CPython
// 3.11's unicodedata. Go 1.24's unicode.IsDigit follows Unicode 15.0 and
// also accepts Kawi (U+11F50–U+11F59) and Nag Mundari (U+1E4F0–U+1E4F9).
// Those are not digits in the reference interpreter, so ?limit=%F0%9E%93%B2
// (U+1E4F2) is 400 there. These two blocks are the whole difference.
func pythonDecimalRune(r rune) bool {
	if (r >= 0x11F50 && r <= 0x11F59) || (r >= 0x1E4F0 && r <= 0x1E4F9) {
		return false
	}
	return unicode.IsDigit(r)
}

// pythonNumericDigits is Unicode Numeric_Type=Digit, the non-decimal
// characters for which str.isdigit is true. int() raises ValueError on each
// of them. CPython 3.11 (Unicode 14) and 3.14 list the same 128 characters.
var pythonNumericDigits = &unicode.RangeTable{
	R16: []unicode.Range16{
		{0x00B2, 0x00B3, 1}, // ² ³
		{0x00B9, 0x00B9, 1}, // ¹
		{0x1369, 0x1371, 1}, // Ethiopic digits
		{0x19DA, 0x19DA, 1}, // New Tai Lue Tham digit
		{0x2070, 0x2070, 1}, // ⁰
		{0x2074, 0x2079, 1}, // ⁴..⁹
		{0x2080, 0x2089, 1}, // subscripts
		{0x2460, 0x2468, 1}, // circled digits 1..9
		{0x2474, 0x247C, 1}, // parenthesized digits
		{0x2488, 0x2490, 1}, // digit full stop
		{0x24EA, 0x24EA, 1}, // circled digit 0
		{0x24F5, 0x24FD, 1}, // double circled digits
		{0x24FF, 0x24FF, 1}, // negative circled digit 0
		{0x2776, 0x277E, 1}, // dingbat negative circled digits
		{0x2780, 0x2788, 1}, // dingbat circled sans-serif digits
		{0x278A, 0x2792, 1}, // dingbat negative circled sans-serif digits
	},
	R32: []unicode.Range32{
		{0x10A40, 0x10A43, 1}, // Kharoshthi digits
		{0x10E60, 0x10E68, 1}, // Rumi digits
		{0x11052, 0x1105A, 1}, // Brahmi numbers
		{0x1F100, 0x1F10A, 1}, // digit full stop and comma
	},
	LatinOffset: 2,
}
