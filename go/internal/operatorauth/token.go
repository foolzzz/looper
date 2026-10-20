// Package operatorauth is the operator credential for looper serve.
// The token file and the access checks follow looper/agents/operator_auth.py.
// Password records, sessions, and the login limiter are not in this package.
package operatorauth

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"unicode/utf8"

	"golang.org/x/sys/unix"
)

const (
	// TokenFile is the private file under the runtime root.
	TokenFile = "operator-token"
	// Refusal is the 401 body when no operator credential was presented.
	Refusal = "this API answers the operator only: open the dashboard in a browser on the server's machine " +
		"(it signs in on load), or sign in at /login with the user name and password set by " +
		"looper auth set-password; the looper CLI sends the token itself"
	// Wrong is the 401 body when a credential was presented and did not match.
	Wrong = "the operator credential does not match this server's: reload the dashboard, or read the token again"
)

// Error is an unusable credential file. Error() is the remedy text.
type Error struct {
	Msg string
}

func (e *Error) Error() string {
	if e == nil {
		return ""
	}
	return e.Msg
}

// currentUID and statFile are seams for tests that cannot chown.
var (
	currentUID = os.Getuid
	statFile   = unix.Fstat
)

var tokenPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{32,}$`)

// EnsureToken reads the private token, creating it without replacing a
// concurrent server's token. A cancelled context is reported before the
// directory is created. *Error is returned unwrapped so the remedy stays exact.
func EnsureToken(ctx context.Context, runtimeRoot string) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", fmt.Errorf("operator token: %w", err)
	}
	path := filepath.Join(runtimeRoot, TokenFile)
	dirfd, err := openRoot(runtimeRoot)
	if err != nil {
		return "", err
	}
	defer unix.Close(dirfd)

	data, err := readPrivate(dirfd, TokenFile, path)
	if err != nil {
		return "", err
	}
	if data == nil {
		if err := createLinkedToken(dirfd); err != nil {
			return "", credentialError(path, err)
		}
		data, err = readPrivate(dirfd, TokenFile, path)
		if err != nil {
			return "", err
		}
	}
	token := ""
	if data != nil {
		token = strings.TrimSpace(string(data))
	}
	if !tokenPattern.MatchString(token) {
		return "", &Error{Msg: "operator credential is not a token; " + remedy(path)}
	}
	return token, nil
}

// ReadToken reads a client's token without creating or validating it.
// A missing file, a read error, or text that is not UTF-8 is ("", nil),
// matching Python None from read_text(encoding="utf-8"). A cancelled context
// is an error and does not look at the file.
func ReadToken(ctx context.Context, runtimeRoot string) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", fmt.Errorf("operator token: %w", err)
	}
	b, err := os.ReadFile(filepath.Join(runtimeRoot, TokenFile))
	if err != nil {
		return "", nil
	}
	if !utf8.Valid(b) {
		return "", nil
	}
	return strings.TrimSpace(string(b)), nil
}

func openRoot(runtimeRoot string) (int, error) {
	if err := os.MkdirAll(runtimeRoot, 0o700); err != nil {
		return -1, &Error{Msg: fmt.Sprintf("cannot open runtime root %s: %s", runtimeRoot, err)}
	}
	fd, err := unix.Open(runtimeRoot, unix.O_RDONLY|unix.O_DIRECTORY, 0)
	if err != nil {
		return -1, &Error{Msg: fmt.Sprintf("cannot open runtime root %s: %s", runtimeRoot, err)}
	}
	var st unix.Stat_t
	if err := statFile(fd, &st); err != nil {
		unix.Close(fd)
		return -1, &Error{Msg: fmt.Sprintf("cannot open runtime root %s: %s", runtimeRoot, err)}
	}
	if int(st.Uid) != currentUID() {
		unix.Close(fd)
		return -1, &Error{Msg: fmt.Sprintf("the runtime root %s is owned by uid %d, not you", runtimeRoot, st.Uid)}
	}
	if fileMode(&st)&0o022 != 0 {
		unix.Close(fd)
		return -1, &Error{Msg: fmt.Sprintf("the runtime root %s must be owned by you and not writable by group or others (chmod go-w %s)", runtimeRoot, runtimeRoot)}
	}
	return fd, nil
}

func readPrivate(dirfd int, name, path string) ([]byte, error) {
	kind := "password record"
	if name == TokenFile {
		kind = "token"
	}
	fix := remedy(path)
	fd, err := unix.Openat(dirfd, name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	if err != nil {
		if errors.Is(err, unix.ENOENT) {
			return nil, nil
		}
		return nil, credentialError(path, err)
	}
	defer unix.Close(fd)

	var st unix.Stat_t
	if err := statFile(fd, &st); err != nil {
		return nil, credentialError(path, err)
	}
	mode := fileMode(&st)
	if mode&unix.S_IFMT != unix.S_IFREG {
		return nil, &Error{Msg: fmt.Sprintf("operator credential %s is not a regular file; %s", path, fix)}
	}
	if int(st.Uid) != currentUID() {
		return nil, &Error{Msg: fmt.Sprintf("operator credential %s is owned by uid %d, not you; %s", path, st.Uid, fix)}
	}
	if mode&0o7777 != 0o600 {
		return nil, &Error{Msg: fmt.Sprintf("operator credential %s must have mode 0600 (chmod 600 %s)", path, path)}
	}
	if st.Size > 4096 {
		return nil, &Error{Msg: fmt.Sprintf("operator credential is not a %s; %s", kind, fix)}
	}
	data := make([]byte, 0, 64)
	for len(data) <= 4096 {
		chunk := make([]byte, 4097-len(data))
		n, err := unix.Read(fd, chunk)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil {
			return nil, credentialError(path, err)
		}
		if n == 0 {
			return data, nil
		}
		data = append(data, chunk[:n]...)
	}
	return nil, &Error{Msg: fmt.Sprintf("operator credential is not a %s; %s", kind, fix)}
}

// writePrivate replaces name with data through a private temp in the same
// directory. The deferred unlink names that temp, not name: after rename the
// temp is already gone, and unlinking name would delete the credential.
func writePrivate(dirfd int, name string, data []byte) error {
	tmp, err := tempName(name)
	if err != nil {
		return err
	}
	fd, err := unix.Openat(dirfd, tmp, unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	writeErr := writeAll(fd, data)
	if writeErr == nil {
		writeErr = unix.Fsync(fd)
	}
	closeErr := unix.Close(fd)
	if writeErr == nil {
		writeErr = closeErr
	}
	if writeErr != nil {
		_ = unix.Unlinkat(dirfd, tmp, 0)
		return writeErr
	}
	if err := unix.Renameat(dirfd, tmp, dirfd, name); err != nil {
		_ = unix.Unlinkat(dirfd, tmp, 0)
		return err
	}
	if err := unix.Unlinkat(dirfd, tmp, 0); err != nil && !errors.Is(err, unix.ENOENT) {
		return err
	}
	return nil
}

func createLinkedToken(dirfd int) error {
	name, err := tempName(TokenFile)
	if err != nil {
		return err
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return err
	}
	body := append([]byte(base64.RawURLEncoding.EncodeToString(raw)), '\n')
	if err := writePrivate(dirfd, name, body); err != nil {
		_ = unix.Unlinkat(dirfd, name, 0)
		return err
	}
	linkErr := unix.Linkat(dirfd, name, dirfd, TokenFile, 0)
	unlinkErr := unix.Unlinkat(dirfd, name, 0)
	if linkErr != nil && !errors.Is(linkErr, unix.EEXIST) {
		return linkErr
	}
	if unlinkErr != nil && !errors.Is(unlinkErr, unix.ENOENT) {
		return unlinkErr
	}
	return nil
}

func tempName(name string) (string, error) {
	var buf [16]byte
	if _, err := rand.Read(buf[:]); err != nil {
		return "", err
	}
	return fmt.Sprintf(".%s-%x.tmp", name, buf[:]), nil
}

func writeAll(fd int, data []byte) error {
	for len(data) > 0 {
		n, err := unix.Write(fd, data)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil {
			return err
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		data = data[n:]
	}
	return nil
}

func remedy(path string) string {
	if filepath.Base(path) == TokenFile {
		return "delete it and start looper serve again to make a new one: " + path
	}
	return "delete it and run looper auth set-password to make a new one: " + path
}

func credentialError(path string, err error) error {
	if err == nil {
		return nil
	}
	var op *Error
	if errors.As(err, &op) {
		return err
	}
	reason := err.Error()
	if errors.Is(err, unix.ELOOP) {
		reason = "is a symbolic link"
	}
	return &Error{Msg: fmt.Sprintf("cannot use operator credential %s: %s; %s", path, reason, remedy(path))}
}

func fileMode(st *unix.Stat_t) uint32 {
	return uint32(st.Mode)
}
