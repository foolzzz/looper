package operatorauth

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"unicode/utf8"

	"github.com/foolzzz/looper/go/internal/pyjson"
	"golang.org/x/crypto/scrypt"
	"golang.org/x/sys/unix"
)

const (
	// PasswordFile is the private scrypt record under the runtime root.
	PasswordFile = "operator-password"
	// PasswordMinChars is the minimum password length in Unicode code points.
	PasswordMinChars = 8

	scryptN      = 32768
	scryptR      = 8
	scryptP      = 1
	scryptLen    = 32
	scryptMaxMem = 64 * 1024 * 1024
	saltLen      = 16
)

var userPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// PasswordRecord is looper.agents.operator_auth.PasswordRecord.
// Salt and Hash are the raw bytes, not the base64 text.
type PasswordRecord struct {
	User string
	N    int
	R    int
	P    int
	Salt []byte
	Hash []byte
}

// InputError is Python's ValueError for a user name or password that
// set_password refuses before it touches the runtime root.
type InputError struct {
	Msg string
}

func (e *InputError) Error() string {
	if e == nil {
		return ""
	}
	return e.Msg
}

// deriveKey is hashlib.scrypt. Tests replace it to count calls.
var deriveKey = scryptDerive

func scryptDerive(password string, salt []byte, n, r, p int) ([]byte, error) {
	if err := scryptAllowed(n, r, p); err != nil {
		return nil, err
	}
	sum, err := scrypt.Key([]byte(password), salt, n, r, p, scryptLen)
	if err != nil {
		return nil, fmt.Errorf("scrypt: %w", err)
	}
	return sum, nil
}

// scryptAllowed is the OpenSSL 3 gate inside hashlib.scrypt before it allocates.
// golang.org/x/crypto/scrypt has no maxmem argument, so this runs first.
// Memory is 128*r*(N+2+p) and must be <= 64 MiB. When 16*r <= 63, N must be
// below 1<<(16*r); that is why N=65536 and r=1 is refused even though the
// byte count is under the cap.
func scryptAllowed(n, r, p int) error {
	if n < 2 || n&(n-1) != 0 || r < 1 || p < 1 {
		return errors.New("scrypt: invalid parameters")
	}
	if uint64(p) > (uint64(1<<30)-1)/uint64(r) {
		return errors.New("scrypt: invalid parameters")
	}
	if 16*r <= 63 && uint64(n) >= uint64(1)<<uint(16*r) {
		return errors.New("scrypt: parameters exceed the 64 MiB OpenSSL limit")
	}
	rr := uint64(r)
	sum := uint64(n) + 2 + uint64(p)
	if sum < uint64(n) {
		return errors.New("scrypt: parameters exceed the 64 MiB OpenSSL limit")
	}
	prod := sum * rr
	if prod/rr != sum {
		return errors.New("scrypt: parameters exceed the 64 MiB OpenSSL limit")
	}
	mem := prod * 128
	if mem/128 != prod || mem > scryptMaxMem {
		return errors.New("scrypt: parameters exceed the 64 MiB OpenSSL limit")
	}
	return nil
}

// SetPassword writes operator-password. A rejected user or password does not
// create the runtime root. *Error and *InputError are returned unwrapped.
func SetPassword(ctx context.Context, runtimeRoot, user, password string) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("set operator password: %w", err)
	}
	if !userPattern.MatchString(user) {
		return &InputError{Msg: "user name must contain 1 to 64 letters, digits, dots, underscores or hyphens"}
	}
	if len([]rune(password)) < PasswordMinChars {
		return &InputError{Msg: fmt.Sprintf("password must contain at least %d characters", PasswordMinChars)}
	}
	salt := make([]byte, saltLen)
	if _, err := rand.Read(salt); err != nil {
		return fmt.Errorf("set operator password: salt: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("set operator password: %w", err)
	}
	sum, err := deriveKey(password, salt, scryptN, scryptR, scryptP)
	if err != nil {
		return fmt.Errorf("set operator password: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("set operator password: %w", err)
	}
	body, err := pyjson.Marshal(pyjson.Object{
		{Key: "user", Value: user},
		{Key: "scheme", Value: "scrypt"},
		{Key: "n", Value: scryptN},
		{Key: "r", Value: scryptR},
		{Key: "p", Value: scryptP},
		{Key: "salt", Value: base64.URLEncoding.EncodeToString(salt)},
		{Key: "hash", Value: base64.URLEncoding.EncodeToString(sum)},
	})
	if err != nil {
		return fmt.Errorf("set operator password: %w", err)
	}
	body = append(body, '\n')
	path := filepath.Join(runtimeRoot, PasswordFile)
	dirfd, err := openRoot(runtimeRoot)
	if err != nil {
		return err
	}
	defer unix.Close(dirfd)
	if err := writePrivate(dirfd, PasswordFile, body); err != nil {
		return &Error{Msg: fmt.Sprintf("cannot write operator credential %s: %s; %s", path, err, remedy(path))}
	}
	return nil
}

// ClearPassword removes operator-password. A missing file is false, nil.
// The runtime root is created, matching Python. Other unlink errors are wrapped.
func ClearPassword(ctx context.Context, runtimeRoot string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, fmt.Errorf("clear operator password: %w", err)
	}
	dirfd, err := openRoot(runtimeRoot)
	if err != nil {
		return false, err
	}
	defer unix.Close(dirfd)
	err = unix.Unlinkat(dirfd, PasswordFile, 0)
	if errors.Is(err, unix.ENOENT) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("clear operator password: %w", err)
	}
	return true, nil
}

// LoadPassword reads operator-password. A missing file is nil, nil.
// A record that does not validate is *Error, unwrapped. The root is created.
func LoadPassword(ctx context.Context, runtimeRoot string) (*PasswordRecord, error) {
	if err := ctx.Err(); err != nil {
		return nil, fmt.Errorf("load operator password: %w", err)
	}
	path := filepath.Join(runtimeRoot, PasswordFile)
	dirfd, err := openRoot(runtimeRoot)
	if err != nil {
		return nil, err
	}
	defer unix.Close(dirfd)
	data, err := readPrivate(dirfd, PasswordFile, path)
	if err != nil {
		return nil, err
	}
	if data == nil {
		return nil, nil
	}
	rec, err := parsePassword(data)
	if err != nil {
		return nil, &Error{Msg: fmt.Sprintf("operator credential %s is not a password record; %s", path, remedy(path))}
	}
	return rec, nil
}

func parsePassword(data []byte) (*PasswordRecord, error) {
	// json.loads on bytes decodes the whole buffer before it parses.
	// encoding/json would replace an invalid UTF-8 byte inside a string and
	// would reject a BOM or a UTF-16 record. Match the Python decode first.
	text, err := pythonJSONText(data)
	if err != nil {
		return nil, errBadRecord
	}
	// json.loads accepts NaN, Infinity, and -Infinity. encoding/json rejects
	// those tokens. An ignored field may hold one; a required integer still
	// fails the int check because the token becomes null.
	text = replacePythonJSONConstants(text)
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	var raw map[string]any
	if err := dec.Decode(&raw); err != nil || raw == nil {
		return nil, errBadRecord
	}
	var extra any
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		return nil, errBadRecord
	}
	user, userOK := raw["user"].(string)
	scheme, schemeOK := raw["scheme"].(string)
	n, nOK := jsonInt(raw["n"])
	r, rOK := jsonInt(raw["r"])
	p, pOK := jsonInt(raw["p"])
	if !userOK || !schemeOK || !nOK || !rOK || !pOK || scheme != "scrypt" || !userPattern.MatchString(user) {
		return nil, errBadRecord
	}
	if n < 1<<14 || n > 1<<20 || n&(n-1) != 0 || r < 1 || r > 32 || p < 1 || p > 32 {
		return nil, errBadRecord
	}
	salt, saltOK := decodeSecret(raw["salt"], saltLen)
	sum, hashOK := decodeSecret(raw["hash"], scryptLen)
	if !saltOK || !hashOK {
		return nil, errBadRecord
	}
	return &PasswordRecord{User: user, N: n, R: r, P: p, Salt: salt, Hash: sum}, nil
}

var errBadRecord = errors.New("not a password record")

// pythonJSONText is json.loads' byte decode: detect_encoding, then
// str.decode(encoding, "surrogatepass"). A surrogate code point becomes
// U+FFFD. Password fields are ASCII, so that substitution does not change
// an accepted record. An illegal byte still fails the whole buffer.
func pythonJSONText(data []byte) (string, error) {
	switch detectPythonJSONEncoding(data) {
	case "utf-8":
		return decodeUTF8SurrogatePass(data)
	case "utf-8-sig":
		if bytes.HasPrefix(data, []byte{0xEF, 0xBB, 0xBF}) {
			data = data[3:]
		}
		return decodeUTF8SurrogatePass(data)
	case "utf-16":
		if bytes.HasPrefix(data, []byte{0xFF, 0xFE}) {
			return decodeUTF16(data[2:], binary.LittleEndian)
		}
		if bytes.HasPrefix(data, []byte{0xFE, 0xFF}) {
			return decodeUTF16(data[2:], binary.BigEndian)
		}
		return "", errBadRecord
	case "utf-16-le":
		return decodeUTF16(data, binary.LittleEndian)
	case "utf-16-be":
		return decodeUTF16(data, binary.BigEndian)
	case "utf-32":
		if bytes.HasPrefix(data, []byte{0xFF, 0xFE, 0x00, 0x00}) {
			return decodeUTF32(data[4:], binary.LittleEndian)
		}
		if bytes.HasPrefix(data, []byte{0x00, 0x00, 0xFE, 0xFF}) {
			return decodeUTF32(data[4:], binary.BigEndian)
		}
		return "", errBadRecord
	case "utf-32-le":
		return decodeUTF32(data, binary.LittleEndian)
	case "utf-32-be":
		return decodeUTF32(data, binary.BigEndian)
	default:
		return "", errBadRecord
	}
}

// replacePythonJSONConstants rewrites json.loads' non-standard number tokens
// to null. Strings are copied through, including a NaN inside quotes.
// NaNfoo and +Infinity stay, and the decoder rejects them.
func replacePythonJSONConstants(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); {
		if s[i] == '"' {
			j := skipJSONString(s, i)
			b.WriteString(s[i:j])
			i = j
			continue
		}
		if n := pythonConstantToken(s, i); n > 0 {
			b.WriteString("null")
			i += n
			continue
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

func skipJSONString(s string, i int) int {
	j := i + 1
	for j < len(s) {
		switch s[j] {
		case '\\':
			if j+1 >= len(s) {
				return len(s)
			}
			j += 2
		case '"':
			return j + 1
		default:
			j++
		}
	}
	return len(s)
}

func pythonConstantToken(s string, i int) int {
	if i > 0 && !pythonValueStart(s[i-1]) {
		return 0
	}
	rest := s[i:]
	tok := ""
	switch {
	case strings.HasPrefix(rest, "-Infinity"):
		tok = "-Infinity"
	case strings.HasPrefix(rest, "Infinity"):
		tok = "Infinity"
	case strings.HasPrefix(rest, "NaN"):
		tok = "NaN"
	default:
		return 0
	}
	end := i + len(tok)
	if end < len(s) && !pythonValueEnd(s[end]) {
		return 0
	}
	return len(tok)
}

func pythonValueStart(c byte) bool {
	switch c {
	case ' ', '\t', '\n', '\r', ':', ',', '[', '{':
		return true
	default:
		return false
	}
}

func pythonValueEnd(c byte) bool {
	switch c {
	case ' ', '\t', '\n', '\r', ',', '}', ']':
		return true
	default:
		return false
	}
}

// detectPythonJSONEncoding is json.decoder.detect_encoding.
func detectPythonJSONEncoding(b []byte) string {
	if bytes.HasPrefix(b, []byte{0x00, 0x00, 0xFE, 0xFF}) || bytes.HasPrefix(b, []byte{0xFF, 0xFE, 0x00, 0x00}) {
		return "utf-32"
	}
	if bytes.HasPrefix(b, []byte{0xFE, 0xFF}) || bytes.HasPrefix(b, []byte{0xFF, 0xFE}) {
		return "utf-16"
	}
	if bytes.HasPrefix(b, []byte{0xEF, 0xBB, 0xBF}) {
		return "utf-8-sig"
	}
	if len(b) >= 4 {
		if b[0] == 0 {
			if b[1] != 0 {
				return "utf-16-be"
			}
			return "utf-32-be"
		}
		if b[1] == 0 {
			if b[2] != 0 || b[3] != 0 {
				return "utf-16-le"
			}
			return "utf-32-le"
		}
	} else if len(b) == 2 {
		if b[0] == 0 {
			return "utf-16-be"
		}
		if b[1] == 0 {
			return "utf-16-le"
		}
	}
	return "utf-8"
}

func decodeUTF8SurrogatePass(data []byte) (string, error) {
	var b strings.Builder
	b.Grow(len(data))
	for i := 0; i < len(data); {
		if utf8Surrogate(data[i:]) {
			b.WriteRune(utf8.RuneError)
			i += 3
			continue
		}
		r, size := utf8.DecodeRune(data[i:])
		if r == utf8.RuneError && size == 1 {
			return "", errBadRecord
		}
		b.WriteRune(r)
		i += size
	}
	return b.String(), nil
}

// utf8Surrogate is a 3-byte UTF-8 encoding of U+D800 through U+DFFF.
// Python's surrogatepass error handler accepts these and rejects other
// illegal sequences.
func utf8Surrogate(data []byte) bool {
	return len(data) >= 3 && data[0] == 0xED && data[1] >= 0xA0 && data[1] <= 0xBF && data[2]&0xC0 == 0x80
}

func decodeUTF16(data []byte, order binary.ByteOrder) (string, error) {
	if len(data)%2 != 0 {
		return "", errBadRecord
	}
	var b strings.Builder
	b.Grow(len(data) / 2)
	for i := 0; i < len(data); i += 2 {
		u := order.Uint16(data[i:])
		if u >= 0xD800 && u <= 0xDBFF && i+4 <= len(data) {
			u2 := order.Uint16(data[i+2:])
			if u2 >= 0xDC00 && u2 <= 0xDFFF {
				r := 0x10000 + (rune(u-0xD800) << 10) + rune(u2-0xDC00)
				b.WriteRune(r)
				i += 2
				continue
			}
		}
		if u >= 0xD800 && u <= 0xDFFF {
			b.WriteRune(utf8.RuneError)
			continue
		}
		b.WriteRune(rune(u))
	}
	return b.String(), nil
}

func decodeUTF32(data []byte, order binary.ByteOrder) (string, error) {
	if len(data)%4 != 0 {
		return "", errBadRecord
	}
	var b strings.Builder
	b.Grow(len(data) / 4)
	for i := 0; i < len(data); i += 4 {
		u := order.Uint32(data[i:])
		if u > unicodeMax || (u >= 0xD800 && u <= 0xDFFF) {
			if u >= 0xD800 && u <= 0xDFFF {
				b.WriteRune(utf8.RuneError)
				continue
			}
			return "", errBadRecord
		}
		b.WriteRune(rune(u))
	}
	return b.String(), nil
}

const unicodeMax = 0x10FFFF

func jsonInt(v any) (int, bool) {
	num, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	i, err := strconv.ParseInt(string(num), 10, 0)
	if err != nil {
		return 0, false
	}
	return int(i), true
}

// decodeSecret accepts Python's base64.b64decode(..., altchars=b"-_", validate=True).
func decodeSecret(v any, n int) ([]byte, bool) {
	s, ok := v.(string)
	if !ok {
		return nil, false
	}
	out, err := pythonB64Decode(s)
	if err != nil || len(out) != n {
		return nil, false
	}
	return out, true
}

// pythonB64Decode is base64.b64decode(s, altchars=b"-_", validate=True).
// encoding/base64.Strict still skips CR and LF, which this rejects, and it
// rejects nonzero leftover padding bits, which this keeps. Python decodes
// AAAAAAAAAAAAAAAAAAAAAB== as 16 zero bytes. A complete quantum (data length
// divisible by 4) may be followed by any run of '='. Those bytes are ignored.
// A short quantum allows only its own padding and nothing after it.
func pythonB64Decode(s string) ([]byte, error) {
	if strings.IndexFunc(s, func(r rune) bool { return r > 127 }) >= 0 {
		return nil, errBadB64
	}
	enc := strings.NewReplacer("-", "+", "_", "/").Replace(s)
	for i := 0; i < len(enc); i++ {
		c := enc[i]
		if c == '=' || c == '+' || c == '/' || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') {
			continue
		}
		return nil, errBadB64
	}
	payload := enc
	if cut := strings.IndexByte(enc, '='); cut >= 0 {
		pad := enc[cut:]
		if strings.TrimRight(pad, "=") != "" {
			return nil, errBadB64
		}
		switch cut % 4 {
		case 0:
			if cut == 0 {
				return nil, errBadB64
			}
			payload = enc[:cut]
		case 1:
			return nil, errBadB64
		case 2:
			if pad != "==" {
				return nil, errBadB64
			}
		case 3:
			if pad != "=" {
				return nil, errBadB64
			}
		}
	} else if len(enc)%4 != 0 {
		return nil, errBadB64
	}
	out, err := base64.StdEncoding.DecodeString(payload)
	if err != nil {
		return nil, errBadB64
	}
	return out, nil
}

var errBadB64 = errors.New("bad base64")

// Verify derives the scrypt digest even when user does not match, then
// compares the user and the digest. A parameter the OpenSSL gate refuses is
// an error. A mismatch is false, nil.
func Verify(ctx context.Context, record *PasswordRecord, user, password string) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, fmt.Errorf("verify operator password: %w", err)
	}
	if record == nil {
		return false, fmt.Errorf("verify operator password: nil record")
	}
	digest, err := deriveKey(password, record.Salt, record.N, record.R, record.P)
	if err != nil {
		return false, fmt.Errorf("verify operator password: %w", err)
	}
	// Derivation does not watch ctx. A cancel during the KDF must not
	// authenticate, including when the digest would have matched.
	if err := ctx.Err(); err != nil {
		return false, fmt.Errorf("verify operator password: %w", err)
	}
	userMatch := subtle.ConstantTimeCompare([]byte(user), []byte(record.User)) == 1
	hashMatch := subtle.ConstantTimeCompare(digest, record.Hash) == 1
	return userMatch && hashMatch, nil
}

// readPassword is LoadPassword. The cache tests replace it to count loads.
var readPassword = LoadPassword

// passwordLstat is unix.Lstat. The cache test reports a different owner.
var passwordLstat = unix.Lstat

type passwordKey struct {
	ino   uint64
	size  int64
	mtime int64
	mode  uint32
	uid   uint32
}

// PasswordCache is http_server._password_record. A missing file does not
// create the runtime root. The same fingerprint returns the cached record or
// the cached *Error without reading again. A stat error other than a missing
// file leaves the cache unchanged.
type PasswordCache struct {
	mu   sync.Mutex
	root string
	has  bool
	key  passwordKey
	rec  *PasswordRecord
	bad  *Error
}

// NewPasswordCache caches LoadPassword for runtimeRoot.
func NewPasswordCache(runtimeRoot string) *PasswordCache {
	return &PasswordCache{root: runtimeRoot}
}

// Record returns the current password record. A cancelled context is reported
// before the file is stated and does not create the runtime root.
func (c *PasswordCache) Record(ctx context.Context) (*PasswordRecord, error) {
	if err := ctx.Err(); err != nil {
		return nil, fmt.Errorf("operator password cache: %w", err)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	path := filepath.Join(c.root, PasswordFile)
	var st unix.Stat_t
	err := passwordLstat(path, &st)
	if errors.Is(err, unix.ENOENT) {
		c.has = false
		c.rec = nil
		c.bad = nil
		return nil, nil
	}
	if err != nil {
		return nil, &Error{Msg: "cannot stat password file: " + err.Error()}
	}
	key := passwordKey{
		ino:   st.Ino,
		size:  st.Size,
		mtime: st.Mtim.Nano(),
		mode:  fileMode(&st),
		uid:   st.Uid,
	}
	if c.has && key == c.key {
		if c.bad != nil {
			return nil, c.bad
		}
		return c.rec, nil
	}
	rec, err := readPassword(ctx, c.root)
	if err != nil {
		var op *Error
		if !errors.As(err, &op) {
			return nil, err
		}
		c.rec = nil
		c.bad = op
		c.has = true
		c.key = key
		slog.Error(op.Error())
		return nil, op
	}
	c.rec = rec
	c.bad = nil
	c.has = true
	c.key = key
	return rec, nil
}
