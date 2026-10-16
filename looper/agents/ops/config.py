"""The Manager's T2 tools (Manager tools RFC, section 7): projects, Agent Profiles, LLM providers, the capability
catalog and spending; its settings tool is ``ops.settings``.

Each tool calls the code the dashboard's API calls (``projects``,
``profile_registry``, ``catalog``, ``db.llm_records``), so both check alike
and a refusal carries the dashboard's own message (``answered``). Listing and
reading are ``read``; a change is ``write``; deleting a project or an Agent
Profile is ``high``, asked first with code's words. A provider is registered
without its API key: no tool takes a key (``check_arguments``), and the user
enters it on the dashboard.
"""

from __future__ import annotations

from collections.abc import Awaitable, Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any, TypeVar

from ...db import llm_records as db_llm_records, projects as db_projects
from .. import catalog, profile_registry, projects
from ..agent_settings import SettingsError, SettingsNotFound
from ..projects import RequestError, project_name
from ..tiers.journal import clip
from . import Call, ErrorCode, OpError, Risk, Tool
from .tasks import _TASK_ID, _request_error, _task, _task_argument

T = TypeVar("T")

# How many of a project's tasks looper_costs lists, most first.
COST_TASKS_LISTED = 10
KEY_NOTE = ("Looper stores no API key from chat: ask the user to enter the provider's key on the dashboard (Settings → "
            "LLM Providers → {name} → API key); its Agent Profiles cannot run until then.")


async def answered(request: Awaitable[T]) -> T:
    """``request``'s result; a refusal of the dashboard's code as the tool's error, with its message and the code
    its HTTP status stands for."""
    try:
        return await request
    except RequestError as exc:
        raise _request_error(exc) from None
    except (profile_registry.RegistryNotFound, catalog.CatalogNotFound, SettingsNotFound) as exc:
        raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
    except (profile_registry.RegistryConflict, catalog.CatalogConflict) as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None
    except (profile_registry.RegistryError, catalog.CatalogError, SettingsError) as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None


async def project_named(raw: Any) -> dict[str, Any]:
    """The project of that name (ignoring case), with its counts; refuses an unknown one."""
    try:
        name = project_name(raw)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, f"project: {exc}") from None
    row = await db_projects.find_project_by_name(name)
    project = await db_projects.get_project(row["id"]) if row is not None else None
    if project is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no project named {name!r}; the Projects line before the message lists "
                                           "them")
    return project


def by_id_or_name(rows: Iterable[Mapping[str, Any]], wanted: Any, *, id_key: str, what: str,
                  keys: tuple[str, ...] = ("name",)) -> dict[str, Any]:
    """The row whose ``id_key`` is ``wanted``, else whose ``keys`` value is (ignoring case); refuses none, naming
    the rows there are."""
    rows = list(rows)
    text = str(wanted or "").strip()
    match = next((r for r in rows if r[id_key] == text), None) or next(
        (r for r in rows for key in keys if str(r.get(key) or "").casefold() == text.casefold()), None)
    if match is None:
        names = ", ".join(str(r[keys[0]]) for r in rows[:30])
        raise OpError(ErrorCode.NOT_FOUND, f"no {what} {text!r}; there are: {names or 'none'}")
    return dict(match)


def _required(arguments: Mapping[str, Any], action: str, *names: str) -> None:
    missing = [name for name in names if arguments.get(name) is None]
    if missing:
        raise OpError(ErrorCode.INVALID, f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required for "
                                         f"action {action}")


def _given(arguments: Mapping[str, Any], names: Iterable[str], *, clears: Iterable[str] = ()) -> dict[str, Any]:
    """The arguments of ``names`` the call gives; a null one only of ``clears``, where null clears it."""
    clears = set(clears)
    return {name: arguments[name] for name in names
            if name in arguments and (arguments[name] is not None or name in clears)}


# --- looper_projects ---


def _project_listed(view: Mapping[str, Any]) -> dict[str, Any]:
    return {"project": view["name"], "work_dir": view["work_dir"] or None, "tasks": view["task_count"],
            "active_tasks": view["active_count"], "schedules": view["schedule_count"],
            "background": clip(view["background"], 200) or None}


async def _projects(call: Call) -> dict[str, Any]:
    arguments, action = call.arguments, call.arguments["action"]
    if action == "list":
        return {"projects": [_project_listed(p) for p in (await projects.list_projects())["projects"]]}
    if action == "create":
        _required(arguments, action, "name")
        fields = {"name": arguments["name"], **_given(arguments, ("work_dir", "background"))}
        return {"project": await answered(projects.create_project(fields))}
    _required(arguments, action, "project")
    project = await project_named(arguments["project"])
    if action == "get":
        return {"project": projects.project_view(project)}
    if action == "update":
        fields = _given(arguments, ("name", "work_dir", "background"))
        return {"project": await answered(projects.update_project(project["id"], fields))}
    await answered(projects.delete_project(project["id"]))
    return {"deleted": project["name"]}


async def _deleting_project(call: Call) -> str | None:
    """What deleting a project does, for the user to confirm; None for the other actions."""
    if call.arguments["action"] != "delete":
        return None
    _required(call.arguments, "delete", "project")
    project = await answered(projects.deletable_project((await project_named(call.arguments["project"]))["id"]))
    return (f"要删除项目「{project['name']}」（默认目录：{project['work_dir'] or '没有'}）吗？它没有任务和定时任务。它的 "
            "Agent 设置、规划者可选的 Agent Profile 列表、技能和 MCP 的安装、项目知识和它自己写的技能会一起删除，不能恢复；"
            "目录里的文件不受影响。")


# --- looper_agent_profiles and looper_providers ---


def _profile_listed(view: Mapping[str, Any]) -> dict[str, Any]:
    used = profile_registry.usage_text(view["used_by"])
    return {"profile_id": view["profile_id"], "name": view["name"], "runtime": view["runtime_name"],
            "provider": view["provider_name"], "model": view["model"], "effort": view["effort"],
            "agent_teams": view["agent_teams"], "roles": view["roles"], "builtin": view["builtin"],
            "used_by": used or None}


def _provider_listed(view: Mapping[str, Any]) -> dict[str, Any]:
    listed = {"provider_id": view["provider_id"], "name": view["name"], "auth": view["auth"],
              "protocols": view["protocols"], "base_url": view["base_url"] or None,
              "models": [m["name"] for m in view["models"]], "profiles": view["profile_count"],
              "builtin": view["builtin"]}
    if view["auth"] == "api_key":
        listed["api_key"] = "set" if view["api_key"] else "missing: the user enters it on the dashboard"
    return listed


async def _registry() -> dict[str, Any]:
    return await profile_registry.registry_view()


def _profile(registry: Mapping[str, Any], wanted: Any) -> dict[str, Any]:
    return by_id_or_name(registry["profiles"], wanted, id_key="profile_id", what="Agent Profile")


def _profile_parts(registry: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The runtime and provider a call names (each by id or name; a runtime also by its type), as the registry's
    ids."""
    parts = {}
    if arguments.get("runtime") is not None:
        parts["runtime_id"] = by_id_or_name(registry["runtimes"], arguments["runtime"], id_key="runtime_id",
                                            what="runtime", keys=("name", "type"))["runtime_id"]
    if arguments.get("provider") is not None:
        parts["provider_id"] = by_id_or_name(registry["providers"], arguments["provider"], id_key="provider_id",
                                             what="provider")["provider_id"]
    return parts


def _profile_answer(answer: Mapping[str, Any]) -> dict[str, Any]:
    return {"profile": _profile_listed(answer["profile"]), "warnings": answer["warnings"]}


async def _profiles(call: Call) -> dict[str, Any]:
    arguments, action = call.arguments, call.arguments["action"]
    registry = await _registry()
    if action == "list":
        return {"profiles": [_profile_listed(p) for p in registry["profiles"]],
                "runtimes": [{"runtime_id": r["runtime_id"], "name": r["name"], "type": r["type"], "roles": r["roles"]}
                             for r in registry["runtimes"]],
                "providers": [{"provider_id": p["provider_id"], "name": p["name"],
                               "models": [m["name"] for m in p["models"]] or p["suggested_models"]}
                              for p in registry["providers"]]}
    fields = {**_given(arguments, ("name", "model", "effort", "agent_teams"), clears=("effort",)),
              **_profile_parts(registry, arguments)}
    if action == "create":
        _required(arguments, action, "name", "runtime", "provider", "model")
        return _profile_answer(await answered(profile_registry.create_profile(fields)))
    _required(arguments, action, "profile")
    profile = _profile(registry, arguments["profile"])
    if action == "update":
        return _profile_answer(await answered(profile_registry.update_profile(profile["profile_id"], fields)))
    await answered(profile_registry.delete_profile(profile["profile_id"]))
    return {"deleted": profile["name"]}


async def _deleting_profile(call: Call) -> str | None:
    """What deleting an Agent Profile does, for the user to confirm; None for the other actions."""
    if call.arguments["action"] != "delete":
        return None
    _required(call.arguments, "delete", "profile")
    profile = _profile(await _registry(), call.arguments["profile"])
    row = await answered(profile_registry.deletable_profile(profile["profile_id"]))
    effort = f" · effort {row['effort']}" if row.get("effort") else ""
    return (f"要删除 Agent Profile「{row['name']}」（{row['runtime_name']} · {row['provider_name']} · {row['model']}"
            f"{effort}）吗？没有设置层、任务、定时任务或项目的可选列表在用它；删除后不能恢复。")


async def _providers(call: Call) -> dict[str, Any]:
    arguments = call.arguments
    if arguments["action"] == "list":
        return {"providers": [_provider_listed(p) for p in (await _registry())["providers"]]}
    _required(arguments, "register", "name", "protocols")
    data: dict[str, Any] = {"name": arguments["name"], "protocols": arguments["protocols"], "auth": "api_key",
                            "base_url": arguments.get("base_url"),
                            "models": [{"name": model} for model in arguments.get("models") or ()],
                            "env": [{"key": key, "value": value} for key, value in (arguments.get("env") or {}).items()]}
    answer = await answered(profile_registry.create_provider(data))
    provider = _provider_listed(answer["provider"])
    return {"provider": provider, "note": KEY_NOTE.format(name=provider["name"])}


# --- looper_catalog ---


def _install_listed(view: Mapping[str, Any]) -> dict[str, Any]:
    return {"install_id": view["install_id"], "scope": view["scope"], "project": view["project_name"],
            "roles": view["roles"], "enabled": view["enabled"]}


def _item_listed(view: Mapping[str, Any]) -> dict[str, Any]:
    return {"item_id": view["item_id"], "kind": view["kind"], "name": view["qualified_name"],
            "description": clip(view["description"] or "", 160) or None, "project": view["owner_project_name"]}


async def _catalog(call: Call) -> dict[str, Any]:
    arguments, action = call.arguments, call.arguments["action"]
    project = await project_named(arguments["project"]) if arguments.get("project") is not None else None
    if action == "list":
        if project is not None:
            view = await answered(catalog.project_view(project["id"]))
            return {"project": project["name"], "installs": [
                {**_install_listed(entry), "inherited": entry["inherited"], "item": _item_listed(entry["item"])}
                for entry in view["installs"]]}
        view = await answered(catalog.catalog_view())
        return {"items": [{**_item_listed(item), "installs": [_install_listed(i) for i in item["installs"]]}
                          for item in view["items"]]}
    if action == "install":
        _required(arguments, action, "item")
        items = (await answered(catalog.catalog_view()))["items"]
        item = by_id_or_name(items, arguments["item"], id_key="item_id", what="catalog item",
                             keys=("qualified_name", "name"))
        data = {"item_id": item["item_id"], "scope": "project" if project else "global",
                **({"project_id": project["id"]} if project else {}), **_given(arguments, ("roles",))}
        answer = await answered(catalog.install(data))
        return {"install": _install_listed(answer["install"]), "item": _item_listed(answer["item"]),
                "note": "it loads from each agent's next run on"}
    _required(arguments, action, "install_id")
    if action == "update":
        answer = await answered(catalog.update_install(arguments["install_id"],
                                                       _given(arguments, ("roles", "enabled"))))
        return {"install": _install_listed(answer["install"]), "item": _item_listed(answer["item"])}
    answer = await answered(catalog.uninstall(arguments["install_id"]))
    return {"deleted": arguments["install_id"], "item": _item_listed(answer["item"])}


# --- looper_costs ---

_SPEND_NOTE = ("provider_priced_usd is money (runs with an API key); equivalent_usd is a runtime's own login (a "
               "subscription) at API prices, not money spent. A running run's cost is not known yet.")


def _moment(raw: str, name: str, *, end: bool) -> datetime:
    """A day (YYYY-MM-DD) or a date-time in ISO 8601, in the server's time zone when it names none; a day as the end
    counts whole."""
    text = raw.strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            raise OpError(ErrorCode.INVALID, f"{name} must be a day (YYYY-MM-DD) or an ISO 8601 date-time, not "
                                             f"{raw!r}") from None
        return moment if moment.tzinfo is not None else moment.astimezone()
    start = datetime.combine(day + timedelta(days=1) if end else day, datetime.min.time()).astimezone()
    return start


def _range(arguments: Mapping[str, Any]) -> tuple[datetime | None, datetime | None]:
    period = arguments.get("period")
    if period is not None and (arguments.get("since") or arguments.get("until")):
        raise OpError(ErrorCode.INVALID, "give period, or since and until, not both")
    if period is not None:
        from ..overview import local_midnight

        today = local_midnight()
        start = {"today": today, "this_week": today - timedelta(days=today.weekday()),
                 "this_month": today.replace(day=1)}[period]
        return start, None
    since = _moment(arguments["since"], "since", end=False) if arguments.get("since") else None
    until = _moment(arguments["until"], "until", end=True) if arguments.get("until") else None
    if since is not None and until is not None and until <= since:
        raise OpError(ErrorCode.INVALID, "until must be after since")
    return since, until


async def _costs(call: Call) -> dict[str, Any]:
    arguments = call.arguments
    if arguments.get("task_id") is not None:
        if any(arguments.get(name) is not None for name in ("project", "period", "since", "until")):
            raise OpError(ErrorCode.INVALID, "a task's cost is its whole cost: give task_id alone")
        task = await _task(arguments["task_id"])
        cost = (await db_llm_records.task_costs([task["id"]]))[task["id"]]
        return {"task_id": task["id"], "name": task["name"], "status": task["status"], "cost": cost,
                "note": "cost_usd is its Planner's, Workers' and Reviewers' runs, as the task page shows it"}
    since, until = _range(arguments)
    project = await project_named(arguments["project"]) if arguments.get("project") is not None else None
    project_id = project["id"] if project else None
    answer: dict[str, Any] = {"since": since, "until": until, **({"project": project["name"]} if project else {}),
                              "total": await db_llm_records.spend_since(since, until=until, project_id=project_id)}
    tasks = await db_llm_records.task_spend(since, until, project_id=project_id)
    if project is not None:
        answer["tasks"] = [{key: row[key] for key in ("task_id", "name", "provider_priced_usd", "equivalent_usd",
                                                     "runs")} for row in tasks[:COST_TASKS_LISTED]]
        answer["more_tasks"] = max(0, len(tasks) - COST_TASKS_LISTED)
    else:
        by_project: dict[str | None, dict[str, Any]] = {}
        for row in tasks:
            entry = by_project.setdefault(row["project_id"], {"project": row["project_name"], "provider_priced_usd": 0.0,
                                                              "equivalent_usd": 0.0, "runs": 0, "tasks": 0})
            for key in ("provider_priced_usd", "equivalent_usd", "runs"):
                entry[key] += row[key]
            entry["tasks"] += 1
        answer["projects"] = sorted(by_project.values(),
                                    key=lambda e: -(e["provider_priced_usd"] + e["equivalent_usd"]))
        answer["note_projects"] = "projects counts its tasks' runs; the total also counts the Manager's"
    answer["note"] = _SPEND_NOTE
    return answer


# --- The tools ---

_PROJECT = {"type": "string", "minLength": 1, "description": "The project's name."}
_ROLES = {"type": "array", "description": "Of manager (global only), planner, worker, reviewer; default all."}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_projects",
        description="Projects: list; get one; create (name, and the work_dir and background the user gave; never "
                    "pick a directory); update its name, work_dir or background; delete one without tasks or "
                    "schedules (the user confirms first).",
        schema={
            "type": "object", "required": ["action"], "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                "project": {**_PROJECT, "description": "The project (get, update, delete)."},
                "name": {"type": "string", "minLength": 1, "description": "create: its name; update: its new name."},
                "work_dir": {"type": "string", "description": "Its default absolute directory; '' for none."},
                "background": {"type": "string", "description": "What its agents should know."},
            },
        },
        risk=Risk.WRITE, run=_projects, escalate=_deleting_project, read_actions=frozenset({"list", "get"}),
    ),
    Tool(
        name="looper_agent_profiles",
        description="Agent Profiles (runtime, provider, model, effort): list (with runtimes and providers); create; "
                    "update; delete one nothing uses (the user confirms first). A role's profile: looper_settings.",
        schema={
            "type": "object", "required": ["action"], "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "update", "delete"]},
                "profile": {"type": "string", "minLength": 1, "description": "Its id or name (update, delete)."},
                "name": {"type": "string", "minLength": 1, "description": "create: its name; update: its new name."},
                "runtime": {"type": "string", "description": "A runtime's id, name or type."},
                "provider": {"type": "string", "description": "A provider's id or name."},
                "model": {"type": "string"},
                "effort": {"type": "string", "description": "low, medium, high, xhigh, max; null: the runtime's."},
                "agent_teams": {"type": "boolean", "description": "Claude Code only."},
            },
        },
        risk=Risk.WRITE, run=_profiles, escalate=_deleting_profile, read_actions=frozenset({"list"}),
    ),
    Tool(
        name="looper_providers",
        description="LLM providers: list (with whether each has its API key); register one, without its key. Never "
                    "take or pass a key: the user enters it on the dashboard.",
        schema={
            "type": "object", "required": ["action"], "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "register"]},
                "name": {"type": "string", "minLength": 1},
                "protocols": {"type": "array", "description": "Of anthropic-messages, openai-responses, openai-chat."},
                "base_url": {"type": "string", "description": "http(s) URL; leave out for the protocol's own."},
                "models": {"type": "array", "description": "Model names it serves."},
                "env": {"type": "object", "description": "Extra environment for its runs: {NAME: value}."},
            },
        },
        risk=Risk.WRITE, run=_providers, read_actions=frozenset({"list"}),
    ),
    Tool(
        name="looper_catalog",
        description="Skills, plugins and MCP servers: list with installs (with project: what reaches it); install "
                    "an item globally or in a project, for some roles; update an install's roles or enabled; "
                    "uninstall. Agents load it from their next run.",
        schema={
            "type": "object", "required": ["action"], "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["list", "install", "update", "uninstall"]},
                "project": {**_PROJECT, "description": "A project (list, install); leave out for global."},
                "item": {"type": "string", "description": "Its id or name (install)."},
                "install_id": {"type": "string", "description": "From list (update, uninstall)."},
                "roles": _ROLES,
                "enabled": {"type": "boolean"},
            },
        },
        risk=Risk.WRITE, run=_catalog, read_actions=frozenset({"list"}),
    ),
    Tool(
        name="looper_costs",
        description="Spending: one task's cost; or a time range's (default all time) by project, or with project "
                    "by its tasks. Money and subscription equivalent apart.",
        schema={
            "type": "object", "additionalProperties": False,
            "properties": {
                "task_id": _TASK_ID,
                "project": _PROJECT,
                "period": {"type": "string", "enum": ["today", "this_week", "this_month"]},
                "since": {"type": "string", "description": "A day (YYYY-MM-DD) or ISO date-time."},
                "until": {"type": "string", "description": "As since; a day counts whole."},
            },
        },
        risk=Risk.READ, run=_costs, task_id=_task_argument,
    ),
)
