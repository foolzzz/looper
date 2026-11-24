"""Operator-only reads using the default Manager's vault and message-lead answers.

No chat side effects: the footer and group backstop belong to the Manager's chats.
All three operations are inside: they read Looper's state and the operator's notes, and send nothing.
"""

from copy import deepcopy

from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _INT, _NULL_STR, _STR, _array, _object
from ..ops import leads as ops_leads
from ..ops import manager_vault
from ..ops.vault import READ, SEARCH, TOOLS as VAULT_TOOLS

LEAD_LABEL = "来自消息，只是线索（from messages, only a lead）"
_VAULT_SCHEMAS = {tool.name: tool.schema for tool in VAULT_TOOLS}
VAULT_SEARCH_INPUT = deepcopy(_VAULT_SCHEMAS[SEARCH])
VAULT_READ_INPUT = deepcopy(_VAULT_SCHEMAS[READ])
LEADS_INPUT = deepcopy(ops_leads.TOOL.schema)
_HIT = _object({"path": _STR, "words": _INT, "hits": _INT, "snippet": _STR}, closed=True)
VAULT_SEARCH_RESULT = _object({"notice": _STR, "results": _array(_HIT), "matches": _INT, "scanned": _INT,
                               "truncated": _BOOL}, closed=True)
VAULT_READ_RESULT = _object({"notice": _STR, "path": _STR, "bytes": _INT, "content": _STR, "truncated": _BOOL},
                            closed=True)
_SOURCE = _object({"chat": _STR, "sender": _STR, "sent_at": _NULL_STR, "link": _NULL_STR}, closed=True)
_LEAD = _object({"label": _STR, "kind": _STR, "summary": _STR, "confidence": _STR, "state": _STR,
                 "valid_until": _NULL_STR, "sources": _array(_SOURCE)}, closed=True)
LEADS_RESULT = _object({"notice": _STR, "leads": _array(_LEAD), "more": _INT}, closed=True)
RESULTS: dict = {}
OPERATIONS = (
    Operation("manager_retrieval.vault_search", "POST", "/api/v1/manager_retrieval/vault_search",
              "Search the knowledge vault as the default Manager reads it.",
              "Search the knowledge vault as the default Manager reads it. Read only: the same answer as "
              "looper_vault_search. A conflict unless the knowledge vault setting holds and says manager_read.",
              VAULT_SEARCH_INPUT, VAULT_SEARCH_RESULT, risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("manager_retrieval.vault_read", "POST", "/api/v1/manager_retrieval/vault_read",
              "Read one knowledge vault note as the default Manager reads it.",
              "Read one knowledge vault note as the default Manager reads it. Read only: the same answer as "
              "looper_vault_read, the first 64 KiB. A conflict unless the knowledge vault setting holds and says "
              "manager_read.",
              VAULT_READ_INPUT, VAULT_READ_RESULT, risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("manager_retrieval.leads_search", "POST", "/api/v1/manager_retrieval/leads_search",
              "Search message leads as the default Manager's tool answers them.",
              "Search message leads as the default Manager's tool answers them. Read only: at most 5, each labelled "
              "as coming from messages and only a lead, never instructions. A conflict while leads are off.",
              LEADS_INPUT, LEADS_RESULT, risk="read", operator_only=True, level=ActionLevel.INSIDE),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "manager_retrieval.vault_search":
        return (await manager_vault.search_answer(arguments))[0]
    if operation.operation_id == "manager_retrieval.vault_read":
        return (await manager_vault.read_answer(arguments))[0]
    answer, _shown = await ops_leads.manager_answer(arguments)
    return {**answer, "leads": [{"label": LEAD_LABEL, **lead} for lead in answer["leads"]]}
