"""The Manager's settings tool (Manager tools RFC, section 7): the layers every agent inherits, a role's timeout, and
the Agent Profiles a project's Planners may choose.

A layer changes as the dashboard changes it (``agent_settings.update_system``,
``update_role``, ``update_project``: the shared defaults, a role, a project, a
role in a project), a role's timeout as Settings → Roles saves it
(``db.role_configs``), and the list as the project's Settings tab does
(``projects.set_allowed_profiles``, with the profiles the call adds and
without those it removes), so each refuses with the dashboard's message. A change is ``write``, but ``high`` when it changes the shared
defaults, a concurrency limit (as ``looper_concurrency`` asks for a project's)
or raises a spend cap (a run's cap any role would run with goes up, or away):
the user is shown each value before and after, and confirms first. A limit
set lets what waits check again (``recheck_limits``), as the dashboard does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from ...db import agent_setting_layers as db_layers, profile_registry as db_profiles, role_configs as db_roles
from .. import agent_settings, projects
from ..agent_settings import CONCURRENCY_MAX, EMPTY_LAYER, TASK_ROLES, AgentType, Layer, LayerValues, SettingsError
from ..agent_settings import index_layers, layer_update, resolve, resolve_concurrency
from ..project_execution import format_usd
from ..tiers.journal import clip
from ..tiers.task import recheck_limits
from . import Call, ErrorCode, OpError, Risk, Tool
from .concurrency import COUNTS, shown
from .config import answered, by_id_or_name, project_named

# A role's timeout per turn or run, at most (as a configuration import takes it).
TIMEOUT_MAX_SECONDS = 365 * 86400
FIELD_WORDS = {"harness_prompt": "Harness prompt", "profile_id": "Agent Profile", "env": "环境变量",
               "run_budget_usd": "每次运行的花费上限", "max_concurrency": "并发上限", "timeout_seconds": "超时"}
LIMIT_NOTE = "调低不会停止正在运行的工作：超出的运行完为止，新的会等待；调高后排队的会立即开始。"


@dataclass(frozen=True)
class _Place:
    """The layer a call changes: the shared defaults, a role, a project, or a role in a project."""

    layer: Layer
    role: AgentType | None
    project: Mapping[str, Any] | None

    @property
    def project_id(self) -> str | None:
        return self.project["id"] if self.project else None

    @property
    def roles(self) -> tuple[AgentType, ...]:
        """The roles whose settings it changes."""
        if self.role is not None:
            return (self.role,)
        return TASK_ROLES if self.project else tuple(AgentType)

    @property
    def name(self) -> str:
        project = f"project {self.project['name']}" if self.project else ""
        return {Layer.SYSTEM: "shared defaults", Layer.ROLE: f"role {self.role.value if self.role else ''}",
                Layer.PROJECT: project, Layer.ROLE_PROJECT: f"{self.role.value if self.role else ''} in {project}"
                }[self.layer]

    @property
    def words(self) -> str:
        """The layer's settings as the user is shown them."""
        if self.layer is Layer.SYSTEM:
            return "共用默认（Settings → Roles → Shared defaults：所有角色都继承，下层设置的除外）"
        role = self.role.value.capitalize() if self.role else ""
        if self.layer is Layer.ROLE:
            return f"角色 {role} 在所有项目里的设置"
        name = self.project["name"] if self.project else ""
        return f"项目「{name}」的设置" if self.layer is Layer.PROJECT else f"项目「{name}」里 {role} 的设置"


async def _place(arguments: Mapping[str, Any]) -> _Place:
    role = AgentType(arguments["role"]) if arguments.get("role") else None
    if arguments.get("project") is None:
        return _Place(Layer.ROLE if role else Layer.SYSTEM, role, None)
    if role is AgentType.MANAGER:
        raise OpError(ErrorCode.INVALID, "the manager spans every project: set it without project")
    return _Place(Layer.ROLE_PROJECT if role else Layer.PROJECT, role, await project_named(arguments["project"]))


async def _layers(place: _Place) -> dict[Any, LayerValues]:
    return index_layers(await db_layers.list_layers(place.project_id), place.project_id)


async def _layer_data(place: _Place, values: Mapping[str, Any]) -> dict[str, Any]:
    """``values`` as the dashboard's layer update takes them: ``profile`` (an id or a name) as ``profile_id``, ``env``
    as its list; a ``timeout_seconds`` left out (a role's own, not a layer's)."""
    data: dict[str, Any] = {}
    for name, value in values.items():
        if name == "timeout_seconds":
            if place.layer is not Layer.ROLE:
                raise OpError(ErrorCode.INVALID, "timeout_seconds is a role's own (Settings → Roles): give role "
                                                 "without project")
        elif name == "profile":
            data["profile_id"] = None if value is None else by_id_or_name(
                await db_profiles.list_profiles(), value, id_key="id", what="Agent Profile")["id"]
        elif name == "env":
            data["env"] = None if value is None else [{"key": key, "value": item} for key, item in value.items()]
        else:
            data[name] = value
    return data


def _checked(place: _Place, data: Mapping[str, Any]) -> dict[str, Any]:
    """``data`` as the layer would store it (``layer_update``), refused as the dashboard refuses it."""
    try:
        return layer_update(dict(data), place.layer, place.role) if data else {}
    except SettingsError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None


def _after(layers: Mapping[Any, LayerValues], place: _Place, checked: Mapping[str, Any]) -> dict[Any, LayerValues]:
    """``layers`` once the place's layer has ``checked``'s values (only the budget and the limit matter here)."""
    key = (place.layer, place.role)
    now = layers.get(key, EMPTY_LAYER)
    fields = {name: checked[name] for name in ("run_budget_usd", "max_concurrency") if name in checked}
    return {**layers, key: replace(now, **fields)}


def _raises_a_cap(place: _Place, layers: Mapping[Any, LayerValues], checked: Mapping[str, Any]) -> bool:
    """Whether a run of one of the place's roles would get a higher spend cap, or none instead of one."""
    if "run_budget_usd" not in checked:
        return False
    after = _after(layers, place, checked)
    for role in place.roles:
        before_cap = resolve(role, layers, place.project_id).max_budget_usd
        after_cap = resolve(role, after, place.project_id).max_budget_usd
        if before_cap is not None and (after_cap is None or after_cap > before_cap):
            return True
    return False


def _shown_value(name: str, value: Any, profiles: Mapping[str, str]) -> str:
    """A layer's own value as the user is shown it."""
    if value in (None, [], ""):
        return "未设置（继承上一层）"
    if name == "harness_prompt":
        return f"自定义（{len(value)} 字：{clip(' '.join(value.split()), 40)}）"
    if name == "profile_id":
        return profiles.get(value, value)
    if name == "env":
        return ", ".join(f"{e['key']}={'（取消继承）' if e['value'] is None else e['value']}" for e in value)
    if name == "run_budget_usd":
        return f"${format_usd(value)}"
    return str(value)


async def _confirming(call: Call) -> str | None:
    """What a change of the shared defaults, of a limit, or one raising a spend cap does, for the user to confirm;
    None for any other call (``write``), or another action."""
    if call.arguments["action"] != "set":
        return None
    place = await _place(call.arguments)
    values = _values(call.arguments)
    checked = _checked(place, await _layer_data(place, values))
    layers = await _layers(place)
    raises = _raises_a_cap(place, layers, checked)
    if place.layer is not Layer.SYSTEM and "max_concurrency" not in checked and not raises:
        return None
    profiles = {row["id"]: row["name"] for row in await db_profiles.list_profiles()}
    now = layers.get((place.layer, place.role), EMPTY_LAYER).view()
    lines = [f"要修改{place.words}吗？"]
    for name, value in checked.items():
        if name == "max_concurrency" and place.role is not None:
            after = _after(layers, place, checked)
            lines.append(f"- {FIELD_WORDS[name]}（{COUNTS[place.role]}）："
                         f"{shown(resolve_concurrency(place.role, layers, place.project_id).view())} → "
                         f"{shown(resolve_concurrency(place.role, after, place.project_id).view())}")
        else:
            lines.append(f"- {FIELD_WORDS[name]}：{_shown_value(name, now[name], profiles)} → "
                         f"{_shown_value(name, value, profiles)}")
    if "timeout_seconds" in values:
        lines.append(f"- {FIELD_WORDS['timeout_seconds']}：{await _timeout(place)} 秒 → {values['timeout_seconds']} 秒")
    if raises:
        lines.append("这会提高（或取消）运行的花费上限。")
    if "max_concurrency" in checked:
        lines.append(LIMIT_NOTE)
    return "\n".join(lines)


def _values(arguments: Mapping[str, Any]) -> dict[str, Any]:
    values = arguments.get("values")
    if not values:
        raise OpError(ErrorCode.INVALID, "values: give at least one setting to change (null clears it)")
    return dict(values)


async def _timeout(place: _Place) -> int | None:
    config = await db_roles.get_config(place.role.value) if place.role else None
    return config["timeout_seconds"] if config else None


async def _read(place: _Place) -> dict[str, Any]:
    """The settings view the dashboard reads for the place: Settings → Roles', or the project's Settings tab's."""
    if place.project_id is None:
        return await answered(agent_settings.settings_view())
    return await answered(agent_settings.project_settings_view(place.project_id))


async def _write(place: _Place, data: dict[str, Any]) -> dict[str, Any]:
    """Set the place's layer as the dashboard does; the view it answers with (as ``_read``)."""
    if place.layer is Layer.SYSTEM:
        return await answered(agent_settings.update_system(data))
    if place.layer is Layer.ROLE:
        return await answered(agent_settings.update_role(place.role.value if place.role else "", data))
    return await answered(agent_settings.update_project(place.project_id or "", data,
                                                        role=place.role.value if place.role else None))


def _own(place: _Place, view: Mapping[str, Any]) -> dict[str, Any]:
    """The place's own layer values in a settings view (``LayerValues.view``)."""
    if place.role is not None:
        return view["roles"][place.role.value]["values"]
    return view["system" if place.layer is Layer.SYSTEM else "project"]["values"]


async def _set(call: Call) -> dict[str, Any]:
    place = await _place(call.arguments)
    values = _values(call.arguments)
    data = await _layer_data(place, values)
    profiles = {row["id"]: row["name"] for row in await db_profiles.list_profiles()}
    before = _own(place, await _read(place))
    timeout_before = await _timeout(place)
    view = await _write(place, data) if data else None
    if "timeout_seconds" in values and place.role is not None:
        if await db_roles.set_config(place.role.value, {"timeout_seconds": values["timeout_seconds"] or 0}) is None:
            raise OpError(ErrorCode.NOT_FOUND, f"role {place.role.value!r} has no configuration")
    if "max_concurrency" in data:
        await recheck_limits(call.platform.task_router, call.platform.worker_pool)
    view = view or await _read(place)
    after = _own(place, view)
    changes = [{"setting": name, "before": _shown_value(name, before[name], profiles),
                "after": _shown_value(name, after[name], profiles)} for name in data]
    if "timeout_seconds" in values:
        changes.append({"setting": "timeout_seconds", "before": timeout_before, "after": await _timeout(place)})
    return {"layer": place.name, "changes": changes,
            "effective": {role.value: _effective(view["roles"][role.value]["effective"]) for role in place.roles}}


# --- Reading them ---


def _layer_set(values: Mapping[str, Any], profiles: Mapping[str, str]) -> dict[str, Any]:
    """What one layer sets itself (``LayerValues.view``), the rest left out."""
    return {name: _shown_value(name, value, profiles) for name, value in values.items() if value not in (None, [])}


def _effective(view: Mapping[str, Any]) -> dict[str, Any]:
    """What applies to a role (a settings view's ``effective``), each with the layer it comes from."""
    effective: dict[str, Any] = {
        "profile": {"value": view["profile"]["value"]["name"], "source": view["profile"]["source"]},
        "harness_prompt": {"source": view["harness_prompt"]["source"], "chars": len(view["harness_prompt"]["value"])},
        "env": {entry["key"]: entry["value"] for entry in view["env"]},
        "run_budget_usd": view["run_budget_usd"],
    }
    if "max_concurrency" in view:
        effective["max_concurrency"] = view["max_concurrency"]
    return effective


async def _view(call: Call) -> dict[str, Any]:
    profiles = {row["id"]: row["name"] for row in await db_profiles.list_profiles()}
    if call.arguments.get("project") is None:
        view = await _read(_Place(Layer.SYSTEM, None, None))
        timeouts = {c["role"]: c["timeout_seconds"] for c in await db_roles.list_configs()}
        return {"shared_defaults": _layer_set(view["system"]["values"], profiles),
                "roles": {role: {"set": _layer_set(entry["values"], profiles),
                                 "effective": _effective(entry["effective"]), "timeout_seconds": timeouts.get(role)}
                          for role, entry in view["roles"].items()}}
    project = await project_named(call.arguments["project"])
    view = await _read(_Place(Layer.PROJECT, None, project))
    allowed = await answered(projects.allowed_profiles_view(project["id"]))
    return {"project": project["name"], "project_layer": _layer_set(view["project"]["values"], profiles),
            "roles": {role: {"set": _layer_set(entry["values"], profiles), "effective": _effective(entry["effective"])}
                      for role, entry in view["roles"].items()},
            "allowed_profiles": [p["name"] for p in allowed["profiles"]]}


def _on_list(entry: Mapping[str, Any], names: list[str]) -> bool:
    return any(entry["profile_id"] == name or entry["name"].casefold() == name.strip().casefold() for name in names)


async def _allowed(call: Call) -> dict[str, Any]:
    """Add profiles to a project's allowed list and remove some, then set the whole list as its Settings tab does
    (which checks each one); a profile to remove that is not on it is refused, and nothing changes."""
    add, remove = list(call.arguments.get("add") or ()), list(call.arguments.get("remove") or ())
    if call.arguments.get("project") is None or not add and not remove:
        raise OpError(ErrorCode.INVALID, "project, and add or remove (profile ids or names), are required for action "
                                         "allowed_profiles")
    project = await project_named(call.arguments["project"])
    before = (await answered(projects.allowed_profiles_view(project["id"])))["profiles"]
    if missing := [name for name in remove if not any(_on_list(entry, [name]) for entry in before)]:
        listed = ", ".join(entry["name"] for entry in before) or "none"
        raise OpError(ErrorCode.NOT_FOUND, f"remove: {missing[0]!r} is not on the list of project "
                                           f"{project['name']!r} (it has: {listed})")
    kept = [entry["profile_id"] for entry in before if not _on_list(entry, remove)]
    after = await answered(projects.set_allowed_profiles(project["id"], {"profile_ids": [*kept, *add]}))
    return {"project": project["name"], "before": [entry["name"] for entry in before],
            "allowed_profiles": after["profiles"],
            "note": "the project's Planners may choose these for their tasks' Workers and Reviewers"}


async def _settings(call: Call) -> dict[str, Any]:
    action = call.arguments["action"]
    if action == "view":
        return await _view(call)
    if action == "allowed_profiles":
        return await _allowed(call)
    return await _set(call)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="looper_settings",
        description="Settings agents inherit, a later layer winning: shared defaults → role → project → role in "
                    "project. view (with project: its layers and allowed profiles); set values on one layer (no role "
                    "nor project: shared defaults); allowed_profiles: add or remove the profiles a project's Planners "
                    "may choose. The user confirms shared defaults, limits and higher spend caps first. Model and "
                    "effort are the profile's.",
        schema={
            "type": "object", "required": ["action"], "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["view", "set", "allowed_profiles"]},
                "project": {"type": "string", "minLength": 1, "description": "The project's name."},
                "role": {"type": "string", "enum": [role.value for role in AgentType]},
                "values": {
                    "type": "object", "description": "set: what to change; null inherits.",
                    "properties": {
                        "profile": {"type": "string", "description": "An Agent Profile's id or name."},
                        "harness_prompt": {"type": "string"},
                        "env": {"type": "object", "description": "The layer's whole {NAME: value}; a null value "
                                                                 "unsets an inherited one."},
                        "run_budget_usd": {"type": "number", "description": "Each run's spend cap."},
                        "max_concurrency": {"type": "integer", "minimum": 1, "maximum": CONCURRENCY_MAX,
                                            "description": "Per project (planner: tasks); with role."},
                        "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": TIMEOUT_MAX_SECONDS,
                                            "description": "With role, no project; 0: none."},
                    },
                },
                "add": {"type": "array", "description": "allowed_profiles: profile ids or names."},
                "remove": {"type": "array", "description": "allowed_profiles: as add."},
            },
        },
        risk=Risk.WRITE, run=_settings, escalate=_confirming, read_actions=frozenset({"view"}),
    ),
)
