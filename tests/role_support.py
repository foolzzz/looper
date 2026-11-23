"""The role lists runtime tests expect, read from the role registry (``runtimes.ROLE_CAPABILITIES``) in its order.

Each list is a kind of runtime, not a role count: a role added to the registry joins the lists whose runtimes can run
it, and a test about something else needs no edit.
"""

from __future__ import annotations

from looper.agents.runtimes import ROLE_CAPABILITIES, Capability

# A runtime with every capability (Claude Code) runs every role.
EVERY_ROLE = list(ROLE_CAPABILITIES)
# A runtime that keeps sessions but cannot run without tools (Codex, Pi, Cursor) runs the roles that have tools.
ROLES_WITH_TOOLS = [role for role, needs in ROLE_CAPABILITIES.items() if Capability.NO_TOOLS not in needs]
# A runtime that only runs a prompt once (a stand-in, an older probe) runs the roles that need nothing more.
RUN_ONLY_ROLES = [role for role, needs in ROLE_CAPABILITIES.items() if needs == {Capability.RUN}]
