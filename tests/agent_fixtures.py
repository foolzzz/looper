"""Agent Profiles as the database gives them to the agent settings (``db.profile_registry.PROFILE_JSON``).

``profile_json(model)`` is a profile of the built-in Claude Code runtime and its
own login with ``model``, named ``claude-code/<model>`` the way the upgrade
names the profile of a model a layer held: what a test stores in a layer row's
``profile`` where it used to store a ``model``. The model is a row of its own
(``model-<model>``) on the login account, as the registry stores it.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from looper.agents.profile_registry import Endpoint, ModelSpec, builtin_resolved_profile
from looper.agents.runtimes import ApiProtocol


def profile_json(model: str, *, effort: str | None = None) -> dict[str, Any]:
    builtin = builtin_resolved_profile().resolved_model
    account = builtin.provider
    saved = ModelSpec(f"model-{model}", account.id, model, ApiProtocol.ANTHROPIC_MESSAGES)
    provider = asdict(account)
    provider["vendor_type"] = account.vendor_type.value
    provider["auth"] = account.auth.value
    provider["env"] = []
    endpoint = asdict(Endpoint(account.id, saved.protocol, ""))
    endpoint["protocol"] = saved.protocol.value
    row = asdict(saved)
    row["protocol"] = saved.protocol.value
    return {
        "id": f"agent-{model}", "name": f"claude-code/{model}", "model_id": saved.id, "model": row,
        "provider": provider, "endpoint": endpoint, "effort": effort, "builtin": False, "agent_teams": False,
        "custom_prompt": "",
        "runtime_id": "rt-claude-code", "runtime_name": "claude-code", "runtime_type": "claude-code",
        "runtime_executable": "",
        "runtime_capabilities": ["add_dirs", "autonomous_turns", "claude_plugins", "compaction_events", "cost_report",
                                 "mcp", "resident_session", "resume", "run"],
    }
