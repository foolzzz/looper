// Package pyjson writes JSON the way Python json.dumps does with
// ensure_ascii=False: UTF-8, ", " and ": " separators, no HTML escaping,
// no trailing newline, and Python's number spelling.
//
// Object keeps the order of its fields. A Go map has no insertion order, so
// a non-empty map is an error. An empty map is {}. Use Object for a response
// whose keys are not a struct.
//
// Struct fields are emitted in declaration order. json names are honored and
// json:"-" is skipped. omitempty is ignored, so a zero still appears.
// []byte is refused (Python would not base64 it). time.Time is refused
// because Python's isoformat is not RFC3339. Invalid UTF-8 bytes in a string
// become U+FFFD. A cycle through a pointer, a slice, or an Object is an
// error. The encoder does not panic on a bad value.
package pyjson

import (
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// ContentType is the Content-Type of a Python _json_response body.
const ContentType = "application/json; charset=utf-8"

// Field is one object member, written in the order it appears in Object.
type Field struct {
	Key   string
	Value any
}

// Object is a JSON object with Python insertion order.
// A nil Object is {} , the same as an empty one. A nil interface is null.
type Object []Field

var (
	timeType   = reflect.TypeOf(time.Time{})
	objectType = reflect.TypeOf(Object(nil))
	numberType = reflect.TypeOf(json.Number(""))
)

// visit is one container currently being encoded. Pointers use n -1.
// Slices and Objects use the data pointer and the length, so a value may
// contain a shorter subslice of itself. Type distinguishes a struct pointer
// from a pointer to its first field, which can have the same address.
type visit struct {
	ptr uintptr
	n   int
	typ reflect.Type
}

// Marshal returns the Python json.dumps encoding of v.
func Marshal(v any) ([]byte, error) {
	var s state
	if err := s.appendValue(v); err != nil {
		return nil, err
	}
	return s.buf, nil
}

type state struct {
	buf  []byte
	seen map[visit]struct{}
}

// enter records a container. n == 0 is an empty slice or Object: it has no
// elements, so it cannot contain itself and is not recorded.
func (s *state) enter(ptr uintptr, n int, typ reflect.Type) error {
	if n == 0 {
		return nil
	}
	if s.seen == nil {
		s.seen = make(map[visit]struct{})
	}
	v := visit{ptr: ptr, n: n, typ: typ}
	if _, ok := s.seen[v]; ok {
		return fmt.Errorf("python json: cycle")
	}
	s.seen[v] = struct{}{}
	return nil
}

func (s *state) leave(ptr uintptr, n int, typ reflect.Type) {
	if n == 0 || s.seen == nil {
		return
	}
	delete(s.seen, visit{ptr: ptr, n: n, typ: typ})
}

func (s *state) appendValue(v any) error {
	if v == nil {
		s.buf = append(s.buf, "null"...)
		return nil
	}
	switch x := v.(type) {
	case Object:
		return s.appendObject(x)
	case json.Number:
		return s.appendNumberLiteral(string(x))
	}
	return s.appendReflect(reflect.ValueOf(v))
}

func (s *state) appendObject(obj Object) error {
	n := len(obj)
	var ptr uintptr
	if n > 0 {
		ptr = reflect.ValueOf(obj).Pointer()
	}
	if err := s.enter(ptr, n, objectType); err != nil {
		return err
	}
	defer s.leave(ptr, n, objectType)
	s.buf = append(s.buf, '{')
	for i, f := range obj {
		if i > 0 {
			s.buf = append(s.buf, ',', ' ')
		}
		s.appendString(f.Key)
		s.buf = append(s.buf, ':', ' ')
		if err := s.appendValue(f.Value); err != nil {
			return err
		}
	}
	s.buf = append(s.buf, '}')
	return nil
}

func (s *state) appendReflect(v reflect.Value) error {
	for {
		switch v.Kind() {
		case reflect.Interface:
			if v.IsNil() {
				s.buf = append(s.buf, "null"...)
				return nil
			}
			v = v.Elem()
			continue
		case reflect.Pointer:
			if v.IsNil() {
				s.buf = append(s.buf, "null"...)
				return nil
			}
			ptr := v.Pointer()
			if err := s.enter(ptr, -1, v.Type()); err != nil {
				return err
			}
			defer s.leave(ptr, -1, v.Type())
			v = v.Elem()
			continue
		}
		break
	}
	// Object and json.Number may sit behind an interface, a pointer, a
	// slice element, or a struct field. The kind switch would encode an
	// Object as an array of Key/Value records and a json.Number as a string.
	if v.IsValid() && v.CanInterface() {
		switch v.Type() {
		case objectType:
			return s.appendObject(v.Interface().(Object))
		case numberType:
			return s.appendNumberLiteral(string(v.Interface().(json.Number)))
		}
	}
	switch v.Kind() {
	case reflect.Bool:
		if v.Bool() {
			s.buf = append(s.buf, "true"...)
		} else {
			s.buf = append(s.buf, "false"...)
		}
		return nil
	case reflect.String:
		s.appendString(v.String())
		return nil
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		s.buf = strconv.AppendInt(s.buf, v.Int(), 10)
		return nil
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		s.buf = strconv.AppendUint(s.buf, v.Uint(), 10)
		return nil
	case reflect.Float32, reflect.Float64:
		text, err := formatFloat(v.Float())
		if err != nil {
			return err
		}
		s.buf = append(s.buf, text...)
		return nil
	case reflect.Map:
		return s.appendMap(v)
	case reflect.Slice:
		return s.appendSlice(v)
	case reflect.Array:
		return s.appendArray(v)
	case reflect.Struct:
		if v.Type() == timeType {
			return fmt.Errorf("python json: time.Time is not encoded")
		}
		return s.appendStruct(v)
	default:
		return fmt.Errorf("python json: unsupported type %s", v.Type())
	}
}

func (s *state) appendMap(v reflect.Value) error {
	if v.IsNil() {
		s.buf = append(s.buf, "null"...)
		return nil
	}
	if v.Len() == 0 {
		s.buf = append(s.buf, '{', '}')
		return nil
	}
	return fmt.Errorf("python json: map has no insertion order; use pyjson.Object")
}

func (s *state) appendSlice(v reflect.Value) error {
	// encoding/json base64-encodes []byte. Python json.dumps does not.
	if v.Type().Elem().Kind() == reflect.Uint8 {
		return fmt.Errorf("python json: %s is not encoded", v.Type())
	}
	if v.IsNil() {
		s.buf = append(s.buf, "null"...)
		return nil
	}
	n := v.Len()
	var ptr uintptr
	if n > 0 {
		ptr = v.Pointer()
	}
	if err := s.enter(ptr, n, v.Type()); err != nil {
		return err
	}
	defer s.leave(ptr, n, v.Type())
	return s.appendSequence(v)
}

func (s *state) appendArray(v reflect.Value) error {
	return s.appendSequence(v)
}

func (s *state) appendSequence(v reflect.Value) error {
	s.buf = append(s.buf, '[')
	for i := 0; i < v.Len(); i++ {
		if i > 0 {
			s.buf = append(s.buf, ',', ' ')
		}
		if err := s.appendReflect(v.Index(i)); err != nil {
			return err
		}
	}
	s.buf = append(s.buf, ']')
	return nil
}

func (s *state) appendStruct(v reflect.Value) error {
	t := v.Type()
	s.buf = append(s.buf, '{')
	wrote := false
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if f.PkgPath != "" {
			continue
		}
		name, skip := jsonFieldName(f)
		if skip {
			continue
		}
		if wrote {
			s.buf = append(s.buf, ',', ' ')
		}
		wrote = true
		s.appendString(name)
		s.buf = append(s.buf, ':', ' ')
		if err := s.appendReflect(v.Field(i)); err != nil {
			return err
		}
	}
	s.buf = append(s.buf, '}')
	return nil
}

// jsonFieldName returns the object key. omitempty and other options are
// ignored so a zero value is still written. "-" skips the field.
func jsonFieldName(f reflect.StructField) (string, bool) {
	tag, ok := f.Tag.Lookup("json")
	if !ok {
		return f.Name, false
	}
	if tag == "-" {
		return "", true
	}
	name, _, _ := strings.Cut(tag, ",")
	if name == "" {
		return f.Name, false
	}
	return name, false
}

func (s *state) appendString(str string) {
	s.buf = append(s.buf, '"')
	for i := 0; i < len(str); {
		r, size := utf8.DecodeRuneInString(str[i:])
		if r == utf8.RuneError && size == 1 {
			s.buf = utf8.AppendRune(s.buf, utf8.RuneError)
			i++
			continue
		}
		s.appendRune(r)
		i += size
	}
	s.buf = append(s.buf, '"')
}

func (s *state) appendRune(r rune) {
	switch r {
	case '"', '\\':
		s.buf = append(s.buf, '\\', byte(r))
	case '\b':
		s.buf = append(s.buf, '\\', 'b')
	case '\f':
		s.buf = append(s.buf, '\\', 'f')
	case '\n':
		s.buf = append(s.buf, '\\', 'n')
	case '\r':
		s.buf = append(s.buf, '\\', 'r')
	case '\t':
		s.buf = append(s.buf, '\\', 't')
	default:
		if r < 0x20 {
			s.buf = fmt.Appendf(s.buf, `\u%04x`, r)
			return
		}
		s.buf = utf8.AppendRune(s.buf, r)
	}
}

func (s *state) appendNumberLiteral(n string) error {
	switch n {
	case "NaN", "Infinity", "-Infinity":
		s.buf = append(s.buf, n...)
		return nil
	}
	if !validNumberLiteral(n) {
		return fmt.Errorf("python json: json.Number %q", n)
	}
	s.buf = append(s.buf, n...)
	return nil
}

func validNumberLiteral(s string) bool {
	i := 0
	if i < len(s) && s[i] == '-' {
		i++
	}
	if i >= len(s) {
		return false
	}
	if s[i] == '0' {
		i++
	} else if s[i] >= '1' && s[i] <= '9' {
		for i < len(s) && s[i] >= '0' && s[i] <= '9' {
			i++
		}
	} else {
		return false
	}
	if i < len(s) && s[i] == '.' {
		i++
		start := i
		for i < len(s) && s[i] >= '0' && s[i] <= '9' {
			i++
		}
		if i == start {
			return false
		}
	}
	if i < len(s) && (s[i] == 'e' || s[i] == 'E') {
		i++
		if i < len(s) && (s[i] == '+' || s[i] == '-') {
			i++
		}
		start := i
		for i < len(s) && s[i] >= '0' && s[i] <= '9' {
			i++
		}
		if i == start {
			return false
		}
	}
	return i == len(s)
}

// formatFloat is Python's float repr, which json.dumps uses.
// Shortest digits come from strconv. Python keeps fixed notation until the
// normalized exponent is <= -5 or >= 16, and a whole number keeps a trailing
// ".0". Go's 'g' switches to scientific at exponent 6 and omits ".0".
func formatFloat(f float64) (string, error) {
	switch {
	case math.IsNaN(f):
		return "NaN", nil
	case math.IsInf(f, 1):
		return "Infinity", nil
	case math.IsInf(f, -1):
		return "-Infinity", nil
	}
	if f == 0 {
		if math.Signbit(f) {
			return "-0.0", nil
		}
		return "0.0", nil
	}
	sci := strconv.FormatFloat(f, 'e', -1, 64)
	exp, err := scientificExponent(sci)
	if err != nil {
		return "", err
	}
	if exp <= -5 || exp >= 16 {
		return sci, nil
	}
	return expandScientific(sci)
}

func scientificExponent(sci string) (int, error) {
	i := strings.IndexByte(sci, 'e')
	if i < 0 || i+1 >= len(sci) {
		return 0, fmt.Errorf("python json: float %q", sci)
	}
	exp, err := strconv.Atoi(sci[i+1:])
	if err != nil {
		return 0, fmt.Errorf("python json: float %q: %w", sci, err)
	}
	return exp, nil
}

func expandScientific(sci string) (string, error) {
	i := strings.IndexByte(sci, 'e')
	if i < 0 {
		return "", fmt.Errorf("python json: float %q", sci)
	}
	exp, err := strconv.Atoi(sci[i+1:])
	if err != nil {
		return "", fmt.Errorf("python json: float %q: %w", sci, err)
	}
	mant := sci[:i]
	neg := false
	if strings.HasPrefix(mant, "-") {
		neg = true
		mant = mant[1:]
	} else if strings.HasPrefix(mant, "+") {
		mant = mant[1:]
	}
	dot := strings.IndexByte(mant, '.')
	digits := strings.ReplaceAll(mant, ".", "")
	if digits == "" {
		return "", fmt.Errorf("python json: float %q", sci)
	}
	point := 1
	if dot >= 0 {
		point = dot
	}
	newPoint := point + exp
	var b strings.Builder
	if neg {
		b.WriteByte('-')
	}
	switch {
	case newPoint <= 0:
		b.WriteString("0.")
		for n := 0; n < -newPoint; n++ {
			b.WriteByte('0')
		}
		b.WriteString(strings.TrimRight(digits, "0"))
	case newPoint >= len(digits):
		b.WriteString(digits)
		for n := 0; n < newPoint-len(digits); n++ {
			b.WriteByte('0')
		}
		b.WriteString(".0")
	default:
		b.WriteString(digits[:newPoint])
		b.WriteByte('.')
		frac := strings.TrimRight(digits[newPoint:], "0")
		if frac == "" {
			b.WriteByte('0')
		} else {
			b.WriteString(frac)
		}
	}
	return b.String(), nil
}
