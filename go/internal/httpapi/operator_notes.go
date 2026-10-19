package httpapi

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"unicode"
	"unicode/utf8"
)

const (
	notesRootEnv    = "LOOPER_OPERATOR_NOTES_ROOT"
	notesMaxBytes   = 24 * 1024
	notesIndex      = "MEMORY.md"
	notesIndexLines = 200
	notesNameMax    = 200
)

// noteLink is a Markdown link to a file in the memory directory.
var noteLink = regexp.MustCompile(`\[[^\]]*\]\(\s*<?([^)\s/<>]+\.md)>?\s*\)`)

const notesHeading = "## The operator's notes for this directory"
const notesLead = "The operator's own Claude Code keeps these notes about this directory; Looper gives you a read-only copy. " +
	"They are background from earlier sessions and may be out of date: where they disagree with your task or " +
	"with what the files say now, the task and the files win. Do not try to change them."

type operatorNotes struct {
	text    string
	files   []string
	omitted []string
	err     string
}

func emptyNotes() operatorNotes {
	return operatorNotes{files: []string{}, omitted: []string{}}
}

// notesView is operator_notes.notes_view. A missing memory directory is
// found false with empty lists. LOOPER_OPERATOR_NOTES_ROOT stands in for
// <config>/projects. Otherwise the root is $CLAUDE_CONFIG_DIR/projects, or
// ~/.claude/projects when that variable is unset. Tests set one of those
// so the operator's real directory is not read.
func notesView(ctx context.Context, workDir string) map[string]any {
	notes := readOperatorNotes(ctx, workDir)
	view := map[string]any{
		"found":   notes.text != "",
		"files":   notes.files,
		"omitted": notes.omitted,
	}
	if notes.err != "" {
		view["error"] = notes.err
	}
	return view
}

func notesRoot() string {
	if configured := os.Getenv(notesRootEnv); configured != "" {
		return expandUser(configured)
	}
	// operator_notes.notes_root: claude_config_dir() / "projects".
	// claude_config_dir is $CLAUDE_CONFIG_DIR, else ~/.claude.
	config := os.Getenv("CLAUDE_CONFIG_DIR")
	if config != "" {
		return filepath.Join(expandUser(config), "projects")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".claude", "projects")
}

func notesDir(ctx context.Context, workDir string) string {
	root := notesRoot()
	if root == "" {
		return ""
	}
	return filepath.Join(root, directoryName(notesKey(ctx, workDir)), "memory")
}

func readOperatorNotes(ctx context.Context, workDir string) operatorNotes {
	dir := notesDir(ctx, workDir)
	if dir == "" {
		return emptyNotes()
	}
	root, err := filepath.EvalSymlinks(dir)
	if err != nil {
		return emptyNotes()
	}
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return emptyNotes()
	}
	indexPath := filepath.Join(root, notesIndex)
	real, err := filepath.EvalSymlinks(indexPath)
	if err != nil {
		// A missing index is no notes. Search permission on memory/ is not
		// missing: Python reports that OSError and does not drop the error.
		return notesReadError(err)
	}
	if !pathInside(root, real) {
		return emptyNotes()
	}
	fileInfo, err := os.Stat(real)
	if err != nil {
		return notesReadError(err)
	}
	if !fileInfo.Mode().IsRegular() {
		return emptyNotes()
	}
	raw, err := os.ReadFile(real)
	if err != nil {
		return notesReadError(err)
	}
	// read_text translates \r\n and \r, then splitlines splits the rest
	// (\v, \f, \x1c–\x1e, U+0085, U+2028, U+2029). A trailing break is not
	// an extra line. Splitting on \n only keeps every link in a CR file.
	lines := pythonSplitLines(string(raw))
	if len(lines) > notesIndexLines {
		lines = lines[:notesIndexLines]
	}
	// Python joins [heading, lead, "", "### MEMORY.md", ""]. The last empty
	// piece is one trailing newline, not a blank line. A second newline
	// shrinks the budget by one byte and omits a file Python includes.
	prefix := notesHeading + "\n" + notesLead + "\n\n### " + notesIndex + "\n"
	budget := notesMaxBytes - len([]byte(prefix))
	index := strings.TrimSpace(strings.Join(lines, "\n"))
	index = clipUTF8(index, budget)
	names := linkedNames(index)
	footer := "\n\nNot included (past the " + itoa(notesMaxBytes/1024) + " KiB limit, or not readable): "
	reserved := 0
	if len(names) > 0 {
		reserved = len([]byte(footer + strings.Join(names, ", ")))
	}
	index = strings.TrimSpace(clipUTF8(index, max(0, budget-reserved)))
	if index == "" {
		return emptyNotes()
	}
	parts := []string{prefix + index}
	used := len([]byte(parts[0]))
	files := []string{notesIndex}
	var omitted []string
	for _, name := range linkedNames(index) {
		// An empty file is included. Python's block is the heading even when
		// the text strips to nothing. Only an unreadable file is omitted.
		body, ok := readNoteFile(filepath.Join(root, name), root, notesMaxBytes-used-reserved)
		if !ok {
			omitted = append(omitted, name)
			continue
		}
		block := "\n\n### " + name + "\n" + strings.TrimSpace(body)
		if used+len([]byte(block))+reserved > notesMaxBytes {
			omitted = append(omitted, name)
			continue
		}
		parts = append(parts, block)
		used += len([]byte(block))
		files = append(files, name)
	}
	if omitted == nil {
		omitted = []string{}
	}
	if len(omitted) > 0 {
		parts = append(parts, footer+strings.Join(omitted, ", "))
	}
	return operatorNotes{text: strings.Join(parts, ""), files: files, omitted: omitted}
}

func pythonSplitLines(s string) []string {
	lines := make([]string, 0)
	start := 0
	for i := 0; i < len(s); {
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == '\r' {
			lines = append(lines, s[start:i])
			i += size
			if i < len(s) && s[i] == '\n' {
				i++
			}
			start = i
			continue
		}
		if pythonLineBreak(r) {
			lines = append(lines, s[start:i])
			i += size
			start = i
			continue
		}
		i += size
	}
	if start < len(s) {
		lines = append(lines, s[start:])
	}
	return lines
}

func pythonLineBreak(r rune) bool {
	switch r {
	case '\n', '\v', '\f', 0x1c, 0x1d, 0x1e, '\u0085', '\u2028', '\u2029':
		return true
	default:
		return false
	}
}

// notesReadError is read_notes' OSError handler. FileNotFoundError is no
// notes. Any other error is the OS text only, so a client never sees the path.
func notesReadError(err error) operatorNotes {
	if os.IsNotExist(err) {
		return emptyNotes()
	}
	return operatorNotes{
		files:   []string{},
		omitted: []string{},
		err:     "cannot read the operator's notes: " + pythonOSMessage(err),
	}
}

// pythonOSMessage is OSError.strerror: the system text, without the path
// PathError puts in Error. Python capitalizes that text ("Permission denied").
func pythonOSMessage(err error) string {
	var pathErr *os.PathError
	if errors.As(err, &pathErr) && pathErr.Err != nil {
		err = pathErr.Err
	}
	var errno syscall.Errno
	if !errors.As(err, &errno) {
		return "input/output error"
	}
	text := errno.Error()
	if text == "" {
		return "input/output error"
	}
	r, size := utf8.DecodeRuneInString(text)
	return string(unicode.ToUpper(r)) + text[size:]
}

func linkedNames(index string) []string {
	seen := map[string]bool{}
	var names []string
	for _, match := range noteLink.FindAllStringSubmatch(index, -1) {
		name := match[1]
		if name == notesIndex || seen[name] {
			continue
		}
		seen[name] = true
		names = append(names, name)
	}
	return names
}

func readNoteFile(path, root string, budget int) (string, bool) {
	if budget < 0 {
		return "", false
	}
	real, err := filepath.EvalSymlinks(path)
	if err != nil {
		return "", false
	}
	if !pathInside(root, real) {
		return "", false
	}
	info, err := os.Stat(real)
	if err != nil || !info.Mode().IsRegular() || info.Size() > int64(budget) {
		return "", false
	}
	raw, err := os.ReadFile(real)
	if err != nil {
		return "", false
	}
	// Path.read_text translates newlines before the caller counts bytes.
	// The stat() gate above stays on the raw size.
	return pythonNewlines(string(raw)), true
}

// pythonNewlines is the universal-newline translation of Path.read_text.
// "\r\n" is one break. A lone "\r" is a break too.
func pythonNewlines(s string) string {
	s = strings.ReplaceAll(s, "\r\n", "\n")
	return strings.ReplaceAll(s, "\r", "\n")
}

func pathInside(root, path string) bool {
	root = strings.TrimRight(root, "/")
	if path == root {
		return true
	}
	return strings.HasPrefix(path, root+"/")
}

func clipUTF8(s string, n int) string {
	if n < 0 {
		n = 0
	}
	b := []byte(s)
	if len(b) <= n {
		return s
	}
	b = b[:n]
	for len(b) > 0 && !utf8.Valid(b) {
		b = b[:len(b)-1]
	}
	return string(b)
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var digits [16]byte
	i := len(digits)
	for n > 0 {
		i--
		digits[i] = byte('0' + n%10)
		n /= 10
	}
	return string(digits[i:])
}

func directoryName(key string) string {
	var b strings.Builder
	for _, r := range key {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
			continue
		}
		b.WriteByte('-')
	}
	name := b.String()
	if len(name) <= notesNameMax {
		return name
	}
	return name[:notesNameMax] + "-" + base36(absInt(djb2(key)))
}

func djb2(text string) int32 {
	var value uint32
	data := utf16LE(text)
	for i := 0; i+1 < len(data); i += 2 {
		unit := uint32(data[i]) | uint32(data[i+1])<<8
		// JavaScript (value << 5) - value + unit, in uint32. That is
		// value*31 + unit. value*33 is the other djb2 and names the wrong directory.
		value = value*31 + unit
	}
	return int32(value)
}

func utf16LE(s string) []byte {
	var out []byte
	for _, r := range s {
		if r > 0xFFFF {
			r -= 0x10000
			hi := uint16(0xD800 + (r >> 10))
			lo := uint16(0xDC00 + (r & 0x3FF))
			out = append(out, byte(hi), byte(hi>>8), byte(lo), byte(lo>>8))
			continue
		}
		out = append(out, byte(r), byte(r>>8))
	}
	return out
}

func base36(n int) string {
	digits := "0123456789abcdefghijklmnopqrstuvwxyz"
	if n == 0 {
		return "0"
	}
	var out []byte
	for n > 0 {
		out = append(out, digits[n%36])
		n /= 36
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return string(out)
}

func absInt(n int32) int {
	v := int(n)
	if v < 0 {
		return -v
	}
	return v
}

// notesKey is operator_notes.notes_key. Git is not asked when the directory
// is missing. A failure returns the real path of the directory itself.
func notesKey(ctx context.Context, workDir string) string {
	directory := realpath(workDir)
	info, err := os.Stat(directory)
	if err != nil || !info.IsDir() {
		return directory
	}
	run, err := runGit(ctx, directory, notesGitTimeout, false, "rev-parse", "--path-format=absolute", "--git-common-dir")
	if err != nil {
		return directory
	}
	common := strings.TrimSpace(string(run.stdout))
	if run.code != 0 || filepath.Base(common) != ".git" {
		return directory
	}
	return realpath(filepath.Dir(common))
}
