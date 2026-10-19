package httpapi

import (
	"errors"
	"fmt"
	"log/slog"
	"math"
	"math/big"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"github.com/foolzzz/looper/go/internal/db"
)

const (
	usageTurnsDefault   = 100
	usageTurnsMax       = 1000
	recordsLimitDefault = 100
	recordsLimitMax     = 500
	eventsLimitDefault  = 1000
	eventsLimitMax      = 5000
	// pythonIntMaxStrDigits is sys.get_int_max_str_digits(). int() counts
	// decimal digits after stripping whitespace and the sign, and ignores
	// underscores. One more digit is ValueError.
	pythonIntMaxStrDigits = 4300
)

var (
	agentUsagePattern = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	recordIDPattern   = regexp.MustCompile(`^[0-9]+$`)
)

type recordsBody struct {
	Records []map[string]any `json:"records"`
}

type recordBody struct {
	Record map[string]any `json:"record"`
}

type eventsBody struct {
	Events []map[string]any `json:"events"`
}

// rawUsageAgent is the agent id Python's usage route captures. The pattern
// is applied to the escaped path, so a percent-encoded character is not a
// letter or a digit.
func rawUsageAgent(r *http.Request) (string, bool) {
	const prefix = "/api/agents/"
	const suffix = "/usage"
	rest, ok := strings.CutPrefix(r.URL.EscapedPath(), prefix)
	if !ok || !strings.HasSuffix(rest, suffix) {
		return "", false
	}
	id := strings.TrimSuffix(rest, suffix)
	if strings.Contains(id, "/") || !agentUsagePattern.MatchString(id) {
		return "", false
	}
	return id, true
}

func rawRecordID(r *http.Request, events bool) (string, bool) {
	rest, ok := strings.CutPrefix(r.URL.EscapedPath(), "/api/llm-records/")
	if !ok || rest == "" {
		return "", false
	}
	if events {
		id, suffix, found := strings.Cut(rest, "/")
		if !found || suffix != "events" || !recordIDPattern.MatchString(id) {
			return "", false
		}
		return id, true
	}
	if strings.Contains(rest, "/") || !recordIDPattern.MatchString(rest) {
		return "", false
	}
	return rest, true
}

func (s *Server) handleAgentUsage(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	agentID, ok := rawUsageAgent(r)
	if !ok {
		notFound(w)
		return
	}
	raw := queryValue(pythonQuery(r.URL.RawQuery), "turns")
	if raw == "" {
		raw = strconv.Itoa(usageTurnsDefault)
	}
	n, overflow, ok := pythonInt(raw)
	if !ok {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: "turns must be an integer"})
		return
	}
	turns := clampTurns(n, overflow)
	ctx, cancel := readContext(r)
	defer cancel()
	if s.Reads == nil {
		databaseUnavailable(w, r, errors.New("no database"))
		return
	}
	body, err := s.Reads.AgentUsage(ctx, agentID, turns)
	if err != nil {
		databaseUnavailable(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleListRecords(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	// ServeMux matches the decoded path, so /api/llm%2drecords lands here.
	// Python keeps the escape and matches no route.
	if r.URL.EscapedPath() != "/api/llm-records" {
		notFound(w)
		return
	}
	values := pythonQuery(r.URL.RawQuery)
	limitRaw := queryValue(values, "limit")
	if limitRaw == "" {
		limitRaw = strconv.Itoa(recordsLimitDefault)
	}
	offsetRaw := queryValue(values, "offset")
	if offsetRaw == "" {
		offsetRaw = "0"
	}
	limit, limitOver, ok := pythonInt(limitRaw)
	if !ok {
		internalServerError(w, errors.New("limit is not an integer"))
		return
	}
	offset, offsetOver, ok := pythonInt(offsetRaw)
	if !ok {
		internalServerError(w, errors.New("offset is not an integer"))
		return
	}
	// A positive value past int64 is still min()'d with the cap. A negative
	// value that does not fit is what the database driver would reject, and
	// this handler answers 200 with an empty list.
	if limitOver > 0 {
		limit = recordsLimitMax
	}
	if limitOver < 0 || offsetOver != 0 {
		emptyRecords(w, errors.New("limit or offset does not fit"))
		return
	}
	if limit > recordsLimitMax {
		limit = recordsLimitMax
	}
	ctx, cancel := readContext(r)
	defer cancel()
	if s.Reads == nil {
		emptyRecords(w, errors.New("no database"))
		return
	}
	rows, err := s.Reads.ListRecords(ctx, db.RecordQuery{
		AgentID: queryValue(values, "agent_id"),
		TaskID:  queryValue(values, "task_id"),
		Role:    queryValue(values, "role"),
		Limit:   limit,
		Offset:  offset,
	})
	if err != nil {
		emptyRecords(w, err)
		return
	}
	out := make([]map[string]any, len(rows))
	for i, row := range rows {
		out[i] = withRunRole(row)
	}
	writeJSON(w, http.StatusOK, recordsBody{Records: out})
}

func (s *Server) handleGetRecord(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	raw, ok := rawRecordID(r, false)
	if !ok {
		notFound(w)
		return
	}
	s.writeRecord(w, r, raw)
}

func (s *Server) handleRecordEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		notFound(w)
		return
	}
	raw, ok := rawRecordID(r, true)
	if !ok {
		notFound(w)
		return
	}
	// The route converts the id with int() before the handler runs. A value
	// past Python's digit limit is that conversion failing, so it is 500
	// before the query is read. A value that parses and does not fit int64
	// reaches the handler, which checks after and limit first.
	id, idOver, idOK := pythonInt(raw)
	if !idOK {
		internalServerError(w, errors.New("llm record id is not an integer"))
		return
	}
	values := pythonQuery(r.URL.RawQuery)
	afterRaw := queryValue(values, "after")
	if afterRaw == "" {
		afterRaw = "0"
	}
	limitRaw := queryValue(values, "limit")
	if limitRaw == "" {
		limitRaw = strconv.Itoa(eventsLimitDefault)
	}
	after, afterOver, ok1 := pythonInt(afterRaw)
	limit, limitOver, ok2 := pythonInt(limitRaw)
	if !ok1 || !ok2 {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: "after and limit must be integers"})
		return
	}
	if idOver != 0 {
		databaseUnavailable(w, r, errors.New("llm record id out of range"))
		return
	}
	if afterOver != 0 || limitOver < 0 {
		databaseUnavailable(w, r, errors.New("after or limit does not fit"))
		return
	}
	if limitOver > 0 || limit > eventsLimitMax {
		limit = eventsLimitMax
	}
	ctx, cancel := readContext(r)
	defer cancel()
	if s.Reads == nil {
		databaseUnavailable(w, r, errors.New("no database"))
		return
	}
	rows, err := s.Reads.ListEvents(ctx, id, after, limit)
	if err != nil {
		databaseUnavailable(w, r, err)
		return
	}
	events := make([]map[string]any, len(rows))
	copy(events, rows)
	writeJSON(w, http.StatusOK, eventsBody{Events: events})
}

func (s *Server) writeRecord(w http.ResponseWriter, r *http.Request, raw string) {
	id, overflow, ok := pythonInt(raw)
	if !ok {
		internalServerError(w, errors.New("llm record id is not an integer"))
		return
	}
	if overflow != 0 {
		databaseUnavailable(w, r, errors.New("llm record id out of range"))
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	if s.Reads == nil {
		databaseUnavailable(w, r, errors.New("no database"))
		return
	}
	row, err := s.Reads.GetRecord(ctx, id)
	if errors.Is(err, db.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, errorBody{Error: fmt.Sprintf("llm record %d not found", id)})
		return
	}
	if err != nil {
		databaseUnavailable(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, recordBody{Record: withRunRole(row)})
}

func clampTurns(n int64, overflow int) int {
	if overflow > 0 || n > usageTurnsMax {
		return usageTurnsMax
	}
	if overflow < 0 || n < 1 {
		return 1
	}
	return int(n)
}

func emptyRecords(w http.ResponseWriter, err error) {
	slog.Error("llm records", "err", db.LogText(err))
	writeJSON(w, http.StatusOK, recordsBody{Records: []map[string]any{}})
}

func internalServerError(w http.ResponseWriter, err error) {
	slog.Error("http request", "err", err)
	writeJSON(w, http.StatusInternalServerError, errorBody{Error: "internal server error"})
}

// pythonInt parses s the way Python int() parses a str. overflow is 1 when
// the value is above math.MaxInt64, -1 when it is below math.MinInt64, and 0
// when n is the value. ok is false on ValueError: blank, a sign that is not
// ASCII, a trailing or doubled underscore, a non-digit, or more than
// pythonIntMaxStrDigits decimal digits. Unicode decimal digits and
// underscores between digits are accepted. The result is not clamped.
// Leading and trailing whitespace is the set int() strips. str.isspace() is
// true for U+001C–U+001F, and int() still rejects them.
func pythonInt(s string) (n int64, overflow int, ok bool) {
	s = pythonStrip(s)
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
	if !pythonDigits(s) {
		return 0, 0, false
	}
	if pythonDigitCount(s) > pythonIntMaxStrDigits {
		return 0, 0, false
	}
	mag := new(big.Int)
	if _, ok := mag.SetString(asciiDigits(strings.ReplaceAll(s, "_", "")), 10); !ok {
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

func pythonStrip(s string) string {
	return strings.TrimFunc(s, pythonSpace)
}

// pythonSpace is the whitespace Python's int() strips. It is unicode.IsSpace.
// U+001C–U+001F are spaces for str.isspace and for task names, and they are
// not spaces here: turns=%1C2 is 400 and limit=%1C2 is 500.
func pythonSpace(r rune) bool {
	return unicode.IsSpace(r)
}

// pythonDigitCount is the digit count int() compares with
// sys.get_int_max_str_digits. Underscores are not digits. The sign and
// surrounding whitespace are already gone.
func pythonDigitCount(s string) int {
	n := 0
	for _, r := range s {
		if r != '_' {
			n++
		}
	}
	return n
}

func pythonDigits(s string) bool {
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
		if _, ok := decimalDigit(r); !ok {
			return false
		}
		sawDigit = true
		prevDigit = true
	}
	return sawDigit && prevDigit
}

func asciiDigits(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		d, _ := decimalDigit(r)
		b.WriteByte(byte('0' + d))
	}
	return b.String()
}

func withRunRole(record map[string]any) map[string]any {
	if role := runRole(taskRoleRaw(record)); role != nil {
		record["task_role"] = role
		return record
	}
	delete(record, "task_role")
	return record
}

func taskRoleRaw(record map[string]any) any {
	meta, _ := record["metadata"].(map[string]any)
	if meta == nil {
		return nil
	}
	layers, _ := meta["prompt_layers"].(map[string]any)
	if layers == nil {
		return nil
	}
	return layers["task_role"]
}

// runRole is task_roles.run_role. The key is omitted when the recorded role
// is missing or not a work or review role. version is db.RoleVersion, the
// same Python int() task detail and chat targets use.
func runRole(raw any) map[string]any {
	m, ok := raw.(map[string]any)
	if !ok {
		return nil
	}
	id, hasID := m["id"]
	kind, hasKind := m["kind"]
	version, hasVersion := m["version"]
	if !hasID || !hasKind || !hasVersion {
		return nil
	}
	kindName, ok := kind.(string)
	if !ok || (kindName != "work" && kindName != "review") {
		return nil
	}
	ver, ok := db.RoleVersion(version)
	if !ok {
		return nil
	}
	text, ok := roleID(id)
	if !ok {
		return nil
	}
	return map[string]any{"id": text, "kind": kindName, "version": ver}
}

func roleID(v any) (string, bool) {
	switch t := v.(type) {
	case string:
		return t, true
	case bool:
		if t {
			return "True", true
		}
		return "False", true
	case int:
		return strconv.Itoa(t), true
	case int32:
		return strconv.FormatInt(int64(t), 10), true
	case int64:
		return strconv.FormatInt(t, 10), true
	case float64:
		if math.IsNaN(t) {
			return "nan", true
		}
		if math.IsInf(t, 1) {
			return "inf", true
		}
		if math.IsInf(t, -1) {
			return "-inf", true
		}
		if t == math.Trunc(t) && t >= math.MinInt64 && t <= math.MaxInt64 {
			return strconv.FormatInt(int64(t), 10), true
		}
		return strconv.FormatFloat(t, 'f', -1, 64), true
	default:
		return "", false
	}
}
