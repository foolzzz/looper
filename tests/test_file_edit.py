"""looper-edit replaces text or applies a unified diff, and writes nothing when the input does not match."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def run(cwd: Path, args: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "looper.file_edit", *args],
        cwd=cwd,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def replace_json(old: str, new: str, count: int | None = None) -> str:
    payload: dict[str, object] = {"old": old, "new": new}
    if count is not None:
        payload["count"] = count
    return json.dumps(payload)


def test_replace_one_match(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_text("pre a.b post\n", encoding="utf-8")
    path.chmod(0o640)
    result = run(tmp_path, ["replace", "note.txt"], replace_json("a.b", "A.B"))
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.stderr == ""
    assert path.read_text(encoding="utf-8") == "pre A.B post\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_replace_zero_matches(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_text("alpha\n", encoding="utf-8")
    before = path.stat().st_mtime_ns
    result = run(tmp_path, ["replace", "note.txt"], replace_json("missing", "nope"))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: no match in note.txt\n"
    assert path.read_text(encoding="utf-8") == "alpha\n"
    assert path.stat().st_mtime_ns == before


def test_replace_too_many_matches_reports_lines(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_text(
        "one\ntwo\nthree\ntarget\nfour\nfive\ntarget\nsix\nseven\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    result = run(tmp_path, ["replace", "note.txt"], replace_json("target", "TARGET", 1))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "error: 2 matches in note.txt, count is 1\n"
        "match line 4\n"
        "2|two\n"
        "3|three\n"
        "4|target\n"
        "5|four\n"
        "6|five\n"
        "match line 7\n"
        "5|four\n"
        "6|five\n"
        "7|target\n"
        "8|six\n"
        "9|seven\n"
    )
    assert path.read_bytes() == before


def test_replace_count_above_one_replaces_every_match(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_text("token\nother\ntoken\n", encoding="utf-8")
    result = run(tmp_path, ["replace", "note.txt"], replace_json("token", "TOKEN", 3))
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert path.read_text(encoding="utf-8") == "TOKEN\nother\nTOKEN\n"


def test_replace_creates_a_missing_file_when_old_is_empty(tmp_path: Path):
    result = run(tmp_path, ["replace", "fresh.txt"], replace_json("", "hello\n"))
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert (tmp_path / "fresh.txt").read_bytes() == b"hello\n"


def test_replace_rejects_empty_old_on_an_existing_file(tmp_path: Path):
    path = tmp_path / "fresh.txt"
    path.write_text("keep", encoding="utf-8")
    result = run(tmp_path, ["replace", "fresh.txt"], replace_json("", "hello"))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: empty old only creates a missing file\n"
    assert path.read_text(encoding="utf-8") == "keep"


def test_replace_keeps_crlf_and_mode(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    path.chmod(0o640)
    result = run(tmp_path, ["replace", "note.txt"], replace_json("beta", "BETA"))
    assert result.returncode == 0
    assert path.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_replace_unicode(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_text("前缀 你好 后缀\n", encoding="utf-8")
    result = run(tmp_path, ["replace", "note.txt"], replace_json("你好", "世界"))
    assert result.returncode == 0
    assert path.read_text(encoding="utf-8") == "前缀 世界 后缀\n"


def test_replace_follows_a_symlink_inside_cwd(tmp_path: Path):
    real = tmp_path / "real.txt"
    real.write_text("alpha", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to("real.txt")
    result = run(tmp_path, ["replace", "link.txt"], replace_json("alpha", "beta"))
    assert result.returncode == 0
    assert real.read_text(encoding="utf-8") == "beta"
    assert link.is_symlink()


def test_replace_allows_absolute_and_dotdot_inside_cwd(tmp_path: Path):
    nested = tmp_path / "sub"
    nested.mkdir()
    path = tmp_path / "inside.txt"
    path.write_text("alpha", encoding="utf-8")
    absolute = run(tmp_path, ["replace", str(path)], replace_json("alpha", "beta"))
    assert absolute.returncode == 0
    assert path.read_text(encoding="utf-8") == "beta"
    dotted = run(tmp_path, ["replace", "sub/../inside.txt"], replace_json("beta", "gamma"))
    assert dotted.returncode == 0
    assert path.read_text(encoding="utf-8") == "gamma"


def test_patch_updates_every_file(tmp_path: Path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("keep\nold\n", encoding="utf-8")
    second.write_text("before\n", encoding="utf-8")
    first.chmod(0o640)
    diff = """diff --git a/a.txt b/a.txt
index 1111111..2222222 100644
--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 keep
-old
+new
diff --git a/b.txt b/b.txt
index 3333333..4444444 100644
--- a/b.txt
+++ b/b.txt
@@ -1 +1 @@
-before
+after
"""
    result = run(tmp_path, ["patch"], diff)
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.stderr == ""
    assert first.read_text(encoding="utf-8") == "keep\nnew\n"
    assert second.read_text(encoding="utf-8") == "after\n"
    assert stat.S_IMODE(first.stat().st_mode) == 0o640


def test_patch_hunk_mismatch_leaves_every_file_unchanged(tmp_path: Path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("keep\nold\n", encoding="utf-8")
    second.write_text("before\n", encoding="utf-8")
    before = (first.read_bytes(), first.stat().st_mtime_ns, second.read_bytes(), second.stat().st_mtime_ns)
    diff = """--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 keep
-old
+new
--- a/b.txt
+++ b/b.txt
@@ -1 +1 @@
-missing
+after
"""
    result = run(tmp_path, ["patch"], diff)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "hunk mismatch in b.txt" in result.stderr
    assert (first.read_bytes(), first.stat().st_mtime_ns, second.read_bytes(), second.stat().st_mtime_ns) == before


def test_patch_context_mismatch_shows_expected_and_actual(tmp_path: Path):
    path = tmp_path / "b.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    diff = """--- a/b.txt
+++ b/b.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 four
"""
    result = run(tmp_path, ["patch"], diff)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "error: hunk mismatch in b.txt\n"
        "hunk:\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        " four\n"
        "expected:\n"
        "one\ntwo\nfour\n"
        "actual:\n"
        "one\ntwo\nthree\n"
    )
    assert path.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_patch_creates_and_deletes_files(tmp_path: Path):
    removed = tmp_path / "removed.txt"
    kept = tmp_path / "kept.txt"
    removed.write_text("gone\n", encoding="utf-8")
    kept.write_text("stay\n", encoding="utf-8")
    diff = """diff --git a/created.txt b/created.txt
new file mode 100755
index 0000000..1111111
--- /dev/null
+++ b/created.txt
@@ -0,0 +1 @@
+created
diff --git a/removed.txt b/removed.txt
deleted file mode 100644
index 2222222..0000000
--- a/removed.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""
    result = run(tmp_path, ["patch"], diff)
    assert result.returncode == 0
    assert result.stdout == "ok\n"
    created = tmp_path / "created.txt"
    assert created.read_text(encoding="utf-8") == "created\n"
    assert stat.S_IMODE(created.stat().st_mode) == 0o755
    assert not removed.exists()
    assert kept.read_text(encoding="utf-8") == "stay\n"


def test_patch_refuses_to_create_an_existing_file(tmp_path: Path):
    created = tmp_path / "created.txt"
    other = tmp_path / "other.txt"
    created.write_text("keep", encoding="utf-8")
    other.write_text("before\n", encoding="utf-8")
    diff = """--- /dev/null
+++ b/created.txt
@@ -0,0 +1 @@
+created
--- a/other.txt
+++ b/other.txt
@@ -1 +1 @@
-before
+after
"""
    result = run(tmp_path, ["patch"], diff)
    assert result.returncode == 1
    assert "already exists: created.txt" in result.stderr
    assert created.read_text(encoding="utf-8") == "keep"
    assert other.read_text(encoding="utf-8") == "before\n"


def test_patch_preserves_crlf(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    diff = """--- a/note.txt
+++ b/note.txt
@@ -1,3 +1,3 @@
 alpha
-beta
+BETA
 gamma
"""
    result = run(tmp_path, ["patch"], diff)
    assert result.returncode == 0
    assert path.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_patch_keeps_a_missing_final_newline(tmp_path: Path):
    path = tmp_path / "note.txt"
    path.write_bytes(b"alpha\nbeta")
    diff = """--- a/note.txt
+++ b/note.txt
@@ -1,2 +1,2 @@
 alpha
-beta
\\ No newline at end of file
+BETA
\\ No newline at end of file
"""
    result = run(tmp_path, ["patch"], diff)
    assert result.returncode == 0
    assert path.read_bytes() == b"alpha\nBETA"


def test_paths_outside_cwd_are_refused(tmp_path: Path):
    inside = tmp_path / "inside.txt"
    inside.write_text("keep", encoding="utf-8")
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir()
    outside = outside_dir / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    dir_link = tmp_path / "out"
    dir_link.symlink_to(outside_dir, target_is_directory=True)
    try:
        relative = run(
            tmp_path,
            ["replace", f"../{outside_dir.name}/secret.txt"],
            replace_json("secret", "nope"),
        )
        assert relative.returncode == 1
        assert relative.stderr == f"error: path escapes cwd: ../{outside_dir.name}/secret.txt\n"

        absolute = run(tmp_path, ["replace", str(outside)], replace_json("secret", "nope"))
        assert absolute.returncode == 1
        assert absolute.stderr == f"error: path escapes cwd: {outside}\n"

        through_link = run(tmp_path, ["replace", "link.txt"], replace_json("secret", "nope"))
        assert through_link.returncode == 1
        assert through_link.stderr == "error: path escapes cwd: link.txt\n"
        assert link.is_symlink()

        through_dir = run(tmp_path, ["replace", "out/secret.txt"], replace_json("secret", "nope"))
        assert through_dir.returncode == 1
        assert through_dir.stderr == "error: path escapes cwd: out/secret.txt\n"

        diff = (
            f"--- a/../{outside_dir.name}/secret.txt\n"
            f"+++ b/../{outside_dir.name}/secret.txt\n"
            "@@ -1 +1 @@\n"
            "-secret\n"
            "+nope\n"
        )
        patched = run(tmp_path, ["patch"], diff)
        assert patched.returncode == 1
        assert patched.stderr == f"error: path escapes cwd: ../{outside_dir.name}/secret.txt\n"
        assert outside.read_text(encoding="utf-8") == "secret"
        assert inside.read_text(encoding="utf-8") == "keep"
    finally:
        outside.unlink()
        outside_dir.rmdir()


@pytest.mark.parametrize("args", [
    [],
    ["replace"],
    ["patch", "extra"],
    ["rename", "a"],
    ["replace", "a", "b"],
])
def test_bad_argv_exits_2(tmp_path: Path, args: list[str]):
    result = run(tmp_path, args)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "error: usage: looper-edit replace <file> | looper-edit patch\n"


@pytest.mark.parametrize("stdin,stderr", [
    ("", "error: invalid json\n"),
    ("{", "error: invalid json\n"),
    ("[]", "error: invalid replace payload\n"),
    ("123", "error: invalid replace payload\n"),
    ('{"new": "x"}', "error: invalid replace payload\n"),
    ('{"old": 1, "new": "x"}', "error: invalid replace payload\n"),
    ('{"old": "a", "new": "b", "count": 0}', "error: invalid replace payload\n"),
    ('{"old": "a", "new": "b", "count": true}', "error: invalid replace payload\n"),
    ('{"old": "a", "new": "b", "count": 1.5}', "error: invalid replace payload\n"),
    ('{"old": "a", "new": "b", "times": 1}', "error: invalid replace payload\n"),
])
def test_bad_replace_json_exits_2(tmp_path: Path, stdin: str, stderr: str):
    path = tmp_path / "a.txt"
    path.write_text("a", encoding="utf-8")
    result = run(tmp_path, ["replace", "a.txt"], stdin)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == stderr
    assert path.read_text(encoding="utf-8") == "a"


def test_empty_patch_exits_1(tmp_path: Path):
    result = run(tmp_path, ["patch"], "")
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "error: empty patch\n"


def test_console_script_reports_the_same_usage(tmp_path: Path):
    script = shutil.which("looper-edit")
    assert script is not None
    scripted = subprocess.run([script], cwd=tmp_path, capture_output=True, text=True, check=False, timeout=30)
    module = run(tmp_path, [])
    assert scripted.returncode == module.returncode == 2
    assert scripted.stderr == module.stderr
