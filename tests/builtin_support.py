"""The built-in Agent Profiles and LLM providers tests expect, read from where Looper declares them.

A built-in added later joins these by itself, so a test about something else needs no edit. The built-ins that exist
now are frozen once, here (``FROZEN_PROFILES``, ``FROZEN_PROVIDERS``), and the reseed test checks them against the
declarations and the database: renaming or dropping one still fails, in that one place.
"""

from __future__ import annotations

from typing import Any

from looper.agents.profile_registry import BUILTIN_PROFILE, BUILTIN_ROLE_PROFILES

# Every built-in Agent Profile by name; several roles may share one.
BUILTIN_PROFILES = {profile.name: profile for profile in (BUILTIN_PROFILE, *BUILTIN_ROLE_PROFILES.values())}
# In the order the registry lists profiles of one kind: by name.
BUILTIN_PROFILE_NAMES = sorted(BUILTIN_PROFILES)
BUILTIN_PROFILE_IDS = {profile.id for profile in BUILTIN_PROFILES.values()}

# The built-ins as of this writing: id -> name.
FROZEN_PROFILES = {"agent-claude-code-opus": "claude-code/opus", "agent-claude-code-fable-5-2": "claude-code/fable-5-2",
                   "agent-claude-code-sonnet-5-5": "claude-code/sonnet-5-5",
                   "agent-claude-code-haiku-5-5": "claude-code/haiku-5-5"}
FROZEN_PROVIDERS = {"prov-claude-subscription": "claude-subscription", "prov-coco-login": "coco login",
                    "prov-codex-login": "codex-login", "prov-cursor-login": "cursor-login",
                    "prov-grok-login": "grok-login"}


async def builtin_providers(pool: Any) -> list[tuple[str, str]]:
    """The schema's built-in providers as (id, name), in the order the registry lists them: by name."""
    return [(row["id"], row["name"]) for row in await pool.fetch(
        "SELECT id, name FROM llm_providers WHERE builtin ORDER BY name, id")]
