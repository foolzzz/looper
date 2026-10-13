"""A synthetic operator home for the library import tests.

``fake_claude_home(root)`` lays out ``root/.claude`` the way Claude Code
keeps an operator's configuration: ``settings.json`` with ``enabledPlugins``
and a personal hook, the installed-plugin registry, one enabled plugin
installed for the user (and a project-scope copy of it that must not be
used), a disabled plugin, an enabled plugin that is not installed, and
personal skills (one a symbolic link, as ``~/.claude/skills`` often holds).
Every file is made up here; nothing is copied from a real configuration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def write(path: Path, text: str | bytes, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    return path


PLUGIN_FILES = {
    ".claude-plugin/plugin.json": json.dumps({"name": "demo", "version": "1.2.0"}),
    "skills/demo-skill/SKILL.md": "---\nname: demo-skill\ndescription: A demo skill.\n---\nSay DEMO.\n",
    "agents/helper.md": "---\nname: helper\ndescription: Helps.\n---\nHelp.\n",
    "hooks/hooks.json": json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/start.sh"'}]}]}}),
    "README.md": "# demo\n",
}


def fake_claude_home(root: Path) -> Path:
    """Build ``root/.claude`` and return it."""
    config = root / ".claude"
    plugin = config / "plugins" / "cache" / "market" / "demo" / "1.2.0"
    project_copy = config / "plugins" / "cache" / "market" / "demo" / "0.9.0"
    disabled = config / "plugins" / "cache" / "market" / "off" / "1.0.0"
    write(config / "settings.json", json.dumps({
        "enabledPlugins": {"demo@market": True, "off@market": False, "ghost@market": True},
        "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "cat >> prompt-log.jsonl"}]}]},
        "model": "opus",
    }))
    write(config / "plugins" / "installed_plugins.json", json.dumps({"version": 2, "plugins": {
        "demo@market": [
            {"scope": "project", "projectPath": str(root / "elsewhere"), "installPath": str(project_copy),
             "version": "0.9.0"},
            {"scope": "user", "installPath": str(plugin), "version": "1.2.0"},
        ],
        "off@market": [{"scope": "user", "installPath": str(disabled), "version": "1.0.0"}],
    }}))
    for relative, text in PLUGIN_FILES.items():
        write(plugin / relative, text)
    write(plugin / "hooks" / "start.sh", "#!/bin/sh\necho started\n", executable=True)
    write(plugin / "assets" / "logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    write(plugin / ".git" / "HEAD", "ref: refs/heads/main\n")
    write(plugin / ".in_use" / "4242", "{}")
    os.symlink("README.md", plugin / "AGENTS.md")  # a link inside the plugin is read as its target
    write(project_copy / ".claude-plugin" / "plugin.json", json.dumps({"name": "demo", "version": "0.9.0"}))
    write(disabled / ".claude-plugin" / "plugin.json", json.dumps({"name": "off"}))

    skills = config / "skills"
    write(skills / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: Alpha.\n---\nReply ALPHA-OK.\n")
    write(skills / "alpha" / "scripts" / "run.py", "print('alpha')\n", executable=True)
    linked = root / "agents-skills" / "beta"
    write(linked / "SKILL.md", "---\nname: beta\ndescription: Beta.\n---\nReply BETA-OK.\n")
    os.symlink(linked, skills / "beta")
    write(skills / "notes" / "todo.md", "not a skill\n")  # a directory without SKILL.md
    write(skills / ".trash" / "old" / "SKILL.md", "deleted\n")  # hidden: ignored
    return config


# Library items as the agents' tests enable them (``enabled_library`` in conftest).
DEMO_PLUGIN = ("plugin", "demo@market", {".claude-plugin/plugin.json": '{"name": "demo"}', "agents/helper.md": "Help.\n"})
ALPHA_SKILL = ("skill", "alpha", {"SKILL.md": "---\nname: alpha\ndescription: Alpha.\n---\nReply ALPHA-OK.\n"})


def plugin_dirs(argv: list[str]) -> list[str]:
    """The ``--plugin-dir`` values of a command, in order."""
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--plugin-dir"]
