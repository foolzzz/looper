"""Project-owned skills; installation switches belong to the catalog domain."""

from .. import catalog, project_skills, skill_lifecycle, skill_proposals, skill_usage, skill_versions
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _INT, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain
from .catalog import PROJECT_INSTALLS, _answer as catalog_answer, _enum

ORDER = 6


PROJECT_SKILL = _object({
    "skill_id": _STR, "name": _STR, "qualified_name": _STR, "description": _STR,
    "created_at": _STR, "updated_at": _STR, "size_bytes": _INT,
    "lifecycle": _enum(*(member.value for member in skill_lifecycle.Lifecycle))})
_FIELDS = dict.fromkeys(("name", "description", "content"), _STR)
_IDS = {"project_id": _STR, "skill_id": _STR}
_CHANGE = _object({**PROJECT_INSTALLS["properties"], "skill": PROJECT_SKILL})


def _op(action, request, result, risk):
    name = f"project_skills.{action}"
    description = f"{action.capitalize()} a project-owned skill."
    if action == "delete":
        description += " Removes its installations in every project; client tokens require session confirmation."
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high", level=ActionLevel.INSIDE)


_VERSIONS_SUMMARY = "List one project skill's versions, and the difference between two of them."
_ROLLBACK_SUMMARY = "Write a new version of a project skill from an older version's content."
_DEPRECATE_SUMMARY = "Mark a project skill deprecated so new runs skip it."
_RESTORE_SUMMARY = "Mark a deprecated project skill active so new runs load it again."
_BUDGET_SUMMARY = "Count the skill name and description characters a run of each role would list."
_AUTHOR = {"type": "string", "enum": ["operator", "proposal", "rollback"]}
_VERSION = _object({
    "version": _INT, "name": _STR, "description": _STR, "sha256": _STR, "author_kind": _AUTHOR,
    "author_ref": {"type": ["string", "null"]}, "reason": _STR, "created_at": _STR,
})
_AFFECTED = _object({"project_id": _STR, "project_name": _STR})
_DIFF = _object({
    "from_version": _INT, "to_version": _INT,
    "name": _object({"from": _STR, "to": _STR}),
    "description": _object({"from": _STR, "to": _STR}),
    "content": _STR,
})
_VERSIONS = _object({
    "skill_id": _STR, "current_version": {"type": ["integer", "null"]}, "versions": _array(_VERSION),
    "affected_projects": _array(_AFFECTED), "diff": {**_DIFF, "type": ["object", "null"]},
})
_ROLLBACK = _object({**_CHANGE["properties"], "version": _INT, "affected_projects": _array(_AFFECTED)})
_LIFECYCLE = _object({**_CHANGE["properties"], "affected_projects": _array(_AFFECTED)})
_BUDGET_ROLE = _object({"chars": _INT, "skills": _INT, "over": _BOOL})
_BUDGET = _object({
    "limit": _INT,
    "roles": _object({"planner": _BUDGET_ROLE, "worker": _BUDGET_ROLE, "reviewer": _BUDGET_ROLE}),
    "counts_plugin_skills": _BOOL,
})


_USAGE_SUMMARY = "Count how often each project skill was loaded and called, and how its attempts fared."
_NUM = {"type": ["number", "null"]}
_INVOKED = _object({"count": _INT, "n": _INT, "unknown": _INT, "rate": _NUM})
_RATE = _object({"state": _enum("ok", "insufficient"), "value": _NUM, "low": _NUM, "high": _NUM, "n": _INT,
                 "successes": _INT})
_AVERAGE = _object({"avg": _NUM, "n": _INT})
_EFFECT = {**_object({"first_pass": _RATE, "control": _RATE,
                      "cost": _object({"loaded": _AVERAGE, "control": _AVERAGE})}), "type": ["object", "null"]}
_USAGE_PERIOD = {
    "loaded_runs": _INT, "loaded_attempts": {"type": ["integer", "null"]}, "invoked": _INVOKED, "effect": _EFFECT,
}
_SPAN = _object({"start": _STR, "end": _STR})
_USAGE_ROW = _object({
    "skill_id": _STR, "name": _STR, "sha256": _STR, "lifecycle": _STR, "versions": _array(_INT),
    **_USAGE_PERIOD, "comparison": _object(_USAGE_PERIOD),
    "review": _array(_enum(*(reason.value for reason in skill_usage.ReviewReason))),
    "window": _SPAN, "comparison_window": _SPAN, "coverage_since": {"type": ["string", "null"]}, "note": _STR,
})
_USAGE = _object({"skills": _array(_USAGE_ROW)})
_PROPOSALS = "project_skills.proposals"
_HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_NULL_STR = {"type": ["string", "null"]}
_EVIDENCE = _object({"task": _STR, "step": _STR}, ["task"], closed=True)
_PROPOSAL = _object({
    "proposal_id": _INT, "project_id": _STR, "action": {"type": "string", "enum": ["create", "update"]},
    "skill_id": _NULL_STR, "name": _STR, "description": _STR, "why": _STR, "evidence": _array(_EVIDENCE),
    "proposer": _STR, "status": {"type": "string", "enum": [s.value for s in skill_proposals.Status]},
    "content_sha256": _STR, "base_sha256": _NULL_STR, "content": _STR, "created_at": _STR, "expires_at": _STR,
    "decided_at": _NULL_STR, "decision_note": _NULL_STR,
    "current": {**_object({"name": _STR, "description": _STR, "sha256": _STR}), "type": ["object", "null"]},
    "diff": {**_object({"name": _object({"from": _STR, "to": _STR}), "description": _object({"from": _STR, "to": _STR}),
                        "content": _STR}), "type": ["object", "null"]},
    "stale": _BOOL, "affected_projects": _array(_AFFECTED),
})
_PROPOSAL_ID = {"project_id": _STR, "proposal_id": _INT}
_PROPOSAL_ONE = _object({"proposal": _PROPOSAL})
_LIST_SUMMARY = "List a project's skill proposals, newest first, with the difference from the current skill."
_GET_SUMMARY = "Read one skill proposal with its events."
_CONFIRM_SUMMARY = "Confirm a skill proposal by the content hash the operator saw, optionally editing it first."
_REJECT_SUMMARY = "Reject a pending skill proposal, with an optional reason."
_PROPOSAL_RESTORE_SUMMARY = "Make an expired skill proposal pending again after checking it once more."
_MODE_SUMMARY = "Allow or stop agents proposing skills for a project."
_CONFIRMED = _object({"proposal": _PROPOSAL, "skill": PROJECT_SKILL, "version": {"type": ["integer", "null"]},
                      "affected_projects": _array(_AFFECTED)})
_EVENT = _object({"action": _STR, "actor_kind": _STR, "via": _STR, "content_sha256": _NULL_STR, "note": _NULL_STR,
                  "at": _STR})


OPERATIONS = (
    _op("add", _object({"project_id": _STR, "skill": _object({**_FIELDS, "enabled": _BOOL},
                                                          list(_FIELDS), closed=True)}, closed=True),
        _CHANGE, "write"),
    _op("get", _object(_IDS, closed=True), _object({"skill": PROJECT_SKILL, "content": _STR}), "read"),
    _op("update", _object({**_IDS, "fields": {**_object(_FIELDS, [], closed=True), "minProperties": 1}},
                           closed=True), _CHANGE, "write"),
    _op("delete", _object(_IDS, closed=True),
        _object({**PROJECT_INSTALLS["properties"], "deleted": _STR}), "high"),
    Operation(
        "project_skills.rollback", "POST", "/api/v1/project_skills/rollback", _ROLLBACK_SUMMARY,
        _ROLLBACK_SUMMARY + " The new version keeps that content and hash. Returns the projects that install the skill.",
        _object({**_IDS, "version": _INT}, closed=True), _ROLLBACK, risk="write", level=ActionLevel.INSIDE,
        operator_only=True,
    ),
    Operation(
        "project_skills.versions", "POST", "/api/v1/project_skills/versions", _VERSIONS_SUMMARY,
        _VERSIONS_SUMMARY + " Each version records the content hash, author kind, reason and time.",
        _object({**_IDS, "from_version": _INT, "to_version": _INT}, ["project_id", "skill_id"], closed=True),
        _VERSIONS, risk="read", level=ActionLevel.INSIDE, operator_only=True,
    ),
    Operation(
        "project_skills.usage", "POST", "/api/v1/project_skills/usage", _USAGE_SUMMARY,
        _USAGE_SUMMARY + " Per skill and current content hash: runs and attempts that loaded it, the share whose "
        "stream called it (unknown for runtimes without that event), first-pass rate and cost against attempts "
        "without it, with 95% intervals; fewer than 20 attempts on a side shows counts only. Observational data, "
        "not a controlled experiment.",
        _object({"project_id": _STR}, closed=True), _USAGE, risk="read", level=ActionLevel.INSIDE,
    ),
    Operation(
        "project_skills.deprecate", "POST", "/api/v1/project_skills/deprecate", _DEPRECATE_SUMMARY,
        _DEPRECATE_SUMMARY + " Installs, versions and files stay. An optional reason is not stored on the skill; it is kept only in the operator action record.",
        _object({**_IDS, "reason": _STR}, ["project_id", "skill_id"], closed=True),
        _LIFECYCLE, risk="write", level=ActionLevel.INSIDE, operator_only=True,
    ),
    Operation(
        "project_skills.restore", "POST", "/api/v1/project_skills/restore", _RESTORE_SUMMARY,
        _RESTORE_SUMMARY + " Installs, versions and files stay.",
        _object(_IDS, closed=True), _LIFECYCLE, risk="write", level=ActionLevel.INSIDE, operator_only=True,
    ),
    Operation(
        "project_skills.context_budget", "POST", "/api/v1/project_skills/context_budget", _BUDGET_SUMMARY,
        _BUDGET_SUMMARY + " The 8000-character line is a display threshold. Plugin-bundled skills are not counted.",
        _object({"project_id": _STR}, closed=True), _BUDGET, risk="read", level=ActionLevel.INSIDE,
        operator_only=True,
    ),
    # Proposals. An API client may read them; confirming, rejecting, restoring and the switch are the operator's.
    Operation(
        f"{_PROPOSALS}.list", "POST", "/api/v1/project_skills/proposals/list", _LIST_SUMMARY,
        _LIST_SUMMARY + " Pending and expired ones unless a status is given. mode is the project's switch.",
        _object({"project_id": _STR, "status": {"type": "string", "enum": [s.value for s in skill_proposals.Status]},
                 "limit": {"type": "integer", "minimum": 1, "maximum": skill_proposals.LIST_MAX}},
                ["project_id"], closed=True),
        _object({"mode": {"type": "string", "enum": ["off", "on"]}, "proposals": _array(_PROPOSAL)}),
        risk="read", level=ActionLevel.INSIDE,
    ),
    Operation(
        f"{_PROPOSALS}.get", "POST", "/api/v1/project_skills/proposals/get", _GET_SUMMARY,
        _GET_SUMMARY + " Events record who did what, through which entry, and the content hash.",
        _object(_PROPOSAL_ID, closed=True), _object({"proposal": _PROPOSAL, "events": _array(_EVENT)}),
        risk="read", level=ActionLevel.INSIDE,
    ),
    Operation(
        f"{_PROPOSALS}.confirm", "POST", "/api/v1/project_skills/proposals/confirm", _CONFIRM_SUMMARY,
        _CONFIRM_SUMMARY + " Dashboard only. A hash that is not the proposal's current one is a 409. An update "
        "whose skill changed since the draft becomes superseded and writes nothing (409). Fields given make the "
        "operator's own edit, written as an operator version.",
        _object({**_PROPOSAL_ID, "content_sha256": _HASH,
                 "fields": {**_object(_FIELDS, [], closed=True), "minProperties": 1}},
                ["project_id", "proposal_id", "content_sha256"], closed=True),
        _CONFIRMED, risk="write", level=ActionLevel.INSIDE, operator_only=True, cli=False, audit_operator=False,
    ),
    Operation(
        f"{_PROPOSALS}.reject", "POST", "/api/v1/project_skills/proposals/reject", _REJECT_SUMMARY,
        _REJECT_SUMMARY + " The reason has at most 500 characters.",
        _object({**_PROPOSAL_ID, "reason": _STR}, ["project_id", "proposal_id"], closed=True),
        _PROPOSAL_ONE, risk="write", level=ActionLevel.INSIDE, operator_only=True,
    ),
    Operation(
        f"{_PROPOSALS}.restore", "POST", "/api/v1/project_skills/proposals/restore", _PROPOSAL_RESTORE_SUMMARY,
        _PROPOSAL_RESTORE_SUMMARY + " It must still pass every rule, and an update's skill must be unchanged.",
        _object(_PROPOSAL_ID, closed=True), _PROPOSAL_ONE, risk="write", level=ActionLevel.INSIDE,
        operator_only=True,
    ),
    Operation(
        f"{_PROPOSALS}.set_mode", "POST", "/api/v1/project_skills/proposals/set_mode", _MODE_SUMMARY,
        _MODE_SUMMARY + " On lets agents draft skills for the operator to confirm; a draft never takes effect "
        "before the operator confirms it. Off stores nothing (NULL).",
        _object({"project_id": _STR, "mode": {"type": "string", "enum": ["off", "on"]}}, closed=True),
        _object({"project_id": _STR, "mode": {"type": "string", "enum": ["off", "on"]}}),
        risk="write", level=ActionLevel.INSIDE, operator_only=True,
    ),
)
RESULTS = {}


async def _answer(request):
    try:
        return _plain(await request)
    except skill_proposals.ProposalForbidden as exc:
        raise OpError(ErrorCode.FORBIDDEN, str(exc)) from None
    except (project_skills.ProjectSkillError, project_skills.ProjectSkillNotFound,
            project_skills.ProjectSkillConflict) as exc:
        code = (ErrorCode.NOT_FOUND if isinstance(exc, project_skills.ProjectSkillNotFound) else
                ErrorCode.CONFLICT if isinstance(exc, project_skills.ProjectSkillConflict) else ErrorCode.INVALID)
        raise OpError(code, str(exc)) from None


async def _references(arguments):
    # Ownership is checked before reading the catalog's full installation projection.
    await _answer(project_skills.get_skill(arguments["project_id"], arguments["skill_id"]))
    return (await catalog_answer(catalog.item_detail(arguments["skill_id"])))["item"]


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "project_skills.delete":
        return None
    item = await _references(arguments)
    places = sorted({f"{install['project_name']} ({install['project_id']})" for install in item["installs"]})
    return (f"Delete project skill {item['qualified_name']!r} and all {len(item['installs'])} installations "
            f"across projects: {', '.join(places) or 'none'}? "
            "Its stored content and installations will be removed; other catalog items stay unchanged.")


async def run(server, operation, arguments, principal, headers):
    name = operation.operation_id
    project_id = arguments["project_id"]
    if name.startswith(_PROPOSALS + "."):
        return await _answer(_proposals(name.rsplit(".", 1)[1], project_id, arguments, principal))
    if name == "project_skills.add":
        return await _answer(project_skills.add_skill(project_id, arguments["skill"]))
    if name == "project_skills.usage":
        return await _answer(skill_usage.view(project_id))
    if name == "project_skills.context_budget":
        return await _answer(skill_lifecycle.context_budget(project_id))
    skill_id = arguments["skill_id"]
    if name == "project_skills.get":
        return await _answer(project_skills.get_skill(project_id, skill_id))
    if name == "project_skills.update":
        return await _answer(project_skills.update_skill(project_id, skill_id, arguments["fields"]))
    if name == "project_skills.versions":
        return await _answer(skill_versions.versions(
            project_id, skill_id, from_version=arguments.get("from_version"),
            to_version=arguments.get("to_version")))
    if name == "project_skills.rollback":
        return await _answer(skill_versions.rollback(project_id, skill_id, arguments["version"]))
    if name == "project_skills.deprecate":
        return await _answer(skill_lifecycle.deprecate(project_id, skill_id, reason=arguments.get("reason", "")))
    if name == "project_skills.restore":
        return await _answer(skill_lifecycle.restore(project_id, skill_id))
    # A pending confirmation reserves nothing: read current references and ownership again.
    # The core deletes the item and all current installations in one transaction.
    await _references(arguments)
    return await _answer(project_skills.delete_skill(project_id, skill_id))


async def _proposals(action, project_id, arguments, principal):
    if action == "list":
        status = skill_proposals.Status(arguments["status"]) if "status" in arguments else None
        return await skill_proposals.list_view(project_id, status=status, limit=arguments.get("limit", 50))
    if action == "set_mode":
        return await skill_proposals.set_mode(project_id, arguments["mode"])
    proposal_id = arguments["proposal_id"]
    if action == "get":
        return await skill_proposals.get_view(project_id, proposal_id)
    if action == "reject":
        return await skill_proposals.reject(project_id, proposal_id, arguments.get("reason", ""), principal=principal)
    if action == "restore":
        return await skill_proposals.restore(project_id, proposal_id, principal=principal)
    if "fields" in arguments:
        return await skill_proposals.edit_and_confirm(
            project_id, proposal_id, arguments["content_sha256"], arguments["fields"], principal=principal)
    return await skill_proposals.confirm(project_id, proposal_id, arguments["content_sha256"], principal=principal)
