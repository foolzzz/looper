package operatorauth

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

const (
	sampleUser = "private-operator"
	samplePass = "correct horse battery"
)

func TestPasswordRecordIsPrivateScryptAndChangesSalt(t *testing.T) {
	root := filepath.Join(t.TempDir(), "new")
	ctx := context.Background()
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	rootInfo, err := os.Stat(root)
	if err != nil {
		t.Fatal(err)
	}
	if rootInfo.Mode().Perm() != 0o700 {
		t.Fatalf("root mode %o", rootInfo.Mode().Perm())
	}
	path := filepath.Join(root, PasswordFile)
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("file mode %o", info.Mode().Perm())
	}
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(body), samplePass) {
		t.Fatal("password was written to the file")
	}
	if len(bytes.Split(bytes.TrimRight(body, "\n"), []byte("\n"))) != 1 || !bytes.HasSuffix(body, []byte("\n")) {
		t.Fatalf("record is not one line: %q", body)
	}
	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		t.Fatal(err)
	}
	if data["user"] != sampleUser || data["scheme"] != "scrypt" || data["n"] != float64(scryptN) || data["r"] != float64(scryptR) || data["p"] != float64(scryptP) {
		t.Fatalf("record %#v", data)
	}
	rec, err := LoadPassword(ctx, root)
	if err != nil || rec == nil {
		t.Fatalf("load %#v %v", rec, err)
	}
	if len(rec.Salt) != saltLen || len(rec.Hash) != scryptLen {
		t.Fatalf("salt %d hash %d", len(rec.Salt), len(rec.Hash))
	}
	ok, err := Verify(ctx, rec, sampleUser, samplePass)
	if err != nil || !ok {
		t.Fatalf("verify %v %v", ok, err)
	}
	ok, err = Verify(ctx, rec, "wrong", samplePass)
	if err != nil || ok {
		t.Fatalf("wrong user %v %v", ok, err)
	}
	ok, err = Verify(ctx, rec, sampleUser, "wrong password")
	if err != nil || ok {
		t.Fatalf("wrong password %v %v", ok, err)
	}
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	again, err := LoadPassword(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(again.Salt, rec.Salt) {
		t.Fatal("salt was reused")
	}
	cleared, err := ClearPassword(ctx, root)
	if err != nil || !cleared {
		t.Fatalf("clear %v %v", cleared, err)
	}
	if _, statErr := os.Stat(path); !os.IsNotExist(statErr) {
		t.Fatal("file remains")
	}
	missing, err := LoadPassword(ctx, root)
	if err != nil || missing != nil {
		t.Fatalf("load after clear %#v %v", missing, err)
	}
	cleared, err = ClearPassword(ctx, root)
	if err != nil || cleared {
		t.Fatalf("second clear %v %v", cleared, err)
	}
	names, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(names) != 0 {
		t.Fatalf("temps left: %v", names)
	}
}

func TestVerifyDerivesBeforeComparingUser(t *testing.T) {
	root := t.TempDir()
	ctx := context.Background()
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	rec, err := LoadPassword(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	orig := deriveKey
	t.Cleanup(func() { deriveKey = orig })
	calls := 0
	deriveKey = func(password string, salt []byte, n, r, p int) ([]byte, error) {
		calls++
		if n != scryptN || r != scryptR || p != scryptP || !bytes.Equal(salt, rec.Salt) {
			t.Fatalf("derive n=%d r=%d p=%d", n, r, p)
		}
		return orig(password, salt, n, r, p)
	}
	ok, err := Verify(ctx, rec, "wrong", samplePass)
	if err != nil || ok || calls != 1 {
		t.Fatalf("ok %v err %v calls %d", ok, err, calls)
	}
}

func TestVerifyDifferentLengthsAreFalse(t *testing.T) {
	orig := deriveKey
	t.Cleanup(func() { deriveKey = orig })
	deriveKey = func(string, []byte, int, int, int) ([]byte, error) {
		return []byte{1}, nil
	}
	ok, err := Verify(context.Background(), &PasswordRecord{
		User: "ab", N: scryptN, R: scryptR, P: scryptP,
		Salt: bytes.Repeat([]byte{2}, saltLen), Hash: []byte{3, 4},
	}, "wrong-user", samplePass)
	if err != nil || ok {
		t.Fatalf("ok %v err %v", ok, err)
	}
}

func TestScryptVectors(t *testing.T) {
	salt, err := hex.DecodeString("00112233445566778899aabbccddeeff")
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		password string
		want     string
	}{
		{password: "correct horse battery", want: "4B5H3rZZyqg1Jb6cZX25OnuHCYuJfU0jglmPluoLpP0="},
		{password: strings.Repeat("密码", 4), want: "8K-8l6vPNHhpJrohs-WG5erbdhJ35Nj5lfviJISkGtc="},
	}
	for _, tt := range tests {
		sum, err := deriveKey(tt.password, salt, scryptN, scryptR, scryptP)
		if err != nil {
			t.Fatal(err)
		}
		if got := base64.URLEncoding.EncodeToString(sum); got != tt.want {
			t.Fatalf("%q hash %s", tt.password, got)
		}
	}
	nacl, err := deriveKey("password", []byte("NaCl"), 16384, 8, 1)
	if err != nil {
		t.Fatal(err)
	}
	const wantNacl = "a8430d7e581f9ca03c952df506ac66c757899d67a21d71c0f1900bd778ac1d14"
	if hex.EncodeToString(nacl) != wantNacl {
		t.Fatalf("nacl %x", nacl)
	}
}

func TestScryptGateMatchesOpenSSL(t *testing.T) {
	tests := []struct {
		name    string
		n, r, p int
		ok      bool
	}{
		{name: "default", n: 32768, r: 8, p: 1, ok: true},
		{name: "r1 under shift", n: 32768, r: 1, p: 1, ok: true},
		{name: "p32 fits", n: 16384, r: 8, p: 32, ok: true},
		{name: "r1 at 65536", n: 65536, r: 1, p: 1, ok: false},
		{name: "r16 memory", n: 32768, r: 16, p: 1, ok: false},
		{name: "n65536 r8", n: 65536, r: 8, p: 1, ok: false},
		{name: "r32 memory", n: 16384, r: 32, p: 1, ok: false},
		{name: "n 2^20 r1", n: 1 << 20, r: 1, p: 1, ok: false},
		{name: "n not power", n: 6, r: 8, p: 1, ok: false},
		{name: "r zero", n: 32768, r: 0, p: 1, ok: false},
		{name: "p zero", n: 32768, r: 8, p: 0, ok: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := scryptAllowed(tt.n, tt.r, tt.p)
			if tt.ok && err != nil {
				t.Fatal(err)
			}
			if !tt.ok && err == nil {
				t.Fatal("accepted")
			}
		})
	}
	_, err := Verify(context.Background(), &PasswordRecord{
		User: sampleUser, N: 65536, R: 1, P: 1,
		Salt: bytes.Repeat([]byte{1}, saltLen),
		Hash: bytes.Repeat([]byte{2}, scryptLen),
	}, sampleUser, samplePass)
	if err == nil || !strings.Contains(err.Error(), "scrypt") {
		t.Fatal(err)
	}
}

func TestUnsafePasswordFileIsRefused(t *testing.T) {
	tests := []struct {
		name    string
		prepare func(t *testing.T, root, path string)
		want    string
	}{
		{name: "permissions", want: "chmod 600", prepare: func(t *testing.T, root, path string) {
			if err := os.Chmod(path, 0o644); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "symlink", want: "is a symbolic link", prepare: func(t *testing.T, root, path string) {
			target := filepath.Join(root, "record")
			if err := os.Rename(path, target); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(target, path); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "fifo", want: "not a regular file", prepare: func(t *testing.T, root, path string) {
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			if err := unix.Mkfifo(path, 0o600); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "directory", want: "not a regular file", prepare: func(t *testing.T, root, path string) {
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			if err := os.Mkdir(path, 0o700); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "owner", want: "owned by uid", prepare: func(t *testing.T, root, path string) {
			orig := statFile
			t.Cleanup(func() { statFile = orig })
			statFile = func(fd int, st *unix.Stat_t) error {
				if err := orig(fd, st); err != nil {
					return err
				}
				if fileMode(st)&unix.S_IFMT == unix.S_IFREG {
					st.Uid++
				}
				return nil
			}
		}},
		{name: "oversize", want: "not a password record", prepare: func(t *testing.T, root, path string) {
			if err := os.WriteFile(path, bytes.Repeat([]byte{'x'}, 4097), 0o600); err != nil {
				t.Fatal(err)
			}
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			root := t.TempDir()
			if err := SetPassword(context.Background(), root, sampleUser, samplePass); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(root, PasswordFile)
			tt.prepare(t, root, path)
			done := make(chan error, 1)
			go func() {
				_, err := LoadPassword(context.Background(), root)
				done <- err
			}()
			select {
			case err := <-done:
				if err == nil || !strings.Contains(err.Error(), tt.want) || !strings.Contains(err.Error(), path) {
					t.Fatal(err)
				}
				var op *Error
				if !errors.As(err, &op) {
					t.Fatalf("type %T", err)
				}
				if tt.name == "permissions" && strings.Contains(err.Error(), "delete it") {
					t.Fatal(err)
				}
				if tt.name != "permissions" && !strings.Contains(err.Error(), "delete it") {
					t.Fatal(err)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("password reader blocked")
			}
		})
	}
}

func TestMalformedPasswordRecordIsRefused(t *testing.T) {
	tests := []struct {
		name  string
		field string
		value any
		drop  bool
	}{
		{name: "scheme", field: "scheme", value: "plain"},
		{name: "user space", field: "user", value: "bad user"},
		{name: "salt star", field: "salt", value: "*"},
		{name: "hash star", field: "hash", value: "*"},
		{name: "salt short", field: "salt", value: "YQ=="},
		{name: "hash short", field: "hash", value: "YQ=="},
		{name: "n odd", field: "n", value: 16385},
		{name: "n small", field: "n", value: 8192},
		{name: "n big", field: "n", value: 1 << 21},
		{name: "n bool", field: "n", value: true},
		{name: "r zero", field: "r", value: 0},
		{name: "r high", field: "r", value: 33},
		{name: "p zero", field: "p", value: 0},
		{name: "p high", field: "p", value: 33},
		{name: "salt junk", field: "salt", value: "YWFhYWFhYWFhYWFhYWFhYQ==!"},
		{name: "r bool", field: "r", value: true},
		{name: "drop scheme", field: "scheme", drop: true},
		{name: "drop user", field: "user", drop: true},
		{name: "drop n", field: "n", drop: true},
		{name: "drop r", field: "r", drop: true},
		{name: "drop p", field: "p", drop: true},
		{name: "drop salt", field: "salt", drop: true},
		{name: "drop hash", field: "hash", drop: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			root := t.TempDir()
			if err := SetPassword(context.Background(), root, sampleUser, samplePass); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(root, PasswordFile)
			body, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			var data map[string]any
			if err := json.Unmarshal(body, &data); err != nil {
				t.Fatal(err)
			}
			if tt.drop {
				delete(data, tt.field)
			} else {
				data[tt.field] = tt.value
			}
			raw, err := json.Marshal(data)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, raw, 0o600); err != nil {
				t.Fatal(err)
			}
			_, err = LoadPassword(context.Background(), root)
			if err == nil || !strings.Contains(err.Error(), "not a password record") {
				t.Fatal(err)
			}
		})
	}
}

func TestInvalidPasswordJSONIsRefused(t *testing.T) {
	tests := []struct {
		name string
		body []byte
	}{
		{name: "text", body: []byte("not-json")},
		{name: "array", body: []byte("[]")},
		{name: "null", body: []byte("null")},
		{name: "invalid utf-8 in string", body: passwordRecordBody([]byte{0xff})},
		{name: "lone ff", body: []byte{0xff}},
		{name: "extra", body: []byte("{\"user\":\"a\"}{}")},
		{name: "float n", body: []byte(`{"user":"a","scheme":"scrypt","n":16384.0,"r":8,"p":1,"salt":"AAAAAAAAAAAAAAAAAAAAAA==","hash":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}`)},
		{name: "exponent", body: []byte(`{"user":"a","scheme":"scrypt","n":1e2,"r":8,"p":1,"salt":"AAAAAAAAAAAAAAAAAAAAAA==","hash":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}`)},
		{name: "p float", body: []byte(`{"user":"a","scheme":"scrypt","n":16384,"r":8,"p":1.0,"salt":"AAAAAAAAAAAAAAAAAAAAAA==","hash":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}`)},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			root := t.TempDir()
			path := filepath.Join(root, PasswordFile)
			if err := os.WriteFile(path, tt.body, 0o600); err != nil {
				t.Fatal(err)
			}
			_, err := LoadPassword(context.Background(), root)
			if err == nil || !strings.Contains(err.Error(), "not a password record") {
				t.Fatal(err)
			}
		})
	}
}

func TestPasswordRecordAcceptsBothBase64AlphabetsAndExtraKeys(t *testing.T) {
	salt := bytes.Repeat([]byte{0xfb}, saltLen)
	sum := bytes.Repeat([]byte{0x01}, scryptLen)
	std := base64.StdEncoding.EncodeToString(salt)
	url := base64.URLEncoding.EncodeToString(salt)
	if std == url {
		t.Fatal("salt did not exercise both alphabets")
	}
	for _, enc := range []string{std, url} {
		body := []byte(`{"extra":1,"user":"a","scheme":"scrypt","n":16384,"r":1,"p":1,"salt":"` + enc + `","hash":"` + base64.URLEncoding.EncodeToString(sum) + `"}` + "\n")
		root := t.TempDir()
		path := filepath.Join(root, PasswordFile)
		if err := os.WriteFile(path, body, 0o600); err != nil {
			t.Fatal(err)
		}
		rec, err := LoadPassword(context.Background(), root)
		if err != nil {
			t.Fatal(err)
		}
		if rec.User != "a" || rec.N != 16384 || rec.R != 1 || rec.P != 1 || !bytes.Equal(rec.Salt, salt) {
			t.Fatalf("%#v", rec)
		}
	}
}

func TestPasswordBase64FollowsPythonNotGoStrict(t *testing.T) {
	// Go's Strict decoder skips CR/LF and rejects leftover padding bits.
	// Python's validate=True decoder does the opposite.
	tests := []struct {
		name   string
		in     string
		n      int
		ok     bool
		strict bool
	}{
		{name: "nonzero salt padding", in: "AAAAAAAAAAAAAAAAAAAAAB==", n: saltLen, ok: true, strict: false},
		{name: "zero salt padding", in: "AAAAAAAAAAAAAAAAAAAAAA==", n: saltLen, ok: true, strict: true},
		{name: "newline", in: "AAAAAAAAAAAAAAAAAAAAAA==\n", n: saltLen, ok: false, strict: true},
		{name: "cr", in: "AAAAAAAAAAAAAAAAAAAAAA==\r", n: saltLen, ok: false, strict: true},
		{name: "crlf", in: "AAAAAAAAAAAAAAAAAAAAAA==\r\n", n: saltLen, ok: false, strict: true},
		{name: "embedded newline", in: "AAAA\nAAAAAAAAAAAAAAAAAA==", n: saltLen, ok: false, strict: true},
		{name: "nonzero hash padding", in: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB=", n: scryptLen, ok: true, strict: false},
		{name: "hash newline", in: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n", n: scryptLen, ok: false, strict: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, strictErr := base64.StdEncoding.Strict().DecodeString(tt.in)
			if (strictErr == nil) != tt.strict {
				t.Fatalf("Go Strict accept %v err %v", strictErr == nil, strictErr)
			}
			got, ok := decodeSecret(tt.in, tt.n)
			if ok != tt.ok {
				t.Fatalf("decode ok %v", ok)
			}
			if tt.ok && !bytes.Equal(got, bytes.Repeat([]byte{0}, tt.n)) {
				t.Fatalf("%x", got)
			}
		})
	}
}

func TestPasswordParameterBoundariesLoad(t *testing.T) {
	tests := []struct {
		field string
		value int
	}{
		{field: "n", value: 1 << 14},
		{field: "n", value: 1 << 20},
		{field: "r", value: 1},
		{field: "r", value: 32},
		{field: "p", value: 1},
		{field: "p", value: 32},
	}
	for _, tt := range tests {
		t.Run(tt.field, func(t *testing.T) {
			root := t.TempDir()
			if err := SetPassword(context.Background(), root, sampleUser, samplePass); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(root, PasswordFile)
			body, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			var data map[string]any
			if err := json.Unmarshal(body, &data); err != nil {
				t.Fatal(err)
			}
			data[tt.field] = tt.value
			raw, err := json.Marshal(data)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, raw, 0o600); err != nil {
				t.Fatal(err)
			}
			rec, err := LoadPassword(context.Background(), root)
			if err != nil {
				t.Fatal(err)
			}
			got := map[string]int{"n": rec.N, "r": rec.R, "p": rec.P}[tt.field]
			if got != tt.value {
				t.Fatalf("got %d", got)
			}
		})
	}
}

func TestPasswordInputBounds(t *testing.T) {
	tests := []struct {
		user     string
		password string
		ok       bool
		want     string
	}{
		{user: "a", password: "12345678", ok: true},
		{user: strings.Repeat("a", 64), password: strings.Repeat("密码", 4), ok: true},
		{user: "A-_.0", password: "12345678", ok: true},
		{user: "", password: "12345678", want: "user name"},
		{user: strings.Repeat("a", 65), password: "12345678", want: "user name"},
		{user: "a b", password: "12345678", want: "user name"},
		{user: "é", password: "12345678", want: "user name"},
		{user: "a", password: "1234567", want: "password must contain at least 8"},
		{user: "", password: "short", want: "user name"},
	}
	for _, tt := range tests {
		name := tt.user + "/" + tt.password
		t.Run(name, func(t *testing.T) {
			root := filepath.Join(t.TempDir(), "runtime")
			err := SetPassword(context.Background(), root, tt.user, tt.password)
			if tt.ok {
				if err != nil {
					t.Fatal(err)
				}
				rec, loadErr := LoadPassword(context.Background(), root)
				if loadErr != nil {
					t.Fatal(loadErr)
				}
				ok, verr := Verify(context.Background(), rec, tt.user, tt.password)
				if verr != nil || !ok {
					t.Fatalf("verify %v %v", ok, verr)
				}
				return
			}
			var input *InputError
			if !errors.As(err, &input) || !strings.Contains(err.Error(), tt.want) {
				t.Fatal(err)
			}
			if _, statErr := os.Stat(filepath.Join(root, PasswordFile)); !os.IsNotExist(statErr) {
				t.Fatal("rejected input created the password file")
			}
			if _, statErr := os.Stat(root); !os.IsNotExist(statErr) {
				t.Fatal("rejected input created the runtime root")
			}
		})
	}
}

func TestPasswordOperationsRefuseUnsafeRoot(t *testing.T) {
	ctx := context.Background()
	t.Run("set", func(t *testing.T) {
		root := t.TempDir()
		if err := os.Chmod(root, 0o775); err != nil {
			t.Fatal(err)
		}
		err := SetPassword(ctx, root, sampleUser, samplePass)
		if err == nil || !strings.Contains(err.Error(), "not writable by group or others") {
			t.Fatal(err)
		}
	})
	t.Run("load", func(t *testing.T) {
		root := t.TempDir()
		if err := os.Chmod(root, 0o775); err != nil {
			t.Fatal(err)
		}
		_, err := LoadPassword(ctx, root)
		if err == nil || !strings.Contains(err.Error(), "not writable by group or others") {
			t.Fatal(err)
		}
	})
	t.Run("clear", func(t *testing.T) {
		root := t.TempDir()
		if err := os.Chmod(root, 0o775); err != nil {
			t.Fatal(err)
		}
		_, err := ClearPassword(ctx, root)
		if err == nil || !strings.Contains(err.Error(), "not writable by group or others") {
			t.Fatal(err)
		}
	})
}

func TestPasswordWriteFailureKeepsOldFile(t *testing.T) {
	root := t.TempDir()
	ctx := context.Background()
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, PasswordFile)
	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(root, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(root, 0o700) })
	err = SetPassword(ctx, root, sampleUser, "different password")
	if err == nil || !strings.Contains(err.Error(), "cannot write operator credential") {
		t.Fatal(err)
	}
	var op *Error
	if !errors.As(err, &op) {
		t.Fatalf("type %T", err)
	}
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	after, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("failed write replaced the password file")
	}
	names, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(names) != 1 || names[0].Name() != PasswordFile {
		t.Fatalf("directory %v", names)
	}
}

func TestClearPasswordDoesNotFollowSymlink(t *testing.T) {
	root := t.TempDir()
	ctx := context.Background()
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, PasswordFile)
	target := filepath.Join(root, "target")
	if err := os.Rename(path, target); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, path); err != nil {
		t.Fatal(err)
	}
	cleared, err := ClearPassword(ctx, root)
	if err != nil || !cleared {
		t.Fatalf("clear %v %v", cleared, err)
	}
	if _, statErr := os.Lstat(path); !os.IsNotExist(statErr) {
		t.Fatal("symlink remains")
	}
	body, err := os.ReadFile(target)
	if err != nil || len(body) == 0 {
		t.Fatal("unlink followed the symlink")
	}
}

func TestOpenedPasswordSurvivesSwapToSymlink(t *testing.T) {
	root := t.TempDir()
	ctx := context.Background()
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, PasswordFile)
	original, err := LoadPassword(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(root, "target")
	if err := os.WriteFile(target, []byte("bad"), 0o600); err != nil {
		t.Fatal(err)
	}
	orig := statFile
	t.Cleanup(func() { statFile = orig })
	swapped := false
	statFile = func(fd int, st *unix.Stat_t) error {
		if err := orig(fd, st); err != nil {
			return err
		}
		if !swapped && fileMode(st)&unix.S_IFMT == unix.S_IFREG {
			swapped = true
			if err := os.Remove(path); err != nil {
				return err
			}
			if err := os.Symlink(target, path); err != nil {
				return err
			}
		}
		return nil
	}
	got, err := LoadPassword(ctx, root)
	if !swapped {
		t.Fatal("file was not swapped")
	}
	if err != nil {
		t.Fatal(err)
	}
	if got.User != original.User || !bytes.Equal(got.Hash, original.Hash) || !bytes.Equal(got.Salt, original.Salt) {
		t.Fatal("record changed after the path was replaced")
	}
}

func TestCancelledPasswordContextDoesNotCreateRoot(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	root := filepath.Join(t.TempDir(), "missing")
	if err := SetPassword(ctx, root, sampleUser, samplePass); err == nil {
		t.Fatal("set accepted a cancelled context")
	}
	if _, err := LoadPassword(ctx, root); err == nil {
		t.Fatal("load accepted a cancelled context")
	}
	if _, err := ClearPassword(ctx, root); err == nil {
		t.Fatal("clear accepted a cancelled context")
	}
	if _, err := os.Stat(root); !os.IsNotExist(err) {
		t.Fatal("cancelled context created the runtime root")
	}
	orig := deriveKey
	t.Cleanup(func() { deriveKey = orig })
	deriveKey = func(string, []byte, int, int, int) ([]byte, error) {
		t.Fatal("verify derived after cancel")
		return nil, errors.New("derived")
	}
	_, err := Verify(ctx, &PasswordRecord{User: sampleUser, N: scryptN, R: scryptR, P: scryptP}, sampleUser, samplePass)
	if err == nil {
		t.Fatal("verify accepted a cancelled context")
	}
	_, err = Verify(context.Background(), nil, sampleUser, samplePass)
	if err == nil {
		t.Fatal("nil record")
	}
}

func TestVerifyRejectsCancelDuringDerivation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	orig := deriveKey
	t.Cleanup(func() { deriveKey = orig })
	digest := bytes.Repeat([]byte{0x5a}, scryptLen)
	deriveKey = func(string, []byte, int, int, int) ([]byte, error) {
		cancel()
		return append([]byte(nil), digest...), nil
	}
	rec := &PasswordRecord{
		User: sampleUser,
		N:    scryptN,
		R:    scryptR,
		P:    scryptP,
		Salt: bytes.Repeat([]byte{1}, saltLen),
		Hash: append([]byte(nil), digest...),
	}
	ok, err := Verify(ctx, rec, sampleUser, samplePass)
	if ok || !errors.Is(err, context.Canceled) {
		t.Fatalf("ok %v err %v", ok, err)
	}
}

func TestPasswordCacheReloadsOnChangeAndRefusesSymlink(t *testing.T) {
	root := t.TempDir()
	ctx := context.Background()
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	orig := readPassword
	t.Cleanup(func() { readPassword = orig })
	calls := 0
	readPassword = func(ctx context.Context, root string) (*PasswordRecord, error) {
		calls++
		return orig(ctx, root)
	}
	logs := captureAuthLogs(t)
	cache := NewPasswordCache(root)
	first, err := cache.Record(ctx)
	if err != nil || first == nil {
		t.Fatal(err)
	}
	second, err := cache.Record(ctx)
	if err != nil || second != first || calls != 1 {
		t.Fatalf("second %#v calls %d err %v", second, calls, err)
	}
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	third, err := cache.Record(ctx)
	if err != nil || bytes.Equal(third.Hash, first.Hash) || calls != 2 {
		t.Fatalf("calls %d err %v", calls, err)
	}
	path := filepath.Join(root, PasswordFile)
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := cache.Record(ctx); err == nil || calls != 3 {
		t.Fatalf("chmod err %v calls %d", err, calls)
	}
	if _, err := cache.Record(ctx); err == nil || calls != 3 {
		t.Fatalf("repeat err %v calls %d", err, calls)
	}
	if strings.Count(logs.String(), "chmod 600") != 1 {
		t.Fatalf("log %s", logs.String())
	}
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatal(err)
	}
	fourth, err := cache.Record(ctx)
	if err != nil || fourth == nil || calls != 4 {
		t.Fatalf("restored %#v calls %d err %v", fourth, calls, err)
	}
	target := filepath.Join(root, "target")
	if err := os.Rename(path, target); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, path); err != nil {
		t.Fatal(err)
	}
	if _, err := cache.Record(ctx); err == nil || !strings.Contains(err.Error(), "is a symbolic link") || calls != 5 {
		t.Fatalf("symlink %v calls %d", err, calls)
	}
	if _, err := cache.Record(ctx); err == nil || calls != 5 {
		t.Fatalf("cached symlink %v calls %d", err, calls)
	}
	if strings.Contains(logs.String(), samplePass) || strings.Count(logs.String(), "is a symbolic link") != 1 {
		t.Fatalf("log %s", logs.String())
	}
}

func TestPasswordJSONFollowsPythonByteDecoding(t *testing.T) {
	for _, tt := range passwordEncodingCases() {
		t.Run(tt.name, func(t *testing.T) {
			root := t.TempDir()
			if err := os.WriteFile(filepath.Join(root, PasswordFile), tt.body, 0o600); err != nil {
				t.Fatal(err)
			}
			rec, err := LoadPassword(context.Background(), root)
			if !tt.ok {
				if err == nil || !strings.Contains(err.Error(), "not a password record") {
					t.Fatal(err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			wantUser := tt.user
			if wantUser == "" {
				wantUser = "a"
			}
			if rec.User != wantUser || rec.N != 16384 || rec.R != 8 || rec.P != 1 {
				t.Fatalf("%#v", rec)
			}
			if !bytes.Equal(rec.Salt, bytes.Repeat([]byte{0}, saltLen)) || !bytes.Equal(rec.Hash, bytes.Repeat([]byte{0}, scryptLen)) {
				t.Fatalf("salt %x hash %x", rec.Salt, rec.Hash)
			}
		})
	}
}

func TestPasswordCacheReloadsOnSizeMtimeAndOwner(t *testing.T) {
	root := t.TempDir()
	ctx := context.Background()
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	orig := readPassword
	t.Cleanup(func() { readPassword = orig })
	calls := 0
	readPassword = func(ctx context.Context, root string) (*PasswordRecord, error) {
		calls++
		return orig(ctx, root)
	}
	cache := NewPasswordCache(root)
	if _, err := cache.Record(ctx); err != nil || calls != 1 {
		t.Fatalf("first calls %d err %v", calls, err)
	}
	path := filepath.Join(root, PasswordFile)
	before := passwordStatKey(t, path)
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, append(append([]byte{}, body...), '\n', '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatal(err)
	}
	stamp := time.Unix(0, before.mtime)
	if err := os.Chtimes(path, stamp, stamp); err != nil {
		t.Fatal(err)
	}
	sized := passwordStatKey(t, path)
	if sized.ino != before.ino || sized.mtime != before.mtime || sized.mode != before.mode || sized.uid != before.uid || sized.size == before.size {
		t.Fatalf("size was not the only change: before %+v after %+v", before, sized)
	}
	if _, err := cache.Record(ctx); err != nil || calls != 2 {
		t.Fatalf("size calls %d err %v", calls, err)
	}
	moved := time.Unix(0, sized.mtime).Add(time.Second)
	if err := os.Chtimes(path, moved, moved); err != nil {
		t.Fatal(err)
	}
	touched := passwordStatKey(t, path)
	if touched.mtime == sized.mtime || touched.ino != sized.ino || touched.size != sized.size || touched.mode != sized.mode || touched.uid != sized.uid {
		t.Fatalf("mtime was not the only change: before %+v after %+v", sized, touched)
	}
	if _, err := cache.Record(ctx); err != nil || calls != 3 {
		t.Fatalf("mtime calls %d err %v", calls, err)
	}
	origStat := passwordLstat
	t.Cleanup(func() { passwordLstat = origStat })
	passwordLstat = func(path string, st *unix.Stat_t) error {
		if err := origStat(path, st); err != nil {
			return err
		}
		st.Uid++
		return nil
	}
	if _, err := cache.Record(ctx); err != nil || calls != 4 {
		t.Fatalf("owner calls %d err %v", calls, err)
	}
	if _, err := cache.Record(ctx); err != nil || calls != 4 {
		t.Fatalf("owner cached calls %d err %v", calls, err)
	}
	passwordLstat = origStat
	if _, err := cache.Record(ctx); err != nil || calls != 5 {
		t.Fatalf("owner restored calls %d err %v", calls, err)
	}
}

func passwordStatKey(t *testing.T, path string) passwordKey {
	t.Helper()
	var st unix.Stat_t
	if err := unix.Lstat(path, &st); err != nil {
		t.Fatal(err)
	}
	return passwordKey{ino: st.Ino, size: st.Size, mtime: st.Mtim.Nano(), mode: fileMode(&st), uid: st.Uid}
}

func pythonConstantRecord(prefix string) []byte {
	return []byte(prefix + `,"user":"a","scheme":"scrypt","n":16384,"r":8,"p":1,"salt":"AAAAAAAAAAAAAAAAAAAAAA==","hash":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}`)
}

func passwordRecordBody(note []byte) []byte {
	const prefix = `{"user":"a","scheme":"scrypt","n":16384,"r":8,"p":1,"salt":"AAAAAAAAAAAAAAAAAAAAAA==","hash":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=","note":"`
	body := make([]byte, 0, len(prefix)+len(note)+2)
	body = append(body, prefix...)
	body = append(body, note...)
	body = append(body, '"', '}')
	return body
}

type passwordEncodingCase struct {
	name string
	body []byte
	ok   bool
	user string
}

func passwordEncodingCases() []passwordEncodingCase {
	text := passwordRecordBody([]byte("x"))
	s := string(text)
	utf16 := encodeUTF16(s, true, true)
	return []passwordEncodingCase{
		{name: "utf-8", body: append([]byte(nil), text...), ok: true},
		{name: "utf-8 bom", body: append([]byte{0xEF, 0xBB, 0xBF}, text...), ok: true},
		{name: "double bom", body: append([]byte{0xEF, 0xBB, 0xBF, 0xEF, 0xBB, 0xBF}, text...), ok: false},
		{name: "utf-16", body: utf16, ok: true},
		{name: "utf-16-be bom", body: encodeUTF16(s, true, false), ok: true},
		{name: "utf-16-le", body: encodeUTF16(s, false, true), ok: true},
		{name: "utf-16-be", body: encodeUTF16(s, false, false), ok: true},
		{name: "utf-32", body: encodeUTF32(s, true, true), ok: true},
		{name: "utf-32-be bom", body: encodeUTF32(s, true, false), ok: true},
		{name: "utf-32-le", body: encodeUTF32(s, false, true), ok: true},
		{name: "utf-32-be", body: encodeUTF32(s, false, false), ok: true},
		{name: "invalid utf-8 in string", body: passwordRecordBody([]byte{0xff}), ok: false},
		// json.loads accepts NaN, Infinity, and -Infinity. encoding/json rejects them.
		{name: "extra nan", body: pythonConstantRecord(`{"extra": NaN`), ok: true},
		{name: "extra nan utf-16", body: encodeUTF16(string(pythonConstantRecord(`{"extra": NaN`)), true, true), ok: true},
		{name: "extra infinity", body: pythonConstantRecord(`{"extra":Infinity`), ok: true},
		{name: "extra -infinity", body: pythonConstantRecord(`{"extra":-Infinity`), ok: true},
		{name: "nested nan", body: pythonConstantRecord(`{"extra":{"x":NaN,"y":[Infinity,-Infinity]}`), ok: true},
		{name: "escaped quote then nan", body: pythonConstantRecord(`{"note":"say \"NaN\"" ,"extra":NaN`), ok: true},
		{name: "nan inside user", body: []byte(`{"user":"aNaN","extra":NaN,"scheme":"scrypt","n":16384,"r":8,"p":1,"salt":"AAAAAAAAAAAAAAAAAAAAAA==","hash":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}`), ok: true, user: "aNaN"},
		{name: "string nan", body: pythonConstantRecord(`{"extra":"NaN"`), ok: true},
		{name: "n nan", body: []byte(`{"user":"a","scheme":"scrypt","n":NaN,"r":8,"p":1,"salt":"AAAAAAAAAAAAAAAAAAAAAA==","hash":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}`), ok: false},
		{name: "nanfoo", body: pythonConstantRecord(`{"extra":NaNfoo`), ok: false},
		{name: "+infinity", body: pythonConstantRecord(`{"extra":+Infinity`), ok: false},
		{name: "surrogate in note", body: passwordRecordBody([]byte{0xED, 0xA0, 0x80}), ok: true},
		{name: "utf-16 emoji note", body: encodeUTF16(string(passwordRecordBody([]byte("😀"))), true, true), ok: true},
		{name: "truncated utf-16", body: utf16[:len(utf16)-1], ok: false},
	}
}

func encodeUTF16(s string, bom, little bool) []byte {
	var dst []byte
	if bom {
		if little {
			dst = append(dst, 0xFF, 0xFE)
		} else {
			dst = append(dst, 0xFE, 0xFF)
		}
	}
	for _, r := range s {
		if r > 0xFFFF {
			r -= 0x10000
			dst = appendUTF16Unit(dst, uint16(0xD800+(r>>10)), little)
			dst = appendUTF16Unit(dst, uint16(0xDC00+(r&0x3FF)), little)
			continue
		}
		dst = appendUTF16Unit(dst, uint16(r), little)
	}
	return dst
}

func appendUTF16Unit(dst []byte, u uint16, little bool) []byte {
	if little {
		return append(dst, byte(u), byte(u>>8))
	}
	return append(dst, byte(u>>8), byte(u))
}

func encodeUTF32(s string, bom, little bool) []byte {
	var dst []byte
	if bom {
		if little {
			dst = append(dst, 0xFF, 0xFE, 0x00, 0x00)
		} else {
			dst = append(dst, 0x00, 0x00, 0xFE, 0xFF)
		}
	}
	for _, r := range s {
		u := uint32(r)
		if little {
			dst = append(dst, byte(u), byte(u>>8), byte(u>>16), byte(u>>24))
		} else {
			dst = append(dst, byte(u>>24), byte(u>>16), byte(u>>8), byte(u))
		}
	}
	return dst
}

func TestPasswordCacheMissDoesNotCreateRoot(t *testing.T) {
	root := filepath.Join(t.TempDir(), "absent")
	rec, err := NewPasswordCache(root).Record(context.Background())
	if err != nil || rec != nil {
		t.Fatalf("rec %#v err %v", rec, err)
	}
	if _, statErr := os.Stat(root); !os.IsNotExist(statErr) {
		t.Fatal("cache created the runtime root")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := NewPasswordCache(root).Record(ctx); err == nil {
		t.Fatal("cancelled cache")
	}
	if _, statErr := os.Stat(root); !os.IsNotExist(statErr) {
		t.Fatal("cancelled cache created the runtime root")
	}
}

func TestPasswordCacheStatErrorDoesNotReload(t *testing.T) {
	root := t.TempDir()
	ctx := context.Background()
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	orig := readPassword
	t.Cleanup(func() { readPassword = orig })
	calls := 0
	readPassword = func(ctx context.Context, root string) (*PasswordRecord, error) {
		calls++
		return orig(ctx, root)
	}
	cache := NewPasswordCache(root)
	first, err := cache.Record(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(root, 0o700) })
	if err := os.Chmod(root, 0); err != nil {
		t.Fatal(err)
	}
	_, err = cache.Record(ctx)
	if err == nil || !strings.Contains(err.Error(), "cannot stat password file") || calls != 1 {
		t.Fatalf("stat %v calls %d", err, calls)
	}
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	second, err := cache.Record(ctx)
	if err != nil || second != first || calls != 1 {
		t.Fatalf("restored %#v calls %d err %v", second, calls, err)
	}
}

func TestPasswordCacheDeleteThenCreate(t *testing.T) {
	root := t.TempDir()
	ctx := context.Background()
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	orig := readPassword
	t.Cleanup(func() { readPassword = orig })
	calls := 0
	readPassword = func(ctx context.Context, root string) (*PasswordRecord, error) {
		calls++
		return orig(ctx, root)
	}
	cache := NewPasswordCache(root)
	if _, err := cache.Record(ctx); err != nil || calls != 1 {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(root, PasswordFile)); err != nil {
		t.Fatal(err)
	}
	rec, err := cache.Record(ctx)
	if err != nil || rec != nil || calls != 1 {
		t.Fatalf("missing %#v calls %d err %v", rec, calls, err)
	}
	if err := SetPassword(ctx, root, sampleUser, samplePass); err != nil {
		t.Fatal(err)
	}
	rec, err = cache.Record(ctx)
	if err != nil || rec == nil || calls != 2 {
		t.Fatalf("recreated %#v calls %d err %v", rec, calls, err)
	}
}

func captureAuthLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}
