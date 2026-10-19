package httpapi

import (
	"errors"
	"math/big"
	"net/http"
	"regexp"
	"unicode/utf8"

	"github.com/foolzzz/looper/go/internal/db"
)

// Python's router. A percent-encoded segment matches the mux and not these
// expressions, and is 404 without a database read. GET /api/schedules/preview
// is a POST route; preview does not match sched-.
var (
	reProjectSchedules      = regexp.MustCompile(`^/api/projects/(proj-[a-zA-Z0-9]+)/schedules$`)
	reSchedule              = regexp.MustCompile(`^/api/schedules/(sched-[a-zA-Z0-9]+)$`)
	reScheduleRuns          = regexp.MustCompile(`^/api/schedules/(sched-[a-zA-Z0-9]+)/runs$`)
	reScheduleNotifications = regexp.MustCompile(`^/api/schedules/(sched-[a-zA-Z0-9]+)/notifications$`)
)

func (s *Server) handleProjectSchedules(w http.ResponseWriter, r *http.Request) {
	m := reProjectSchedules.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	id := m[1]
	if s.Reads == nil {
		requestFailed(w, "project schedules", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.ListProjectSchedules(ctx, id)
	if errors.Is(err, db.ErrProjectNotFound) {
		missingProject(w, id)
		return
	}
	if err != nil {
		requestFailed(w, "project schedules", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleSchedules(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet || r.URL.EscapedPath() != "/api/schedules" {
		notFound(w)
		return
	}
	if s.Reads == nil {
		requestFailed(w, "schedules", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.ListSchedules(ctx)
	if err != nil {
		requestFailed(w, "schedules", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleSchedule(w http.ResponseWriter, r *http.Request) {
	m := reSchedule.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	id := m[1]
	if s.Reads == nil {
		requestFailed(w, "schedule", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.GetSchedule(ctx, id)
	if errors.Is(err, db.ErrScheduleNotFound) {
		missingSchedule(w, id)
		return
	}
	if err != nil {
		requestFailed(w, "schedule", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleScheduleRuns(w http.ResponseWriter, r *http.Request) {
	m := reScheduleRuns.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	id := m[1]
	query := pythonQuery(r.URL.RawQuery)
	limit, status, err := scheduleLimit(queryValue(query, "limit"))
	if err != nil {
		if status == http.StatusInternalServerError {
			internalServerError(w, err)
			return
		}
		writeJSON(w, status, errorBody{Error: err.Error()})
		return
	}
	hide, err := scheduleSkips(queryValue(query, "skips"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody{Error: err.Error()})
		return
	}
	if s.Reads == nil {
		requestFailed(w, "schedule runs", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.ListScheduleRuns(ctx, id, db.ScheduleRunsQuery{
		Limit:     limit,
		Before:    queryValue(query, "before"),
		HideSkips: hide,
	})
	if errors.Is(err, db.ErrScheduleNotFound) {
		missingSchedule(w, id)
		return
	}
	var statusErr *db.StatusError
	if errors.As(err, &statusErr) {
		writeJSON(w, statusErr.Code, errorBody{Error: statusErr.Text})
		return
	}
	if err != nil {
		requestFailed(w, "schedule runs", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func (s *Server) handleScheduleNotifications(w http.ResponseWriter, r *http.Request) {
	m := reScheduleNotifications.FindStringSubmatch(r.URL.EscapedPath())
	if r.Method != http.MethodGet || m == nil {
		notFound(w)
		return
	}
	id := m[1]
	limit, status, err := scheduleLimit(queryValue(pythonQuery(r.URL.RawQuery), "limit"))
	if err != nil {
		if status == http.StatusInternalServerError {
			internalServerError(w, err)
			return
		}
		writeJSON(w, status, errorBody{Error: err.Error()})
		return
	}
	if s.Reads == nil {
		requestFailed(w, "schedule notifications", errNoDatabase)
		return
	}
	ctx, cancel := readContext(r)
	defer cancel()
	body, err := s.Reads.ListScheduleNotifications(ctx, id, limit)
	if errors.Is(err, db.ErrScheduleNotFound) {
		missingSchedule(w, id)
		return
	}
	if err != nil {
		requestFailed(w, "schedule notifications", err)
		return
	}
	writeJSON(w, http.StatusOK, body)
}

func missingSchedule(w http.ResponseWriter, id string) {
	writeJSON(w, http.StatusNotFound, errorBody{Error: "schedule " + pythonRepr(id) + " not found"})
}

// scheduleLimit is list_runs' limit. An empty value is 50. str.isdigit must
// pass, then int(), then 1..200. A digit int() rejects (a superscript, a
// circled digit) is 500 and does not read the database. More than 4,300
// digits is that 500 too. A decimal integer outside 1..200 is 400, including
// one of exactly 4,300 digits.
func scheduleLimit(raw string) (int, int, error) {
	if raw == "" {
		return 50, 0, nil
	}
	if !pythonIsDigit(raw) {
		return 0, http.StatusBadRequest, errors.New("limit must be a whole number from 1 to 200")
	}
	if !pythonDecimalDigits(raw) || utf8.RuneCountInString(raw) > pythonIntMaxStrDigits {
		return 0, http.StatusInternalServerError, errors.New("limit is not a decimal integer")
	}
	mag := new(big.Int)
	if _, ok := mag.SetString(asciiDigits(raw), 10); !ok {
		return 0, http.StatusInternalServerError, errors.New("limit is not a decimal integer")
	}
	if mag.Cmp(big.NewInt(1)) < 0 || mag.Cmp(big.NewInt(200)) > 0 {
		return 0, http.StatusBadRequest, errors.New("limit must be a whole number from 1 to 200")
	}
	return int(mag.Int64()), 0, nil
}

// scheduleSkips is SkipRows. An empty value is show. The error names show
// then hide, which is the enum order.
func scheduleSkips(raw string) (bool, error) {
	if raw == "" || raw == "show" {
		return false, nil
	}
	if raw == "hide" {
		return true, nil
	}
	return false, errors.New("skips must be one of show, hide")
}
