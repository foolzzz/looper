package registry

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
)

// Placeholders the parity goldens show. PrepareParityLogin creates a unique
// directory and RewriteParityPaths puts these back before a body is compared.
// The names stay stable. The directories on disk do not.
const (
	ParityBinPlaceholder       = "/tmp/loopgo-parity-bin"
	ParityCodexHomePlaceholder = "/tmp/loopgo-parity-codex-home"
	ParityGrokHomePlaceholder  = "/tmp/loopgo-parity-grok-home"
	ParityEmptyHomePlaceholder = "/tmp/loopgo-parity-empty-home"
)

// parityHomes are directories created under the unique root, and the
// placeholder each one is rewritten to.
var parityHomes = []struct {
	name        string
	placeholder string
}{
	{"codex-home", ParityCodexHomePlaceholder},
	{"grok-home", ParityGrokHomePlaceholder},
	{"empty-home", ParityEmptyHomePlaceholder},
}

type parityPaths struct {
	root      string
	bin       string
	codexHome string
	grokHome  string
	emptyHome string
}

var (
	parityMu sync.Mutex
	parity   parityPaths
)

// ParityRoot is the unique directory from the last PrepareParityLogin.
func ParityRoot() string {
	parityMu.Lock()
	defer parityMu.Unlock()
	return parity.root
}

// ParityBin is the directory of the fake claude, codex, omp, and grok.
func ParityBin() string {
	parityMu.Lock()
	defer parityMu.Unlock()
	return parity.bin
}

// ParityClaude is the fake claude the registry read uses as the server binary.
func ParityClaude() string {
	return filepath.Join(ParityBin(), "claude")
}

// ParityCodexHome is the empty CODEX_HOME for a parity registry read.
func ParityCodexHome() string {
	parityMu.Lock()
	defer parityMu.Unlock()
	return parity.codexHome
}

// ParityGrokHome is the empty GROK_HOME for a parity registry read.
func ParityGrokHome() string {
	parityMu.Lock()
	defer parityMu.Unlock()
	return parity.grokHome
}

// ParityEmptyHome is HOME for a parity registry read.
func ParityEmptyHome() string {
	parityMu.Lock()
	defer parityMu.Unlock()
	return parity.emptyHome
}

// ParityEnviron is the environment a registry read uses in the parity harness.
// PATH finds the fake codex. CODEX_HOME and GROK_HOME are empty directories.
func ParityEnviron() []string {
	return []string{
		"PATH=" + ParityBin(),
		"CODEX_HOME=" + ParityCodexHome(),
		"GROK_HOME=" + ParityGrokHome(),
		"HOME=" + ParityEmptyHome(),
	}
}

// PrepareParityLogin creates the fake executables and empty login homes in a
// new directory. It does not use a fixed path under /tmp: a leftover symlink
// there would make a write or an auth.json removal follow the link. Writes
// use O_EXCL, so an existing name is not opened. Removing auth.json uses
// os.Root, which unlinks that name and does not delete the file it points at.
func PrepareParityLogin() error {
	closeParityLogin()
	dir, err := os.MkdirTemp("", "loopgo-parity-login-")
	if err != nil {
		return fmt.Errorf("parity login: %w", err)
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return fmt.Errorf("parity login: %w", err)
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		os.Remove(dir)
		return fmt.Errorf("parity login root is not a directory")
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		removeParityRoot(dir)
		return fmt.Errorf("parity login: %w", err)
	}
	defer root.Close()
	if err := root.Mkdir("bin", 0o755); err != nil {
		removeParityRoot(dir)
		return fmt.Errorf("parity bin: %w", err)
	}
	codex := []byte("#!/bin/sh\necho 'Not logged in' >&2\nexit 1\n")
	others := []byte("#!/bin/sh\nexit 0\n")
	for _, name := range []string{"claude", "codex", "omp", "grok"} {
		body := others
		if name == "codex" {
			body = codex
		}
		if err := writeExec(root, filepath.Join("bin", name), body); err != nil {
			removeParityRoot(dir)
			return fmt.Errorf("parity %s: %w", name, err)
		}
	}
	homes := map[string]string{}
	for _, home := range parityHomes {
		if err := root.Mkdir(home.name, 0o755); err != nil {
			removeParityRoot(dir)
			return fmt.Errorf("parity home: %w", err)
		}
		if err := removeWithin(root, filepath.Join(home.name, "auth.json")); err != nil {
			removeParityRoot(dir)
			return fmt.Errorf("parity auth.json: %w", err)
		}
		homes[home.name] = filepath.Join(dir, home.name)
	}
	parityMu.Lock()
	parity = parityPaths{
		root:      dir,
		bin:       filepath.Join(dir, "bin"),
		codexHome: homes["codex-home"],
		grokHome:  homes["grok-home"],
		emptyHome: homes["empty-home"],
	}
	parityMu.Unlock()
	return nil
}

// CloseParityLogin removes the directory from the last PrepareParityLogin.
// A symlink left in its place is unlinked and not followed.
func CloseParityLogin() {
	closeParityLogin()
}

func closeParityLogin() {
	parityMu.Lock()
	dir := parity.root
	parity = parityPaths{}
	parityMu.Unlock()
	removeParityRoot(dir)
}

func removeParityRoot(dir string) {
	if dir == "" {
		return
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return
	}
	if info.Mode()&os.ModeSymlink != 0 {
		os.Remove(dir)
		return
	}
	os.RemoveAll(dir)
}

// writeExec creates name inside root. O_EXCL fails when the name exists, so a
// symlink is not opened and its target is not written.
func writeExec(root *os.Root, name string, body []byte) error {
	f, err := root.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o755)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write(body); err != nil {
		return err
	}
	return nil
}

// removeWithin removes name inside root. os.Root.Remove does not follow a
// symlink in the final component, so auth.json that points at a credential
// file unlinks the link.
func removeWithin(root *os.Root, name string) error {
	err := root.Remove(name)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// RewriteParityPaths replaces the unique parity directories with the
// placeholders the goldens use. An empty path is left unchanged.
func RewriteParityPaths(body []byte) []byte {
	pairs := [][2]string{
		{ParityBin(), ParityBinPlaceholder},
		{ParityCodexHome(), ParityCodexHomePlaceholder},
		{ParityGrokHome(), ParityGrokHomePlaceholder},
		{ParityEmptyHome(), ParityEmptyHomePlaceholder},
	}
	sort.Slice(pairs, func(i, j int) bool {
		return len(pairs[i][0]) > len(pairs[j][0])
	})
	for _, pair := range pairs {
		if pair[0] == "" || pair[0] == pair[1] {
			continue
		}
		body = bytes.ReplaceAll(body, []byte(pair[0]), []byte(pair[1]))
	}
	return body
}
