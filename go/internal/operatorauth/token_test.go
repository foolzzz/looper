package operatorauth

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

const (
	sampleToken = "operator_" + "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	otherToken  = "agent_" + "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)

func TestEnsureTokenCreatesPrivateFileAndKeepsIt(t *testing.T) {
	root := filepath.Join(t.TempDir(), "runtime")
	ctx := context.Background()
	token, err := EnsureToken(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	if !tokenPattern.MatchString(token) {
		t.Fatal("created token does not match the token pattern")
	}
	path := filepath.Join(root, TokenFile)
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != token+"\n" {
		t.Fatal("token file is not the token plus a newline")
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("token mode %o", info.Mode().Perm())
	}
	rootInfo, err := os.Stat(root)
	if err != nil {
		t.Fatal(err)
	}
	if rootInfo.Mode().Perm() != 0o700 {
		t.Fatalf("root mode %o", rootInfo.Mode().Perm())
	}
	again, err := EnsureToken(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	if again != token {
		t.Fatal("second ensure replaced the token")
	}
	read, err := ReadToken(ctx, root)
	if err != nil || read != token {
		t.Fatalf("read %q err %v", read, err)
	}
	for _, env := range os.Environ() {
		_, val, _ := strings.Cut(env, "=")
		if strings.Contains(val, token) {
			t.Fatal("token leaked into the environment")
		}
	}
}

func TestUnsafeRuntimeRootsAreRefused(t *testing.T) {
	tests := []struct {
		name string
		mode os.FileMode
	}{
		{name: "world writable", mode: 0o777},
		{name: "group writable", mode: 0o775},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			root := t.TempDir()
			if err := os.Chmod(root, tt.mode); err != nil {
				t.Fatal(err)
			}
			_, err := EnsureToken(context.Background(), root)
			if err == nil || !strings.Contains(err.Error(), "not writable by group or others") {
				t.Fatal(err)
			}
			if _, statErr := os.Stat(filepath.Join(root, TokenFile)); !os.IsNotExist(statErr) {
				t.Fatal("token file was created in an unsafe root")
			}
		})
	}
}

func TestRuntimeRootOwnerIsChecked(t *testing.T) {
	orig := currentUID
	currentUID = func() int { return os.Getuid() + 1 }
	t.Cleanup(func() { currentUID = orig })
	root := t.TempDir()
	_, err := EnsureToken(context.Background(), root)
	if err == nil || !strings.Contains(err.Error(), "owned by uid") {
		t.Fatal(err)
	}
	if _, statErr := os.Stat(filepath.Join(root, TokenFile)); !os.IsNotExist(statErr) {
		t.Fatal("token file was created for a root owned by someone else")
	}
}

func TestExistingReadableRootAndDirectorySymlink(t *testing.T) {
	parent := t.TempDir()
	real := filepath.Join(parent, "runtime")
	if err := os.Mkdir(real, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(real, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(parent, "link")
	if err := os.Symlink(real, link); err != nil {
		t.Fatal(err)
	}
	for _, root := range []string{real, link} {
		token, err := EnsureToken(context.Background(), root)
		if err != nil {
			t.Fatal(err)
		}
		again, err := EnsureToken(context.Background(), root)
		if err != nil || again != token {
			t.Fatalf("again %q err %v", again, err)
		}
		info, err := os.Stat(root)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o755 {
			t.Fatalf("root mode changed to %o", info.Mode().Perm())
		}
	}
	if _, err := os.Stat(filepath.Join(real, TokenFile)); err != nil {
		t.Fatal(err)
	}
}

func TestTokenOwnerIsChecked(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, TokenFile)
	if err := os.WriteFile(path, []byte(sampleToken+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	orig := statFile
	t.Cleanup(func() { statFile = orig })
	statFile = func(fd int, st *unix.Stat_t) error {
		if err := orig(fd, st); err != nil {
			return err
		}
		if fileMode(st)&unix.S_IFMT == unix.S_IFREG {
			st.Uid = uint32(os.Getuid() + 1)
		}
		return nil
	}
	_, err := EnsureToken(context.Background(), root)
	if err == nil || !strings.Contains(err.Error(), "owned by uid") {
		t.Fatal(err)
	}
}

func TestTokenReadIsBounded(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, TokenFile)
	if err := os.WriteFile(path, []byte(strings.Repeat("a", 4097)), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := EnsureToken(context.Background(), root)
	if err == nil || !strings.Contains(err.Error(), "not a token") {
		t.Fatal(err)
	}
}

func TestTokenModeIsRefusedWithoutChmod(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, TokenFile)
	if err := os.WriteFile(path, []byte(sampleToken+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := EnsureToken(context.Background(), root)
	if err == nil || !strings.Contains(err.Error(), "chmod 600") {
		t.Fatal(err)
	}
	info, statErr := os.Stat(path)
	if statErr != nil {
		t.Fatal(statErr)
	}
	if info.Mode().Perm() != 0o644 {
		t.Fatalf("mode changed to %o", info.Mode().Perm())
	}
}

func TestInvalidTokenNamesTheFile(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, TokenFile)
	if err := os.WriteFile(path, []byte("not a token!\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := EnsureToken(context.Background(), root)
	if err == nil {
		t.Fatal("invalid token was accepted")
	}
	if !strings.Contains(err.Error(), path) || !strings.Contains(err.Error(), "delete it and start looper serve again to make a new one") {
		t.Fatal(err)
	}
	var op *Error
	if !errors.As(err, &op) {
		t.Fatalf("error type %T", err)
	}
}

func TestTokenMustBeRegularFile(t *testing.T) {
	t.Run("symlink", func(t *testing.T) {
		root := t.TempDir()
		target := filepath.Join(root, "target")
		if err := os.WriteFile(target, []byte(sampleToken+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(root, TokenFile)
		if err := os.Symlink(target, path); err != nil {
			t.Fatal(err)
		}
		_, err := EnsureToken(context.Background(), root)
		if err == nil || !strings.Contains(err.Error(), "is a symbolic link") {
			t.Fatal(err)
		}
		body, readErr := os.ReadFile(target)
		if readErr != nil {
			t.Fatal(readErr)
		}
		if string(body) != sampleToken+"\n" {
			t.Fatal("symlink target was rewritten")
		}
	})
	t.Run("directory", func(t *testing.T) {
		root := t.TempDir()
		if err := os.Mkdir(filepath.Join(root, TokenFile), 0o700); err != nil {
			t.Fatal(err)
		}
		_, err := EnsureToken(context.Background(), root)
		if err == nil || !strings.Contains(err.Error(), "not a regular file") {
			t.Fatal(err)
		}
	})
}

func TestFIFODoesNotBlock(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, TokenFile)
	if err := unix.Mkfifo(path, 0o600); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		_, err := EnsureToken(context.Background(), root)
		done <- err
	}()
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "not a regular file") {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("opening the token file blocked on a FIFO")
	}
}

func TestReadTokenDoesNotCreate(t *testing.T) {
	root := t.TempDir()
	got, err := ReadToken(context.Background(), root)
	if err != nil || got != "" {
		t.Fatalf("got %q err %v", got, err)
	}
	if _, statErr := os.Stat(filepath.Join(root, TokenFile)); !os.IsNotExist(statErr) {
		t.Fatal("reader created the token file")
	}
}

func TestReadTokenRejectsInvalidUTF8(t *testing.T) {
	tests := []struct {
		name string
		body []byte
	}{
		{name: "lone continuation", body: []byte{0x80}},
		{name: "invalid lead", body: []byte{0xff, 0xfe}},
		{name: "truncated sequence", body: []byte{'t', 'o', 'k', 0xc3}},
		{name: "surrogate", body: []byte{0xed, 0xa0, 0x80}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			root := t.TempDir()
			path := filepath.Join(root, TokenFile)
			if err := os.WriteFile(path, tt.body, 0o600); err != nil {
				t.Fatal(err)
			}
			got, err := ReadToken(context.Background(), root)
			if err != nil || got != "" {
				t.Fatalf("got %q err %v", got, err)
			}
			if _, statErr := os.Stat(path); statErr != nil {
				t.Fatal(statErr)
			}
		})
	}
}

func TestReadTokenFollowsSymlinkWithoutValidating(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "target")
	if err := os.WriteFile(target, []byte("  short \n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(root, TokenFile)); err != nil {
		t.Fatal(err)
	}
	got, err := ReadToken(context.Background(), root)
	if err != nil || got != "short" {
		t.Fatalf("got %q err %v", got, err)
	}
}

func TestOpenedTokenSurvivesSwapToSymlink(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, TokenFile)
	if err := os.WriteFile(path, []byte(sampleToken), 0o600); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(root, "target")
	if err := os.WriteFile(target, []byte(otherToken), 0o600); err != nil {
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
	got, err := EnsureToken(context.Background(), root)
	if !swapped {
		t.Fatal("file was not swapped")
	}
	if err != nil {
		t.Fatal(err)
	}
	if got != sampleToken {
		t.Fatal("token changed after the path was replaced")
	}
	body, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != otherToken {
		t.Fatal("swap target was rewritten")
	}
}

func TestEnsureTokenConcurrent(t *testing.T) {
	root := t.TempDir()
	const n = 16
	tokens := make([]string, n)
	errs := make([]error, n)
	var wg sync.WaitGroup
	start := make(chan struct{})
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			<-start
			tokens[i], errs[i] = EnsureToken(context.Background(), root)
		}()
	}
	close(start)
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("goroutine %d: %v", i, err)
		}
		if tokens[i] == "" || tokens[i] != tokens[0] {
			t.Fatal("concurrent create did not share one token")
		}
	}
	info, err := os.Stat(filepath.Join(root, TokenFile))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("mode %o", info.Mode().Perm())
	}
	matches, err := filepath.Glob(filepath.Join(root, ".operator-token-*.tmp"))
	if err != nil {
		t.Fatal(err)
	}
	if len(matches) != 0 {
		t.Fatalf("temp files left behind: %d", len(matches))
	}
}

func TestCancelledContextDoesNotCreate(t *testing.T) {
	root := filepath.Join(t.TempDir(), "runtime")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := EnsureToken(ctx, root)
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if _, statErr := os.Stat(root); !os.IsNotExist(statErr) {
		t.Fatal("cancelled ensure created the runtime root")
	}
	if _, err := ReadToken(ctx, t.TempDir()); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
