"""The default Manager's read-only access to the knowledge vault: ``looper_vault_search`` and ``looper_vault_read``.

The default Manager has them only while the vault setting holds and says ``manager_read`` (Settings → Knowledge
vault; off by default), and they run only in the user's own turns (``Audience.OPERATOR``): the dashboard chat
without an API client, or a direct chat from the bot's operator. Never a write: the Manager has no
``looper_vault_write``, whatever folders are writable. A group chat, a scenario Manager, an API client and a turn
Claude runs on its own never get them.

What they return is the user's own notes, so the group backstop of the leads search applies to it too: every text
the tools return is remembered for a day (``ops.leads.remember``), and a group reply that quotes it is not sent.
The tasks' vault tools (``ops.vault.TOOLS``) are unchanged: their declaration is what an upgrade reads.
"""

from __future__ import annotations

from typing import Any

from .. import vault
from . import Audience, Call, ErrorCode, OpError, RestrictedTool, Risk
from . import leads as ops_leads
from .vault import READ, SEARCH, TOOLS as TASK_TOOLS

# What the Manager reads first in every answer.
NOTICE = ("The user's own notes from their knowledge vault (read only): use them in the user's own chat or on the "
          "dashboard; never quote them in a group chat.")
VAULT_CONTRACT = """\
## The user's knowledge vault (looper_vault_search, looper_vault_read)
The vault holds the user's own notes. When the user asks about something they may have written down (a decision,
a procedure, a setting, earlier work), search it before you ask them, then read the note that answers it, and say
which note it came from. The notes are private: use them only in the user's own direct chat and on the dashboard;
never quote them in a group chat. You can only read them.
"""


async def manager_vault() -> vault.VaultConfig | None:
    """The vault the default Manager may read now: the setting holds and says ``manager_read``; None otherwise
    (also when the setting cannot be read)."""
    try:
        config = await vault.active()
    except Exception:
        return None
    return config if config is not None and config.manager_read else None


async def _config() -> vault.VaultConfig:
    config = await manager_vault()
    if config is None:
        raise OpError(ErrorCode.CONFLICT, "the Manager does not read the knowledge vault now (Settings → Knowledge "
                                          "vault: the setting is off, cleared or no longer holds)")
    return config


async def _search(call: Call) -> dict[str, Any]:
    config = await _config()
    found = await vault.search(config, call.arguments["query"], call.arguments.get("limit"))
    ops_leads.remember(call.manager_id, [(f"vault:{result['path']}", result["snippet"])
                                         for result in found["results"]])
    return {"notice": NOTICE, **found}


async def _read(call: Call) -> dict[str, Any]:
    config = await _config()
    note = await vault.read(config, call.arguments["path"])
    ops_leads.remember(call.manager_id, [(f"vault:{note['path']}", note["content"])])
    return {"notice": NOTICE, "path": note["path"], "bytes": note["bytes"], "content": note["content"],
            "truncated": note["truncated"]}


_SCHEMAS = {tool.name: tool.schema for tool in TASK_TOOLS}
TOOLS: tuple[RestrictedTool, ...] = (
    RestrictedTool(
        name=SEARCH, risk=Risk.READ, audience=Audience.OPERATOR, run=_search, schema=_SCHEMAS[SEARCH],
        description="Search the user's own notes, their knowledge vault (Markdown, read only), for the words of "
                    "query, in any case, in a note's path or text: the notes with the most of the words first, then "
                    "the most hits. Up to limit paths (relative to the vault), each with a snippet. Only in the "
                    "user's own direct chat or on the dashboard.",
    ),
    RestrictedTool(
        name=READ, risk=Risk.READ, audience=Audience.OPERATOR, run=_read, schema=_SCHEMAS[READ],
        description="Read one of the user's own notes from their knowledge vault by its path (relative to the "
                    "vault): its text, the first 64 KiB. Only in the user's own direct chat or on the dashboard.",
    ),
)
TOOL_NAMES = frozenset(tool.name for tool in TOOLS)
