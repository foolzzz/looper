"""A scripted ``claude`` stand-in's answers to the task Manager's readback requests.

``SOURCE`` is run inside the stand-in (``exec``); it defines ``readback_answer(request, override_path)``. The answer is
the clean one for the stand-in's own launch command: the effective settings are the ``--settings`` file's, the MCP
servers the ``--mcp-config`` file's, the tool rules the verified version's. A test makes the CLI answer otherwise by
writing JSON to ``override_path`` (read at every request, so it can change between turns):
``{"<subtype>": {...}}`` is deep-merged into that answer, ``{"<subtype>": "error"}`` answers an error and
``{"<subtype>": "hang"}`` does not answer; ``{"init": {...}}`` replaces fields of the turn's ``system/init``.
"""
from __future__ import annotations

SUBTYPES = ("initialize", "get_settings", "get_sandbox_dialog", "get_hooks_listing", "mcp_status",
            "list_permission_rules", "get_context_usage")

SOURCE = r'''
import json, os, sys
from pathlib import Path

READBACK_SUBTYPES = ("initialize", "get_settings", "get_sandbox_dialog", "get_hooks_listing", "mcp_status",
                     "list_permission_rules", "get_context_usage")


def _flag(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def _merge(base, patch):
    if isinstance(base, dict) and isinstance(patch, dict):
        return {**base, **{key: _merge(base.get(key), value) for key, value in patch.items()}}
    return patch


def init_fields(override_path):
    """``tools`` and ``permissionMode`` of the stand-in's ``system/init``: the launch's own; ``{"init": {...}}`` in the
    override file replaces fields."""
    mcp_file = _flag("--mcp-config")
    servers = json.loads(Path(mcp_file).read_text()).get("mcpServers", {}) if mcp_file else {}
    fields = {"tools": ["Bash", *(f"mcp__{name}__task_status" for name in servers)], "permissionMode": "bypassPermissions"}
    override = json.loads(Path(override_path).read_text()) if Path(override_path).exists() else {}
    return {**fields, **override.get("init", {})}


def readback_answer(request, override_path):
    """The ``control_response`` body for one readback request: ``("success", body)``, ``("error", text)`` or None."""
    from looper.agents import task_manager_readback as rb, task_manager_sandbox as sbx

    subtype = request["subtype"]
    settings = json.loads(Path(_flag("--settings")).read_text())
    mcp_file = _flag("--mcp-config")
    servers = json.loads(Path(mcp_file).read_text()).get("mcpServers", {}) if mcp_file else {}
    writes = sorted(rb.writes_allowed(os.environ["HOME"], os.getuid(), os.environ.get("CLAUDE_CODE_TMPDIR")))
    allowed = set(sbx.BUILTIN_TOOLS) | set(sbx.UNREMOVABLE_TOOLS)
    bodies = {
        "initialize": {"current_permission_mode": "bypassPermissions", "sandbox_enabled": True},
        "get_settings": {"effective": settings, "sources": [{"source": "flagSettings", "settings": settings}]},
        "get_sandbox_dialog": {"supported": True, "enabled": True, "dependencies": {"errors": [], "warnings": []},
                               "restrictions": {"fs_allow_write": []},
                               "lists": {"rows": [{"id": "writes_allowed", "entries": writes, "total": len(writes)}]}},
        "get_hooks_listing": {"hooks": []},
        "mcp_status": {"mcpServers": [
            {"name": name, "status": "connected", "tools": [{"name": "task_status"}],
             "config": {key: value for key, value in definition.items() if key in ("type", "command", "args", "url")}}
            for name, definition in servers.items()]},
        "list_permission_rules": {"state": {"rules": [
            {"behavior": "deny", "source": "toolsNarrowing", "rule": name}
            for name in sorted(rb.KNOWN_BUILTIN_TOOLS["2.1.332"] - allowed)]}},
        "get_context_usage": {"agents": [], "mcpTools": [], "systemTools": None},
    }
    override = json.loads(Path(override_path).read_text()) if Path(override_path).exists() else {}
    patch = override.get(subtype)
    if patch == "hang":
        return None
    if patch == "error":
        return ("error", "synthetic error")
    return ("success", _merge(bodies[subtype], patch) if patch else bodies[subtype])
'''
