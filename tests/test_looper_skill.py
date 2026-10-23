"""``looper-skill``: the packaged SKILL.md, its Claude Code plugin copy and ``loopercli skill`` (RFC §13). No database."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from looper.agents import api_v1

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL = REPO_ROOT / "looper" / "skill" / "looper-skill" / "SKILL.md"
PLUGIN = REPO_ROOT / "plugins" / "looper"
PLUGIN_COPY = PLUGIN / "skills" / "looper-skill" / "SKILL.md"
HAND_WRITTEN = {"login", "chat", "wait", "confirm", "skill", "create", "list", "status", "start", "stop", "pipeline"}
DESCRIPTION_LIMIT = 1_024


def _generated() -> dict[str, dict[str, dict]]:
    """``{command: {action: request schema properties}}``; the action of an operation without one is ``""``."""
    found: dict[str, dict[str, dict]] = {}
    for item in api_v1.document()["paths"].values():
        for operation in item.values():
            if operation.get("x-looper-cli-hand"):
                continue
            head, _, tail = operation["operationId"].partition(".")
            schema = (((operation.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
                      ).get("schema") or {}
            found.setdefault(head.replace("_", "-"), {})[tail.replace("_", "-")] = schema.get("properties") or {}
    return found


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md starts with a frontmatter block"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _commands() -> list[list[str]]:
    """The words of each ``loopercli …`` code span of SKILL.md."""
    text = CANONICAL.read_text(encoding="utf-8")
    return [span.split() for span in re.findall(r"`(loopercli [^`]*)`", text)]


def test_the_two_copies_are_byte_for_byte_equal() -> None:
    assert PLUGIN_COPY.read_bytes() == CANONICAL.read_bytes()


def test_the_frontmatter_names_the_skill_and_describes_it() -> None:
    fields = _frontmatter(CANONICAL.read_text(encoding="utf-8"))
    assert fields["name"] == "looper-skill"
    assert 0 < len(fields["description"]) <= DESCRIPTION_LIMIT


def test_every_command_the_skill_names_exists() -> None:
    generated = _generated()
    assert "whoami" in generated
    commands = _commands()
    assert len(commands) > 10
    for words in commands:
        head = words[1]
        if head.startswith(("-", "<")):  # loopercli --help, loopercli <command> --help
            continue
        if head in HAND_WRITTEN:
            continue
        assert head in generated, f"`{' '.join(words)}`: {head} is not a loopercli command"
        actions = set(generated[head]) - {""}
        if actions:
            assert len(words) > 2 and words[2] in actions, f"`{' '.join(words)}`: expected one of {sorted(actions)}"


def test_every_option_after_a_generated_command_is_in_its_request_schema() -> None:
    generated = _generated()
    checked = 0
    for words in _commands():
        head = words[1]
        if head in HAND_WRITTEN or head not in generated:
            continue
        actions = set(generated[head]) - {""}
        properties = generated[head][words[2] if actions else ""]
        for word in words[2:]:
            if not word.startswith("--"):
                continue
            name = word[2:].removesuffix("-file").replace("-", "_")
            assert name in properties, f"`{' '.join(words)}`: {word} is not an option of that operation"
            checked += 1
    assert checked >= 8


def test_the_plugin_and_the_marketplace_parse_and_point_at_the_skill() -> None:
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    # No version in the manifest: it would go stale with every release (the package carries the version).
    assert plugin["name"] == "looper" and "version" not in plugin and plugin["license"] == "Apache-2.0"
    marketplace = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    (entry,) = marketplace["plugins"]
    assert entry["name"] == "looper" and entry["source"] == "./plugins/looper"
    assert (REPO_ROOT / entry["source"] / "skills" / "looper-skill" / "SKILL.md").is_file()
    assert (PLUGIN / "README.md").is_file()


def _loopercli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("LOOPER_", "XDG_")) and key != "HOME"}
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(REPO_ROOT), os.environ.get("PYTHONPATH"))))
    return subprocess.run([sys.executable, "-m", "looper.loopercli", *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)


def test_skill_path_prints_the_directory_of_the_packaged_skill(tmp_path: Path) -> None:
    proc = _loopercli(tmp_path, "skill", "path")
    assert proc.returncode == 0, proc.stderr
    directory = Path(proc.stdout.strip())
    assert directory.is_dir() and (directory / "SKILL.md").is_file()
    assert directory.resolve() == CANONICAL.parent.resolve()
    assert not (tmp_path / ".config").exists()


def test_skill_show_prints_the_skill(tmp_path: Path) -> None:
    proc = _loopercli(tmp_path, "skill", "show")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == CANONICAL.read_text(encoding="utf-8")


def test_skill_without_an_operation_is_a_usage_error(tmp_path: Path) -> None:
    proc = _loopercli(tmp_path, "skill")
    assert proc.returncode == 2 and "error:" in proc.stderr


def test_the_wheel_ships_the_skill() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "skill/looper-skill/SKILL.md" in data["tool"]["setuptools"]["package-data"]["looper"]
