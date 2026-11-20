"""Where the default Manager looks things up, and in what order (Settings → Chat, ``manager.research_order``).

With the setting on, the Manager's contract gets a ``## Looking things up`` section. It lists, in order, the sources
the turn really has, and says to stop once there is enough to answer and to ask the user last. The order is
knowledge (the vault) → Looper's records and message leads → the tools installed for the Manager → the web. A browser
comes after the web once one exists. The sources come from typed availability, never from text:
- the turn's own tool listing (``ManagerOps.current_listing``): a group turn has no leads search, so it is not told
  to search leads;
- the Manager's library, as far as its runtime loads it (MCP servers, skills);
- the runtime's web tools (``RuntimeAdapter.web_tools``) and the profile's ``web_search``.

Off, nothing of it reaches a prompt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .library import ItemKind
from .ops.leads import LEADS_TOOL
from .runtimes import Capability

CONTRACT_NAME = "RESEARCH_ORDER"
VAULT_TOOLS = ("looper_vault_search", "looper_vault_read")
RECORD_TOOLS = ("looper_tasks", "looper_projects")
OWN_SERVER = "looper"  # Looper's own MCP server: its tools are the ones above, not "installed tools"
NAMES_SHOWN = 12


@dataclass(frozen=True)
class ResearchSources:
    """What one Manager turn can look things up with."""

    vault: bool = False  # the user's knowledge vault (``looper_vault_search`` and ``looper_vault_read``)
    records: bool = False  # Looper's tasks and projects (``looper_tasks``, ``looper_projects``)
    leads: bool = False  # message leads (``looper_leads_search``: only in the user's own turns)
    installed: tuple[str, ...] = ()  # the MCP servers and skills installed for the Manager that its runtime loads
    web: bool = False  # web search and fetch

    @classmethod
    def of(cls, tools: Sequence[Mapping[str, Any]], launch: Any) -> ResearchSources:
        """The sources of a turn with this tool listing (``tools/list`` entries) and ``launch``."""
        names = {tool["name"] for tool in tools}
        runtime = launch.runtime
        loads = {ItemKind.MCP} if Capability.MCP in runtime.capabilities else set()
        if Capability.CLAUDE_PLUGINS in runtime.capabilities:
            loads |= {ItemKind.SKILL, ItemKind.PLUGIN}
        installed = sorted({item.name for item in launch.library.items
                            if item.kind in loads and not (item.kind is ItemKind.MCP and item.name == OWN_SERVER)})
        return cls(vault=all(name in names for name in VAULT_TOOLS),
                   records=all(name in names for name in RECORD_TOOLS),
                   leads=LEADS_TOOL in names, installed=tuple(installed),
                   web=runtime.web_tools and bool(getattr(launch.profile, "web_search", True)))


def section(sources: ResearchSources) -> str:
    """The contract's section for ``sources``: the order to look in, when to stop, and asking the user last."""
    steps: list[str] = []
    if sources.vault:
        steps.append("The user's knowledge vault: their notes and what earlier work kept (looper_vault_search, "
                     "then looper_vault_read).")
    if sources.records:
        steps.append("Looper's own records: tasks, their requirements and results, and projects with their "
                     "background (looper_tasks, looper_projects).")
    if sources.leads:
        steps.append("Message leads from the user's chats (looper_leads_search): unconfirmed background, as its "
                     "section says.")
    if sources.installed:
        shown = ", ".join(sources.installed[:NAMES_SHOWN])
        more = len(sources.installed) - NAMES_SHOWN
        steps.append(f"The tools installed for you: {shown}{f' and {more} more' if more > 0 else ''}.")
    if sources.web:
        steps.append("The web: search, then read the pages that answer it.")
    lines = ["## Looking things up",
             "When an answer needs facts you do not have, look for them in this order, and stop as soon as you "
             "have enough to answer. Say where each fact came from.",
             *(f"{number}. {step}" for number, step in enumerate(steps, 1)),
             "Ask the user only for what none of these has, or for a decision that is theirs to make."]
    return "\n".join(lines) + "\n"
