package registry

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// Which is shutil.which for one command. env is the environment to read PATH
// from; it is not os.Environ unless the caller passes that. A command with a
// slash is that path when it is executable, and nothing otherwise. An empty
// command is not found. A missing PATH is not found. An empty PATH entry and
// "." name the working directory. filepath.Join cleans both to a bare name,
// and exec.Command would then search the process PATH, so those matches are
// returned as absolute paths.
func Which(command string, env []string) string {
	if command == "" {
		return ""
	}
	if strings.Contains(command, "/") {
		if isExecutable(command) {
			return command
		}
		return ""
	}
	path, ok := envValue(env, "PATH")
	if !ok {
		return ""
	}
	for _, dir := range strings.Split(path, string(os.PathListSeparator)) {
		name := command
		if dir != "" {
			name = filepath.Join(dir, command)
		}
		if !isExecutable(name) {
			continue
		}
		// exec.Command looks up a bare name on the process PATH before cmd.Env
		// exists. "." and an empty entry both produce that bare name.
		if filepath.Base(name) == name {
			name = absoluteCommand(name)
			if name == "" {
				continue
			}
		}
		return name
	}
	return ""
}

// absoluteCommand resolves a bare executable against the working directory.
// The result is empty when it is not an absolute executable.
func absoluteCommand(name string) string {
	abs, err := filepath.Abs(name)
	if err != nil || !filepath.IsAbs(abs) || filepath.Base(abs) == abs || !isExecutable(abs) {
		return ""
	}
	return abs
}

// xOK is the POSIX X_OK bit. The Darwin syscall package does not export it.
const xOK = 1

func isExecutable(path string) bool {
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return false
	}
	return syscall.Access(path, xOK) == nil
}

// RuntimePath is runtime_path. serverClaude is the Claude Code executable the
// server was started with; an empty executable on claude-code uses it. An
// unknown type uses the stored executable, or nothing when that is empty.
// The result is nil when the command is not found, which JSON writes as null.
func RuntimePath(runtimeType, executable, serverClaude string, env []string) any {
	command, ok := probeCommand(runtimeType, executable, serverClaude)
	if !ok {
		command = executable
	}
	if command == "" {
		return nil
	}
	found := Which(command, env)
	if found == "" {
		return nil
	}
	return found
}

func probeCommand(runtimeType, executable, serverClaude string) (string, bool) {
	if executable != "" {
		return executable, true
	}
	adapter, ok := AdapterByType(runtimeType)
	if !ok {
		return "", false
	}
	if adapter.Type == "claude-code" {
		if serverClaude == "" {
			return "", true
		}
		return serverClaude, true
	}
	return adapter.DefaultExecutable, true
}

// envValue is the last value of key, which is how a process environment
// resolves a repeated name. The second result is false when key is absent.
func envValue(env []string, key string) (string, bool) {
	prefix := key + "="
	found := false
	value := ""
	for _, item := range env {
		if strings.HasPrefix(item, prefix) {
			value = item[len(prefix):]
			found = true
		}
	}
	return value, found
}
