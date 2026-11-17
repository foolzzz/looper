package operatorauth

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

const passwordParityScript = `
import base64, json, os, stat, sys
from pathlib import Path
sys.path.insert(0, os.environ["LOOPER_ROOT"])
from looper.agents import operator_auth

req = json.load(sys.stdin)
root = Path(req["root"])
op = req["op"]

def emit(obj):
    json.dump(obj, sys.stdout)
    sys.stdout.write("\n")

def record_view(text, password):
    data = json.loads(text)
    salt = base64.b64decode(data["salt"], altchars=b"-_", validate=True)
    digest = base64.b64decode(data["hash"], altchars=b"-_", validate=True)
    return {
        "match": text == json.dumps(data) + "\n",
        "mode": stat.S_IMODE(Path(req["root"], operator_auth.PASSWORD_FILE).stat().st_mode),
        "password_absent": password not in text,
        "user": data["user"],
        "scheme": data["scheme"],
        "n": data["n"],
        "r": data["r"],
        "p": data["p"],
        "salt_len": len(salt),
        "hash_len": len(digest),
        "lines": len(text.splitlines()),
    }

try:
    if op == "set":
        operator_auth.set_password(root, req["user"], req["password"])
        text = (root / operator_auth.PASSWORD_FILE).read_text()
        emit(record_view(text, req["password"]))
    elif op == "check":
        text = (root / operator_auth.PASSWORD_FILE).read_text()
        view = record_view(text, req["password"])
        rec = operator_auth.load_password(root)
        view["verify"] = operator_auth.verify(rec, req["user"], req["password"])
        view["wrong_user"] = operator_auth.verify(rec, "wrong-user", req["password"])
        view["wrong_password"] = operator_auth.verify(rec, req["user"], req["password"] + "x")
        emit(view)
    elif op == "load":
        try:
            rec = operator_auth.load_password(root)
        except operator_auth.OperatorTokenError as exc:
            emit({"error": str(exc)})
        else:
            emit({"missing": rec is None, "user": None if rec is None else rec.user,
                  "salt": None if rec is None else rec.salt.hex(),
                  "hash": None if rec is None else rec.hash.hex()})
    elif op == "b64":
        rows = []
        for text in req["samples"]:
            try:
                raw = base64.b64decode(text, altchars=b"-_", validate=True)
            except Exception as exc:
                rows.append({"ok": False, "error": type(exc).__name__})
            else:
                rows.append({"ok": True, "hex": raw.hex()})
        emit({"rows": rows})
    elif op == "plant":
        path = root / operator_auth.PASSWORD_FILE
        root.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.exists():
            path.unlink()
        kind = req["kind"]
        if kind == "fifo":
            os.mkfifo(path, 0o600)
        elif kind == "symlink":
            target = root / "target"
            target.write_text("nope")
            path.symlink_to(target)
        elif kind == "mode":
            path.write_text("{}\n")
            path.chmod(0o644)
        elif kind == "corrupt":
            path.write_bytes(req["body"].encode("utf-8"))
            path.chmod(0o600)
        else:
            raise ValueError(kind)
        emit({"ok": True})
    else:
        emit({"error": "unknown op"})
except Exception as exc:
    emit({"error": type(exc).__name__ + ": " + str(exc)})
`

func TestPasswordFixtureParity(t *testing.T) {
	repo := passwordRepoRoot(t)
	bin := passwordPython(t, repo)
	ctx := context.Background()
	cases := []struct {
		user     string
		password string
	}{
		{user: "a", password: "12345678"},
		{user: strings.Repeat("a", 64), password: strings.Repeat("密码", 4)},
		{user: "A-_.0", password: samplePass},
	}
	for _, tt := range cases {
		t.Run("python writes "+tt.user, func(t *testing.T) {
			root := t.TempDir()
			view := runPasswordPython(t, bin, repo, map[string]any{
				"op": "set", "root": root, "user": tt.user, "password": tt.password,
			})
			assertPasswordView(t, view, tt.user, tt.password)
			rec, err := LoadPassword(ctx, root)
			if err != nil {
				t.Fatal(err)
			}
			ok, err := Verify(ctx, rec, tt.user, tt.password)
			if err != nil || !ok {
				t.Fatalf("verify %v %v", ok, err)
			}
			ok, err = Verify(ctx, rec, "wrong-user", tt.password)
			if err != nil || ok {
				t.Fatalf("wrong user %v %v", ok, err)
			}
			ok, err = Verify(ctx, rec, tt.user, tt.password+"x")
			if err != nil || ok {
				t.Fatalf("wrong password %v %v", ok, err)
			}
			first := append([]byte(nil), rec.Salt...)
			runPasswordPython(t, bin, repo, map[string]any{
				"op": "set", "root": root, "user": tt.user, "password": tt.password,
			})
			again, err := LoadPassword(ctx, root)
			if err != nil {
				t.Fatal(err)
			}
			if bytes.Equal(first, again.Salt) {
				t.Fatal("python reused the salt")
			}
		})
		t.Run("go writes "+tt.user, func(t *testing.T) {
			root := t.TempDir()
			if err := SetPassword(ctx, root, tt.user, tt.password); err != nil {
				t.Fatal(err)
			}
			view := runPasswordPython(t, bin, repo, map[string]any{
				"op": "check", "root": root, "user": tt.user, "password": tt.password,
			})
			assertPasswordView(t, view, tt.user, tt.password)
			if view["verify"] != true || view["wrong_user"] != false || view["wrong_password"] != false {
				t.Fatalf("python verify %#v", view)
			}
		})
	}

	t.Run("corrupt both ways", func(t *testing.T) {
		root := t.TempDir()
		runPasswordPython(t, bin, repo, map[string]any{
			"op": "plant", "root": root, "kind": "corrupt", "body": "not-json",
		})
		_, err := LoadPassword(ctx, root)
		if err == nil || !strings.Contains(err.Error(), "not a password record") {
			t.Fatal(err)
		}
		goRoot := t.TempDir()
		if err := SetPassword(ctx, goRoot, sampleUser, samplePass); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(goRoot, PasswordFile), []byte("null"), 0o600); err != nil {
			t.Fatal(err)
		}
		view := runPasswordPython(t, bin, repo, map[string]any{"op": "load", "root": goRoot})
		msg, _ := view["error"].(string)
		if !strings.Contains(msg, "not a password record") {
			t.Fatalf("%#v", view)
		}
	})

	t.Run("unsafe files both ways", func(t *testing.T) {
		for _, kind := range []string{"symlink", "fifo", "mode"} {
			root := t.TempDir()
			runPasswordPython(t, bin, repo, map[string]any{"op": "plant", "root": root, "kind": kind})
			_, err := LoadPassword(ctx, root)
			if err == nil || !strings.Contains(err.Error(), filepath.Join(root, PasswordFile)) || !strings.Contains(err.Error(), passwordUnsafePhrase(kind)) {
				t.Fatalf("%s: %v", kind, err)
			}
			goRoot := t.TempDir()
			path := filepath.Join(goRoot, PasswordFile)
			switch kind {
			case "symlink":
				target := filepath.Join(goRoot, "target")
				if err := os.WriteFile(target, []byte("nope"), 0o600); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(target, path); err != nil {
					t.Fatal(err)
				}
			case "fifo":
				if err := unix.Mkfifo(path, 0o600); err != nil {
					t.Fatal(err)
				}
			case "mode":
				if err := os.WriteFile(path, []byte("{}\n"), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			view := runPasswordPython(t, bin, repo, map[string]any{"op": "load", "root": goRoot})
			msg, _ := view["error"].(string)
			if !strings.Contains(msg, path) || !strings.Contains(msg, passwordUnsafePhrase(kind)) {
				t.Fatalf("%s python %#v", kind, view)
			}
		}
	})

	t.Run("base64 differentials", func(t *testing.T) {
		// Python accepts leftover padding bits and rejects CR/LF.
		// Go's Strict decoder does the opposite; pythonB64Decode must follow Python.
		samples := []string{
			"AAAAAAAAAAAAAAAAAAAAAB==",
			"AAAAAAAAAAAAAAAAAAAAAA==",
			"AAAAAAAAAAAAAAAAAAAAAA==\n",
			"AAAAAAAAAAAAAAAAAAAAAA==\r",
			"AAAAAAAAAAAAAAAAAAAAAA==\r\n",
			"AAAA\nAAAAAAAAAAAAAAAAAA==",
			"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB=",
			"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n",
			"AAAA====",
			"AA====",
			"Ä",
		}
		view := runPasswordPython(t, bin, repo, map[string]any{"op": "b64", "root": t.TempDir(), "samples": samples})
		rows, _ := view["rows"].([]any)
		if len(rows) != len(samples) {
			t.Fatalf("%#v", view)
		}
		for i, sample := range samples {
			row, _ := rows[i].(map[string]any)
			got, err := pythonB64Decode(sample)
			pythonOK, _ := row["ok"].(bool)
			if pythonOK != (err == nil) {
				t.Fatalf("%q python %#v go %v", sample, row, err)
			}
			if !pythonOK {
				continue
			}
			if hex.EncodeToString(got) != row["hex"] {
				t.Fatalf("%q python %v go %s", sample, row["hex"], hex.EncodeToString(got))
			}
		}

		const (
			salt = "AAAAAAAAAAAAAAAAAAAAAB=="
			sum  = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB="
		)
		body := `{"user":"a","scheme":"scrypt","n":16384,"r":1,"p":1,"salt":"` + salt + `","hash":"` + sum + `"}` + "\n"
		root := t.TempDir()
		runPasswordPython(t, bin, repo, map[string]any{"op": "plant", "root": root, "kind": "corrupt", "body": body})
		rec, err := LoadPassword(ctx, root)
		if err != nil {
			t.Fatal(err)
		}
		view = runPasswordPython(t, bin, repo, map[string]any{"op": "load", "root": root})
		if view["user"] != "a" || view["salt"] != hex.EncodeToString(rec.Salt) || view["hash"] != hex.EncodeToString(rec.Hash) {
			t.Fatalf("go %#v python %#v", rec, view)
		}
		if !bytes.Equal(rec.Salt, bytes.Repeat([]byte{0}, saltLen)) || !bytes.Equal(rec.Hash, bytes.Repeat([]byte{0}, scryptLen)) {
			t.Fatalf("padding bits leaked salt %x hash %x", rec.Salt, rec.Hash)
		}

		newline := `{"user":"a","scheme":"scrypt","n":16384,"r":1,"p":1,"salt":"AAAAAAAAAAAAAAAAAAAAAA==\n","hash":"` + sum + `"}` + "\n"
		nlRoot := t.TempDir()
		runPasswordPython(t, bin, repo, map[string]any{"op": "plant", "root": nlRoot, "kind": "corrupt", "body": newline})
		if _, err := LoadPassword(ctx, nlRoot); err == nil || !strings.Contains(err.Error(), "not a password record") {
			t.Fatal(err)
		}
		view = runPasswordPython(t, bin, repo, map[string]any{"op": "load", "root": nlRoot})
		msg, _ := view["error"].(string)
		if !strings.Contains(msg, "not a password record") {
			t.Fatalf("%#v", view)
		}
	})

	t.Run("json encodings", func(t *testing.T) {
		for _, tt := range passwordEncodingCases() {
			t.Run(tt.name, func(t *testing.T) {
				root := t.TempDir()
				path := filepath.Join(root, PasswordFile)
				if err := os.WriteFile(path, tt.body, 0o600); err != nil {
					t.Fatal(err)
				}
				rec, goErr := LoadPassword(ctx, root)
				view := runPasswordPython(t, bin, repo, map[string]any{"op": "load", "root": root})
				pythonMsg, pythonBad := view["error"].(string)
				if (goErr == nil) != tt.ok || pythonBad != !tt.ok {
					t.Fatalf("ok %v go %v python %#v", tt.ok, goErr, view)
				}
				if !tt.ok {
					if goErr == nil || !strings.Contains(goErr.Error(), "not a password record") || !strings.Contains(pythonMsg, "not a password record") {
						t.Fatalf("go %v python %#v", goErr, view)
					}
					return
				}
				if view["user"] != rec.User || view["salt"] != hex.EncodeToString(rec.Salt) || view["hash"] != hex.EncodeToString(rec.Hash) {
					t.Fatalf("go user %s salt %x hash %x python %#v", rec.User, rec.Salt, rec.Hash, view)
				}
			})
		}
	})
}

func passwordUnsafePhrase(kind string) string {
	switch kind {
	case "symlink":
		return "symbolic link"
	case "fifo":
		return "not a regular file"
	case "mode":
		return "chmod 600"
	default:
		return kind
	}
}

func assertPasswordView(t *testing.T, view map[string]any, user, password string) {
	t.Helper()
	if view["match"] != true || view["password_absent"] != true || view["lines"] != float64(1) {
		t.Fatalf("file %#v", view)
	}
	if view["mode"] != float64(0o600) || view["user"] != user || view["scheme"] != "scrypt" {
		t.Fatalf("meta %#v", view)
	}
	if view["n"] != float64(scryptN) || view["r"] != float64(scryptR) || view["p"] != float64(scryptP) {
		t.Fatalf("params %#v", view)
	}
	if view["salt_len"] != float64(saltLen) || view["hash_len"] != float64(scryptLen) {
		t.Fatalf("lengths %#v", view)
	}
	if strings.Contains(mustString(view["error"]), password) {
		t.Fatal("python error contains the password")
	}
}

func mustString(v any) string {
	s, _ := v.(string)
	return s
}

func runPasswordPython(t *testing.T, bin, repo string, req map[string]any) map[string]any {
	t.Helper()
	raw, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, "-c", passwordParityScript)
	cmd.Dir = repo
	cmd.Env = append(os.Environ(), "PYTHONPATH="+repo, "LOOPER_ROOT="+repo)
	cmd.Stdin = bytes.NewReader(raw)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("python: %v\n%s", err, stderr.String())
	}
	var view map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &view); err != nil {
		t.Fatalf("python output %q: %v", stdout.String(), err)
	}
	if msg, ok := view["error"].(string); ok && req["op"] != "load" {
		t.Fatalf("python %s", msg)
	}
	return view
}

func passwordRepoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "looper", "agents", "operator_auth.py")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("looper/agents/operator_auth.py not found")
		}
		dir = parent
	}
}

func passwordPython(t *testing.T, root string) string {
	t.Helper()
	candidates := []string{
		filepath.Join(root, ".venv", "bin", "python"),
		"python3.11",
		"python3",
	}
	for _, bin := range candidates {
		path := bin
		if !strings.Contains(bin, "/") {
			found, err := exec.LookPath(bin)
			if err != nil {
				continue
			}
			path = found
		} else if _, err := os.Stat(bin); err != nil {
			continue
		}
		cmd := exec.Command(path, "-c", "import looper.agents.operator_auth")
		cmd.Dir = root
		cmd.Env = append(os.Environ(), "PYTHONPATH="+root)
		if cmd.Run() == nil {
			return path
		}
	}
	t.Fatal("python with looper.agents.operator_auth is required")
	return ""
}
