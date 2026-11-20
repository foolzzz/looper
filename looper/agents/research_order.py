"""Where the default Manager looks things up, and in what order (Settings → Chat, ``manager.research_order``).

With the setting on, the Manager's contract gets a ``## Looking things up`` section. It lists, in order, the sources
the turn really has, and says to stop once there is enough to answer and to ask the user last. The order is
knowledge (the vault) → Looper's records and message leads → the tools installed for the Manager → the web. A browser
comes after the web once one exists. The sources come from typed availability, never from text:
- the turn's own tool listing (``ManagerOps.current_listing``): a group turn has no leads search, so it is not told
  to search leads;
- what the runtime reports it really loads of the Manager's library for this launch
  (``RuntimeAdapter.loaded_sources``: MCP servers and skills; a runtime not checked claims nothing);
- whether the runtime has web tools with this launch's configuration (``RuntimeAdapter.web_tools``, False unless
  shown: Claude Code on Anthropic's own API, Grok unless its profile turns them off, DSH when its environment allows).

Off, nothing of it reaches a prompt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .ops.leads import LEADS_TOOL

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
    installed: tuple[str, ...] = ()  # the MCP servers and skills of the Manager's library its runtime really loads
    web: bool = False  # web search and fetch

    @classmethod
    def of(cls, tools: Sequence[Mapping[str, Any]], launch: Any) -> ResearchSources:
        """The sources of a turn with this tool listing (``tools/list`` entries) and ``launch``."""
        names = {tool["name"] for tool in tools}
        loaded = launch.runtime.loaded_sources(launch)
        installed = sorted({*(name for name in loaded.mcp_servers if name != OWN_SERVER), *loaded.skills})
        return cls(vault=all(name in names for name in VAULT_TOOLS),
                   records=all(name in names for name in RECORD_TOOLS),
                   leads=LEADS_TOOL in names, installed=tuple(installed), web=launch.runtime.web_tools(launch))


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
