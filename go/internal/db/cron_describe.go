package db

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// cronRule is the part of looper/agents/cron.py a schedule view needs:
// parse and describe. Fire-time search stays with the scheduler.
type cronRule struct {
	text       string
	minutes    []int
	hours      []int
	days       map[int]bool
	months     map[int]bool
	weekdays   map[int]bool
	anyDay     bool
	anyWeekday bool
}

func describeCron(ruleText, zoneName string) string {
	rule, err := parseCron(ruleText)
	if err != nil {
		return ruleText + " (" + zoneName + ")"
	}
	return describeRule(rule, zoneName)
}

func parseCron(text string) (cronRule, error) {
	parts := strings.Fields(text)
	if len(parts) != 5 {
		return cronRule{}, fmt.Errorf("a cron rule has 5 fields, got %d", len(parts))
	}
	fields := []struct {
		name      string
		low, high int
	}{
		{"minute", 0, 59},
		{"hour", 0, 23},
		{"day of the month", 1, 31},
		{"month", 1, 12},
		{"day of the week", 0, 7},
	}
	values := make([]map[int]bool, 5)
	for i, part := range parts {
		set, err := cronField(part, fields[i].name, fields[i].low, fields[i].high)
		if err != nil {
			return cronRule{}, err
		}
		values[i] = set
	}
	weekdays := map[int]bool{}
	for day := range values[4] {
		weekdays[((day%7)+7)%7] = true
	}
	return cronRule{
		text:       strings.Join(parts, " "),
		minutes:    sortedKeys(values[0]),
		hours:      sortedKeys(values[1]),
		days:       values[2],
		months:     values[3],
		weekdays:   weekdays,
		anyDay:     strings.HasPrefix(parts[2], "*"),
		anyWeekday: strings.HasPrefix(parts[4], "*"),
	}, nil
}

func cronField(text, name string, low, high int) (map[int]bool, error) {
	values := map[int]bool{}
	for _, item := range strings.Split(text, ",") {
		base, stepText, hasStep := strings.Cut(item, "/")
		step := 1
		if hasStep {
			n, err := cronNumber(stepText, name, item)
			if err != nil {
				return nil, err
			}
			step = n
		}
		if step < 1 {
			return nil, fmt.Errorf("the %s step in %q must be at least 1", name, item)
		}
		var start, end int
		switch {
		case base == "*":
			start, end = low, high
		case strings.Contains(base, "-"):
			first, last, _ := strings.Cut(base, "-")
			var err error
			start, err = cronNumber(first, name, item)
			if err != nil {
				return nil, err
			}
			end, err = cronNumber(last, name, item)
			if err != nil {
				return nil, err
			}
			if start > end {
				return nil, fmt.Errorf("the %s range %q runs backwards", name, item)
			}
		default:
			n, err := cronNumber(base, name, item)
			if err != nil {
				return nil, err
			}
			start = n
			end = n
			if hasStep {
				end = high
			}
		}
		if start < low || start > high || end < low || end > high {
			return nil, fmt.Errorf("the %s is %d-%d; %q is outside it", name, low, high, item)
		}
		for value := start; value <= end; {
			values[value] = true
			// A step of MaxInt64 wraps on the next addition. The wrapped
			// value is still <= end, so the set gains negative minutes.
			if step > end-value {
				break
			}
			value += step
		}
	}
	return values, nil
}

func cronNumber(text, name, item string) (int, error) {
	if text == "" || strings.Trim(text, "0123456789") != "" {
		return 0, fmt.Errorf("%q is not a valid %s", item, name)
	}
	n, err := strconv.Atoi(text)
	if err != nil {
		return 0, fmt.Errorf("%q is not a valid %s", item, name)
	}
	return n, nil
}

func describeRule(rule cronRule, zoneName string) string {
	raw := rule.text + " (" + zoneName + ")"
	if !intSetEqual(rule.months, intRange(1, 12)) {
		return raw
	}
	leading, trailing, ok := daysPhrase(rule)
	if !ok {
		return raw
	}
	if every, ok := everyPhrase(rule); ok {
		if trailing == "" {
			return every
		}
		return every + " " + trailing + " (" + zoneName + ")"
	}
	times := cronTimes(rule)
	if times == nil {
		return raw
	}
	return leading + " at " + cronJoin(times) + " (" + zoneName + ")"
}

func daysPhrase(rule cronRule) (leading, trailing string, ok bool) {
	if !rule.anyDay && !rule.anyWeekday {
		return "", "", false
	}
	if intSetEqual(rule.weekdays, intRange(0, 6)) {
		if intSetEqual(rule.days, intRange(1, 31)) {
			return "Daily", "", true
		}
		if len(rule.days) > 4 {
			return "", "", false
		}
		noun := "days"
		if len(rule.days) == 1 {
			noun = "day"
		}
		days := cronJoin(intStrings(sortedKeys(rule.days)))
		return "Monthly on " + noun + " " + days, "on " + noun + " " + days + " of each month", true
	}
	if !intSetEqual(rule.days, intRange(1, 31)) {
		return "", "", false
	}
	if intSetEqual(rule.weekdays, intRange(1, 5)) {
		return "Weekdays", "on weekdays", true
	}
	if intSetEqual(rule.weekdays, map[int]bool{0: true, 6: true}) {
		return "Weekends", "on weekends", true
	}
	names := make([]string, 0, len(rule.weekdays))
	for _, day := range sortedKeys(rule.weekdays) {
		names = append(names, weekdayName(day)+"s")
	}
	joined := cronJoin(names)
	return joined, "on " + joined, true
}

func everyPhrase(rule cronRule) (string, bool) {
	if intSliceEqual(rule.hours, seq(0, 23)) {
		if step, ok := stepFromZero(rule.minutes, 60); ok {
			if step == 1 {
				return "Every minute", true
			}
			return fmt.Sprintf("Every %d minutes", step), true
		}
	}
	if len(rule.minutes) == 1 {
		if step, ok := stepFromZero(rule.hours, 24); ok {
			at := fmt.Sprintf(":%02d", rule.minutes[0])
			if step == 1 {
				return "Every hour at " + at, true
			}
			return fmt.Sprintf("Every %d hours at %s", step, at), true
		}
	}
	return "", false
}

func stepFromZero(values []int, size int) (int, bool) {
	if len(values) < 2 || values[0] != 0 || values[1] == 0 || size%values[1] != 0 {
		return 0, false
	}
	if !intSliceEqual(values, seqStep(0, size-1, values[1])) {
		return 0, false
	}
	return values[1], true
}

func cronTimes(rule cronRule) []string {
	if len(rule.minutes)*len(rule.hours) > 4 {
		return nil
	}
	out := make([]string, 0, len(rule.minutes)*len(rule.hours))
	for _, hour := range rule.hours {
		for _, minute := range rule.minutes {
			out = append(out, fmt.Sprintf("%02d:%02d", hour, minute))
		}
	}
	return out
}

func cronJoin(items []string) string {
	if len(items) == 0 {
		return ""
	}
	if len(items) == 1 {
		return items[0]
	}
	return strings.Join(items[:len(items)-1], ", ") + " and " + items[len(items)-1]
}

func weekdayName(day int) string {
	names := []string{"Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"}
	if day < 0 || day >= len(names) {
		return ""
	}
	return names[day]
}

func sortedKeys(set map[int]bool) []int {
	out := make([]int, 0, len(set))
	for n := range set {
		out = append(out, n)
	}
	for i := 1; i < len(out); i++ {
		j := i
		for j > 0 && out[j] < out[j-1] {
			out[j], out[j-1] = out[j-1], out[j]
			j--
		}
	}
	return out
}

func intRange(low, high int) map[int]bool {
	out := make(map[int]bool, high-low+1)
	for n := low; n <= high; n++ {
		out[n] = true
	}
	return out
}

func intSetEqual(a, b map[int]bool) bool {
	if len(a) != len(b) {
		return false
	}
	for n := range a {
		if !b[n] {
			return false
		}
	}
	return true
}

func intSliceEqual(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func seq(low, high int) []int {
	return seqStep(low, high, 1)
}

func seqStep(low, high, step int) []int {
	if step < 1 {
		return nil
	}
	out := []int{}
	for n := low; n <= high; n += step {
		out = append(out, n)
	}
	return out
}

func intStrings(ns []int) []string {
	out := make([]string, len(ns))
	for i, n := range ns {
		out[i] = strconv.Itoa(n)
	}
	return out
}

// localZoneName is cron.local_zone_name: TZ, else the zoneinfo suffix of
// /etc/localtime, else /etc/timezone, else UTC. A name time.LoadLocation
// rejects is skipped, which is what zone() does for an unknown IANA name.
func localZoneName() string {
	candidates := []string{strings.TrimPrefix(os.Getenv("TZ"), ":")}
	if link, err := os.Readlink("/etc/localtime"); err == nil {
		const marker = "zoneinfo/"
		if i := strings.Index(link, marker); i >= 0 {
			candidates = append(candidates, link[i+len(marker):])
		} else {
			candidates = append(candidates, "")
		}
	}
	if raw, err := os.ReadFile("/etc/timezone"); err == nil {
		candidates = append(candidates, strings.TrimSpace(string(raw)))
	}
	for _, name := range candidates {
		if name == "" {
			continue
		}
		if _, err := time.LoadLocation(name); err == nil {
			return name
		}
	}
	return "UTC"
}
