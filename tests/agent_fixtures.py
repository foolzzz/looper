"""Agents as the database gives them to the agent settings (``db.agent_registry.AGENT_JSON``).

``agent_json(model)`` is an agent of the built-in Claude Code runtime and its
own login with ``model``, named ``claude-code/<model>`` the way the upgrade
names the agent of a model a layer held: what a test stores in a layer row's
``agent`` where it used to store a ``model``.
"""

from __future__ import annotations

from typing import Any


def agent_json(model: str, *, effort: str | None = None) -> dict[str, Any]:
    return {
        "id": f"agent-{model}", "name": f"claude-code/{model}", "model": model, "effort": effort, "builtin": False,
        "runtime_id": "rt-claude-code", "runtime_name": "claude-code", "runtime_type": "claude-code",
        "runtime_executable": "",
        "runtime_capabilities": ["add_dirs", "autonomous_turns", "claude_plugins", "compaction_events", "cost_report",
                                 "mcp", "resident_session", "resume", "run"],
        "provider_id": "prov-claude-subscription", "provider_name": "claude-subscription", "provider_auth": "oauth",
        "provider_protocols": ["anthropic-messages"], "provider_oauth_runtime_type": "claude-code",
        "provider_base_url": "", "provider_models": [],
    }
