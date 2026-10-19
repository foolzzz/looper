package httpapi

import (
	"regexp"
	"strconv"
	"time"
)

// sinceCalendar, sinceCompact, and the week patterns are the forms
// datetime.date.fromisoformat accepts on Python 3.14 for a calendar day and
// an ISO week. Ordinal dates, a time, a sign, and a lowercase "w" are not
// among them. A week or a day that is out of range is the same refusal.
var (
	sinceCalendar = regexp.MustCompile(`^(\d{4})-(\d{2})-(\d{2})$`)
	sinceCompact  = regexp.MustCompile(`^(\d{4})(\d{2})(\d{2})$`)
	sinceWeekDay  = regexp.MustCompile(`^(\d{4})-W(\d{2})-(\d)$`)
	sinceWeek     = regexp.MustCompile(`^(\d{4})-W(\d{2})$`)
	sinceWeekCDay = regexp.MustCompile(`^(\d{4})W(\d{2})(\d)$`)
	sinceWeekC    = regexp.MustCompile(`^(\d{4})W(\d{2})$`)
)

// parseSinceDay is date.fromisoformat. The result is that calendar day at
// UTC midnight. The caller builds local midnight from the year, month, and
// day; time.Date is not used to validate, because it would turn 31 February
// into March.
func parseSinceDay(s string) (time.Time, bool) {
	y, m, d, ok := sinceYMD(s)
	if !ok {
		return time.Time{}, false
	}
	return time.Date(y, time.Month(m), d, 0, 0, 0, 0, time.UTC), true
}

func sinceYMD(s string) (year, month, day int, ok bool) {
	if y, m, d, ok := matchedCalendar(sinceCalendar, s); ok {
		return y, m, d, true
	}
	if y, m, d, ok := matchedCalendar(sinceCompact, s); ok {
		return y, m, d, true
	}
	if y, w, d, ok := matchedWeek(sinceWeekDay, s, true); ok {
		return isoWeekDate(y, w, d)
	}
	if y, w, _, ok := matchedWeek(sinceWeek, s, false); ok {
		return isoWeekDate(y, w, 1)
	}
	if y, w, d, ok := matchedWeek(sinceWeekCDay, s, true); ok {
		return isoWeekDate(y, w, d)
	}
	if y, w, _, ok := matchedWeek(sinceWeekC, s, false); ok {
		return isoWeekDate(y, w, 1)
	}
	return 0, 0, 0, false
}

func matchedCalendar(re *regexp.Regexp, s string) (year, month, day int, ok bool) {
	m := re.FindStringSubmatch(s)
	if m == nil {
		return 0, 0, 0, false
	}
	y, yok := atoi(m[1])
	mo, mok := atoi(m[2])
	d, dok := atoi(m[3])
	if !yok || !mok || !dok || !realDay(y, mo, d) {
		return 0, 0, 0, false
	}
	return y, mo, d, true
}

func matchedWeek(re *regexp.Regexp, s string, withDay bool) (year, week, day int, ok bool) {
	m := re.FindStringSubmatch(s)
	if m == nil {
		return 0, 0, 0, false
	}
	y, yok := atoi(m[1])
	w, wok := atoi(m[2])
	if !yok || !wok {
		return 0, 0, 0, false
	}
	d := 1
	if withDay {
		var dok bool
		d, dok = atoi(m[3])
		if !dok || d < 1 || d > 7 {
			return 0, 0, 0, false
		}
	}
	return y, w, d, true
}

func atoi(s string) (int, bool) {
	n, err := strconv.Atoi(s)
	return n, err == nil
}

func realDay(year, month, day int) bool {
	if year < 1 || year > 9999 || month < 1 || month > 12 || day < 1 {
		return false
	}
	return day <= daysInMonth(year, month)
}

func daysInMonth(year, month int) int {
	switch month {
	case 1, 3, 5, 7, 8, 10, 12:
		return 31
	case 4, 6, 9, 11:
		return 30
	case 2:
		if leapYear(year) {
			return 29
		}
		return 28
	default:
		return 0
	}
}

func leapYear(year int) bool {
	return year%4 == 0 && (year%100 != 0 || year%400 == 0)
}

// isoWeekDate is the calendar day of an ISO week. Week 1 is the week of
// 4 January. The Thursday of the week has to fall in that ISO year, and the
// calendar year of the day has to be inside 1..9999. 9999-W52-7 is the
// Sunday whose year would be 10000, which date.fromisoformat rejects.
func isoWeekDate(year, week, day int) (int, int, int, bool) {
	if year < 1 || year > 9999 || week < 1 || week > 53 || day < 1 || day > 7 {
		return 0, 0, 0, false
	}
	jan4 := time.Date(year, time.January, 4, 0, 0, 0, 0, time.UTC)
	wd := int(jan4.Weekday())
	if wd == 0 {
		wd = 7
	}
	monday := jan4.AddDate(0, 0, 1-wd)
	thursday := monday.AddDate(0, 0, (week-1)*7+3)
	if thursday.Year() != year {
		return 0, 0, 0, false
	}
	result := monday.AddDate(0, 0, (week-1)*7+(day-1))
	y, m, d := result.Date()
	if y < 1 || y > 9999 {
		return 0, 0, 0, false
	}
	return y, int(m), d, true
}
