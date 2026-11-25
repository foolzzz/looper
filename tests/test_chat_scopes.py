"""Scope parsing, resolution and launch authority, without database or model calls."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from looper.agents import chat_scopes as scopes, runtimes
from looper.agents.agent_settings import Access, AgentSettings, Launch
from looper.agents.managers_api import ManagerTools, TaskPolicy, TaskScope
from looper.agents.profile_registry import builtin_resolved_profile

PERSON, BOT = scopes.PrincipalKind.PERSON, scopes.PrincipalKind.BOT
OPERATOR, SHARED = scopes.ScopeAudience.OPERATOR, scopes.ScopeAudience.SHARED
PLATFORM, RESTRICTED, FULL = (scopes.LaunchMode.PLATFORM_TOOLS, scopes.LaunchMode.RESTRICTED_READ,
                              scopes.LaunchMode.FULL)
SUPPORTED = frozenset({PLATFORM, FULL})
UNBOUND = scopes.ScopeConfig(False, None, False, {})


def _config(default=None, *, legacy=False, override=None):
    return scopes.ScopeConfig(True, default, legacy, override or {})


def _definition(**changes):
    return SimpleNamespace(**dict(dict(tools=ManagerTools.ALL, task_scope=TaskScope.ALL,
                                      scope_project_ids=("project-b", "project-a"), task_policy=TaskPolicy.FREE),
                                 **changes))


def _effective(*items):
    return scopes.effective(items, _definition())


@pytest.mark.parametrize("sender,kind,operator,config,expected,audience", [
    ("operator", PERSON, "operator", _config(), "full", OPERATOR),
    ("operator", None, "operator", _config(), "chat_only", SHARED),
    ("operator", BOT, "operator", _config("set-group"), "chat_only", SHARED),
    ("operator", PERSON, None, _config("set-group"), "set-group", SHARED),
    ("member", PERSON, "operator", _config("set-group", override={
        "member": scopes.Override("member", PERSON, "set-person")}), "set-person", SHARED),
    ("bot-alias", BOT, "operator", _config("set-group", override={
        "bot-alias": scopes.Override("bot", BOT, "set-bot")}), "set-bot", SHARED),
    ("bot", BOT, "operator", _config(), "chat_only", SHARED),
    ("bot", BOT, "operator", _config("legacy", legacy=True), "legacy", SHARED),
    ("member", PERSON, "operator", _config("legacy", legacy=True), "legacy", SHARED),
    ("member", PERSON, "operator", _config(), "chat_only", SHARED),
    (None, None, None, UNBOUND, "chat_only", SHARED),
    ("operator", PERSON, "operator", _config(override={
        "operator": scopes.Override("operator", PERSON, "chat_only")}), "full", OPERATOR),
], ids=["operator", "missing-kind", "bot-not-operator", "no-operator-id", "person-override", "bot-override",
        "bot-default-null", "bot-legacy", "person-legacy", "person-default-null", "unbound", "operator-first"])
def test_resolve(sender, kind, operator, config, expected, audience):
    result = scopes.resolve(sender=sender, sender_kind=kind, operator_id=operator, config=config)
    assert result == scopes.Resolved(expected, audience, kind or BOT, config.legacy_allowed)


@pytest.mark.parametrize("scope,audience,legacy", [
    ("full", SHARED, False), ("chat_only", OPERATOR, False), ("legacy", SHARED, False),
], ids=["shared-full", "operator-not-full", "legacy-not-allowed"])
def test_resolved_rejects(scope, audience, legacy):
    with pytest.raises(scopes.ScopeInvariantViolation):
        scopes.Resolved(scope, audience, PERSON, legacy)


@pytest.mark.parametrize("scope", ["full", "legacy"])
def test_resolved_rejects_override(scope):
    with pytest.raises(scopes.ScopeInvariantViolation):
        scopes.resolve(sender="member", sender_kind=PERSON, operator_id=None,
                       config=_config(override={"member": scopes.Override("member", PERSON, scope)}))


@pytest.mark.parametrize("scope,audience,items,mode,reason", [
    ("chat_only", SHARED, (scopes.Chat(), scopes.Shell()), FULL, "shell.*scope"),
    ("chat_only", SHARED, (scopes.Chat(),), FULL, "full.*launch"),
    ("full", OPERATOR, (scopes.Chat(), scopes.Shell()), PLATFORM, "shell.*full"),
], ids=["shell-scope", "full-authority", "shell-mode"])
def test_session_scope_rejects(scope, audience, items, mode, reason):
    with pytest.raises(scopes.ScopeInvariantViolation, match=reason):
        scopes.SessionScope(scopes.Resolved(scope, audience, PERSON, False), 1,
                            _effective(*items), mode, "synthetic-fingerprint")


def test_session_scope_rejects_legacy_without_permission():
    with pytest.raises(scopes.ScopeInvariantViolation):
        scopes.session_scope(scopes.Resolved("legacy", SHARED, PERSON, False), 1, [{"kind": "chat"}],
                             _definition(), supported=SUPPORTED, inputs_version=1)


TASK = {"kind": "tasks", "projects": "manager", "actions": ["read", "create", "instruct", "start", "stop", "restart"],
        "visibility": "project", "templates_only": False}
FILES = {"kind": "files.read", "roots": ["chat_attachments"], "deny": [], "required": False}
CHAT = {"kind": "chat"}


@pytest.mark.parametrize("raw", [
    None, {}, ["chat"], [{"kind": "unknown"}], [{"kind": []}], [{}],
    [dict(CHAT, extra=True)], [CHAT, {"kind": "tasks"}], [CHAT, dict(TASK, projects=1)],
    [CHAT, dict(TASK, projects="other")], [CHAT, dict(TASK, projects=[1])],
    [CHAT, dict(TASK, actions="read")], [CHAT, dict(TASK, actions=[1])],
    [CHAT, dict(TASK, actions=["cancel"])], [CHAT, dict(TASK, actions=["start_over"])],
    [CHAT, dict(TASK, actions=["reopen"])], [CHAT, dict(TASK, visibility="all")],
    [CHAT, dict(TASK, templates_only=1)], [CHAT, CHAT], [{"kind": "shell"}],
    [CHAT, dict(FILES, roots=[])], [CHAT, dict(FILES, roots=["snapshot:public"])],
    [CHAT, dict(FILES, roots=["task_dir"])], [CHAT, dict(FILES, roots="chat_attachments")],
    [CHAT, dict(FILES, roots=["chat_attachments", "chat_attachments"])],
    [CHAT, dict(FILES, deny=[False])], [CHAT, dict(FILES, required="false")],
    [CHAT, {"kind": "vault.read", "paths": ["notes"]}], [CHAT, {"kind": "vault.read", "paths": ""}],
    [CHAT, {"kind": "skills", "ids": [None]}], [CHAT, {"kind": "mcp", "servers": {}}],
    [CHAT, {"kind": "schedules", "actions": ["create"]}],
])
def test_parse_rejects(raw):
    with pytest.raises(scopes.ScopeUnavailable) as error:
        scopes.parse_items(raw, scope="set-invalid")
    assert error.value.scope == "set-invalid"
    assert error.value.reason


# These are the migration's literals. Compare canonical ordering for both item and action lists.
BUILTIN_JSON = {
    "chat_only": [{"kind": "chat"}],
    "full": [{"kind": "chat"}, TASK, {"kind": "schedules", "actions": ["read", "manage"]}, {"kind": "shell"}],
    "legacy": [{"kind": "chat"}, TASK, {"kind": "schedules", "actions": ["read", "manage"]}, {"kind": "shell"}],
}


@pytest.mark.parametrize("name", BUILTIN_JSON)
def test_builtins_round_trip(name):
    version, items = scopes.BUILTIN_VERSIONS[name]
    assert version == 1
    actual = scopes.to_json(items)
    expected = sorted(({key: sorted(value) if isinstance(value, list) else value for key, value in item.items()}
                       for item in BUILTIN_JSON[name]), key=lambda item: item["kind"])
    assert actual == expected
    assert all(list(item) == sorted(item) for item in actual)
    assert scopes.parse_items(actual, scope=name) == items


def test_item_collections_are_canonical():
    raw = [CHAT, dict(TASK, projects=["b", "a", "b"], actions=["create", "read", "read"]),
           dict(FILES, deny=["z", "a", "z"]), {"kind": "vault.read", "paths": []},
           {"kind": "skills", "ids": ["z", "a"]}, {"kind": "mcp", "servers": ["z", "a"]}]
    items = scopes.parse_items(raw, scope="set-canonical")
    assert scopes.parse_items(scopes.to_json(items), scope="set-canonical") == items
    assert scopes.Tasks(("b", "a"), frozenset({scopes.TaskAction.READ}), scopes.TaskVisibility.CHAT, False) == (
        scopes.Tasks(("a", "b"), frozenset({scopes.TaskAction.READ}), scopes.TaskVisibility.CHAT, False))
    for item in scopes.to_json(items):
        assert list(item) == sorted(item)
        assert all(value == sorted(set(value)) for value in item.values() if isinstance(value, list))


@pytest.mark.parametrize("policy,templates,projects,task_scope,expected_projects,expected_templates", [
    (TaskPolicy.TEMPLATES_ONLY, False, "manager", TaskScope.ALL, "manager", True),
    (TaskPolicy.FREE, True, "manager", TaskScope.OWN, "manager", True),
    (TaskPolicy.FREE, False, ("project-b", "project-c"), TaskScope.PROJECTS, ("project-b",), False),
    (TaskPolicy.FREE, False, ("project-c",), TaskScope.PROJECTS, None, False),
    (TaskPolicy.FREE, False, ("project-c",), TaskScope.ALL, ("project-c",), False),
])
def test_effective(policy, templates, projects, task_scope, expected_projects, expected_templates):
    task = scopes.Tasks(projects, frozenset({scopes.TaskAction.READ}), scopes.TaskVisibility.PROJECT, templates)
    result = scopes.effective((scopes.Chat(), task), _definition(task_policy=policy, task_scope=task_scope))
    expected = (scopes.Chat(),) if expected_projects is None else (
        scopes.Chat(), replace(task, projects=expected_projects, templates_only=expected_templates))
    assert result.items == expected
    assert result.ceiling == scopes.Ceiling("all", task_scope.value, ("project-a", "project-b"), policy.value)
    assert scopes.Ceiling.of(_definition(tools="tasks", task_scope="own", task_policy="free")) == (
        scopes.Ceiling("tasks", "own", ("project-a", "project-b"), "free"))


@pytest.mark.parametrize("extra,supported,mode,dropped", [
    ((scopes.Shell(),), frozenset(), FULL, False),
    ((), frozenset(), PLATFORM, False),
    ((scopes.FilesRead(("chat_attachments",), (), False),), SUPPORTED, PLATFORM, True),
    ((scopes.FilesRead(("chat_attachments",), (), False),), frozenset({RESTRICTED}), RESTRICTED, False),
    ((scopes.FilesRead(("chat_attachments",), (), True),), SUPPORTED, RESTRICTED, False),
    ((scopes.VaultRead(()),), SUPPORTED, RESTRICTED, False),
    ((scopes.FilesRead(("chat_attachments",), (), False), scopes.VaultRead(())), SUPPORTED, RESTRICTED, False),
    ((scopes.Shell(), scopes.Skills(("skill",)), scopes.Mcp(("server",))), SUPPORTED, FULL, False),
], ids=["shell", "chat", "optional-drop", "optional-supported", "required", "vault", "vault-required", "shell-library"])
def test_launch_mode(extra, supported, mode, dropped):
    eff = _effective(scopes.Chat(), *extra)
    actual_mode, actual_eff = scopes.launch_mode(eff, supported)
    assert actual_mode is mode
    assert actual_eff == (_effective(scopes.Chat()) if dropped else eff)


@pytest.mark.parametrize("item", [scopes.Skills(("skill",)), scopes.Mcp(("server",))])
def test_launch_mode_rejects_unavailable_library(item):
    with pytest.raises(scopes.ScopeUnavailable, match="step 4"):
        scopes.launch_mode(_effective(scopes.Chat(), item), SUPPORTED)


def test_fingerprint():
    eff = _effective(scopes.Chat())
    params = dict(scope_id="chat_only", version=1, eff=eff, audience=SHARED, inputs_version=1, mode=PLATFORM)
    actual = scopes.fingerprint(**params)
    payload = {"set": ["chat_only", 1], "items": [CHAT], "ceiling": asdict(eff.ceiling), "audience": "shared",
               "inputs": 1, "mode": "platform_tools"}
    assert actual == hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False).encode()).hexdigest()[:16]
    assert scopes.fingerprint(**params) == actual
    for change in [dict(scope_id="set-other"), dict(version=2), dict(audience=OPERATOR), dict(inputs_version=2),
                   dict(mode=RESTRICTED), dict(eff=_effective(scopes.Chat(), scopes.Shell())),
                   dict(eff=replace(eff, ceiling=replace(eff.ceiling, tools="tasks")))]:
        assert scopes.fingerprint(**dict(params, **change)) != actual
    assert set(inspect.signature(scopes.fingerprint).parameters) == set(params)
    task = scopes.parse_items([CHAT, TASK], scope="set-tasks")
    task_eff = scopes.effective(task, _definition())
    changed_task = tuple(replace(item, visibility=scopes.TaskVisibility.CHAT) if isinstance(item, scopes.Tasks)
                         else item for item in task_eff.items)
    assert scopes.fingerprint(**dict(params, eff=task_eff)) != (
        scopes.fingerprint(**dict(params, eff=replace(task_eff, items=changed_task))))
    assert scopes.fingerprint(**dict(params, eff=scopes.effective(task, _definition()))) != (
        scopes.fingerprint(**dict(params, eff=scopes.effective(task, _definition(task_policy="templates_only")))))
    assert scopes.fingerprint(**dict(params, eff=scopes.effective(task, _definition(templates=("new",))))) == (
        scopes.fingerprint(**dict(params, eff=scopes.effective(task, _definition()))))


@pytest.mark.parametrize("extra,expected", [((scopes.Shell(),), None), ((), frozenset())])
def test_looper_tools(extra, expected):
    assert scopes.looper_tools(_effective(scopes.Chat(), *extra)) == expected


@pytest.mark.parametrize("raw", [[CHAT, TASK], [CHAT, {"kind": "schedules", "actions": ["read"]}]])
def test_looper_tools_rejects_unavailable_task_entry(raw):
    with pytest.raises(scopes.ScopeUnavailable, match="step 2"):
        scopes.looper_tools(scopes.effective(scopes.parse_items(raw, scope="set-tasks"), _definition()))


@pytest.mark.parametrize("name", BUILTIN_JSON)
def test_session_scope(name):
    resolved = scopes.Resolved(name, OPERATOR if name == "full" else SHARED, PERSON, name == "legacy")
    result = scopes.session_scope(resolved, 1, BUILTIN_JSON[name], _definition(),
                                  supported=SUPPORTED, inputs_version=1)
    assert result == scopes.SessionScope(resolved, 1, scopes.effective(scopes.BUILTIN_VERSIONS[name][1], _definition()),
                                        PLATFORM if name == "chat_only" else FULL, result.fingerprint)
    assert result.fingerprint == scopes.fingerprint(scope_id=name, version=1, eff=result.effective,
                                                   audience=resolved.audience, inputs_version=1, mode=result.mode)


@pytest.mark.parametrize("runtime", runtimes.ADAPTERS.values(), ids=lambda runtime: runtime.type)
def test_runtime_launch_modes(runtime):
    assert runtime.launch_modes == (SUPPORTED if runtime.type in {"claude-code", "api"} else frozenset({FULL}))
    assert not hasattr(runtime, "supports_platform_tools_only")
    if PLATFORM not in runtime.launch_modes:
        profile = builtin_resolved_profile()
        profile = replace(profile, runtime=replace(profile.runtime, type=runtime.type), agent_teams=False)
        launch = Launch(settings=AgentSettings(profile=profile), access=Access.PLATFORM_TOOLS)
        with pytest.raises(runtimes.RuntimeUnsupported, match="cannot restrict a routed Manager session"):
            launch.args()


def test_session_scope_fingerprints_the_narrowed_items():
    resolved = scopes.Resolved("set-files", SHARED, PERSON, False)
    result = scopes.session_scope(resolved, 1, [CHAT, FILES], _definition(),
                                  supported=SUPPORTED, inputs_version=1)
    assert result.mode is PLATFORM
    assert result.effective == _effective(scopes.Chat())
    assert result.fingerprint == scopes.fingerprint(scope_id="set-files", version=1, eff=_effective(scopes.Chat()),
                                                   audience=SHARED, inputs_version=1, mode=PLATFORM)


def test_session_scope_reports_the_unavailable_set():
    with pytest.raises(scopes.ScopeUnavailable) as error:
        scopes.session_scope(scopes.Resolved("set-skills", SHARED, PERSON, False), 1,
                             [CHAT, {"kind": "skills", "ids": ["skill"]}], _definition(),
                             supported=SUPPORTED, inputs_version=1)
    assert error.value.scope == "set-skills"
