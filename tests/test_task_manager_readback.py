"""The readback's comparison and reading, against canned answers shaped as Claude Code 2.1.332 gives them."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from typing import Any

import pytest

from looper.agents import task_manager_readback as rb
from looper.agents import task_manager_sandbox as sandbox
from looper.agents.claude_cli import CONTROL_STARTUP_SEC, CONTROL_TIMEOUT_SEC, ControlUnavailable
from looper.agents.task_manager_safety import Reason
from looper.agents.task_manager_sandbox import Check
from tests.task_manager_readback_support import KNOWN, READ_DENY, Case, FakeResident, tool_rule

WIDER = Reason.LAUNCH_SETTING_MISMATCH


@pytest.fixture
def case(tmp_path) -> Case:
    return Case(tmp_path)


def _pin_cases():
    from pathlib import Path
    pins = sandbox.pins(Path("/work"), allowed_domains=("api.example.com",), read_deny=READ_DENY)
    return [pin for pin in pins if pin.check is not Check.NOT_ASSERTED]


def _loosened(pin) -> Any:
    if pin.check is Check.EQUAL:
        return (not pin.value) if isinstance(pin.value, bool) else "loosened"
    if pin.check is Check.ARRAY_EQUAL:
        return [*pin.value, "/tmp/x.sock"]
    if pin.check is Check.CONTAINS:
        return []
    return {"set": "by the user"}  # ABSENT


def test_a_clean_readback_has_no_mismatch(case) -> None:
    assert case.compare() == []


@pytest.mark.parametrize("pin", _pin_cases(), ids=lambda pin: pin.item)
def test_a_user_layer_that_loosens_a_pin_is_refused_with_its_item(case, pin) -> None:
    pin = next(candidate for candidate in case.expected.pins if candidate.key == pin.key)
    case.user_layer(pin.key, _loosened(pin))
    assert [(m.reason, m.item, m.source, m.key) for m in case.compare()] == [
        (WIDER, pin.item, rb.Source.USER, ".".join(pin.key))]


def test_array_pins_refuse_extras_and_contains_pins_allow_them(case) -> None:
    deny_write = case.injected["sandbox"]["filesystem"]["denyWrite"]
    case.settings["effective"]["sandbox"]["filesystem"]["denyWrite"] = [*deny_write, "/extra/secret"]
    assert case.compare() == []  # narrower only
    case.settings["effective"]["sandbox"]["filesystem"]["denyWrite"] = deny_write[:-1]
    assert [m.item for m in case.compare()] == ["F11"]
    case.settings["effective"]["sandbox"]["filesystem"]["denyWrite"] = deny_write
    case.settings["effective"]["sandbox"]["network"]["allowUnixSockets"] = ["/tmp/x.sock"]
    case.settings["effective"]["sandbox"]["excludedCommands"] = ["git"]
    case.settings["effective"]["sandbox"]["network"]["allowedDomains"].append("evil.example")
    assert sorted(m.item for m in case.compare()) == sorted(
        ["sandbox.network.allowUnixSockets", "sandbox.excludedCommands", "sandbox.network.allowedDomains"])


def test_a_policy_layer_is_blamed_on_the_policy_and_the_notice_does_not_quote_it(case) -> None:
    case.settings["effective"]["sandbox"]["network"]["allowedDomains"].append("policy-secret.example")
    case.settings["sources"].append({"source": "policySettings", "settings": {
        "sandbox": {"network": {"allowedDomains": ["policy-secret.example"]}}}})
    [mismatch] = case.compare()
    assert mismatch.source is rb.Source.POLICY
    text = rb.notice([mismatch], "2.1.332")
    assert "organization-managed policy" in text and "policy-secret" not in text


def test_a_value_no_layer_claims_is_attributed_to_unknown(case) -> None:
    case.settings["effective"]["sandbox"]["enabled"] = False
    [mismatch] = case.compare()
    assert (mismatch.item, mismatch.source) == ("F1", rb.Source.UNKNOWN)
    assert "upgrade probe" in rb.notice([mismatch], "2.1.999")


def test_a_key_under_sandbox_the_verified_readback_does_not_know_is_refused(case) -> None:
    case.settings["effective"]["sandbox"]["network"]["newKey"] = True
    case.settings["effective"]["sandbox"]["brandNew"] = {"a": 1}
    mismatches = case.compare()
    assert [(m.item, m.actual) for m in mismatches] == [("unknown_key", "sandbox.network.newKey"),
                                                        ("unknown_key", "sandbox.brandNew")]
    assert "upgrade probe" in rb.notice(mismatches, "2.1.999")


def test_a_project_layer_in_sources_is_refused(case) -> None:
    case.settings["sources"].append({"source": "projectSettings", "settings": {}})
    assert [(m.item, m.actual) for m in case.compare()] == [("sources", ["project"])]


@pytest.mark.parametrize("source,refused", [("policySettings", False), ("userSettings", False),
                                            ("projectSettings", True), ("flagSettings", True),
                                            ("localSettings", True), ("sessionHook", True)])
def test_hooks_come_from_the_user_or_the_policy_only(case, source, refused) -> None:
    case.hooks["hooks"].append({"event": "Stop", "matcher": "", "source": source, "type": "command"})
    assert [m.item for m in case.compare()] == (["hooks"] if refused else [])


EXTRA_TOOLS = ["Read", "Grep", "Glob", "TodoWrite", "Skill", "ToolSearch", "WebFetch", "Edit", "Monitor"]


@pytest.mark.parametrize("name", [tool for tool in EXTRA_TOOLS if tool in KNOWN])
def test_a_builtin_outside_the_allowlist_that_is_not_denied_is_refused(case, name) -> None:
    case.rules["state"]["rules"] = [rule for rule in case.rules["state"]["rules"] if rule["rule"] != name]
    [mismatch] = case.compare()
    assert (mismatch.item, mismatch.actual) == ("tools", [name])


def test_an_allowed_tool_that_is_denied_is_refused(case) -> None:
    case.rules["state"]["rules"].append(tool_rule("Bash"))
    [mismatch] = case.compare()
    assert (mismatch.item, mismatch.actual) == ("tools", ["Bash"])


def test_a_deny_rule_of_another_source_does_not_count_as_narrowing(case) -> None:
    case.rules["state"]["rules"] = [{**rule, "source": "flagSettings"} if rule["rule"] == "Read" else rule
                                    for rule in case.rules["state"]["rules"]]
    assert [(m.item, m.actual) for m in case.compare()] == [("tools", ["Read"])]


def test_unremovable_tools_and_allowed_server_tools_pass_and_a_foreign_tool_does_not(case) -> None:
    assert case.compare() == []  # EndConversation / WaitForMcpServers are in KNOWN and never required denied
    case.mcp["mcpServers"][0]["tools"].append({"name": "x"})
    assert case.compare() == []
    case.mcp["mcpServers"].append({"name": "stray", "status": "connected", "config": {}, "tools": [{"name": "x"}]})
    assert sorted((m.item, m.actual) for m in case.compare() if m.item == "tools") == [
        ("tools", ["mcp__stray__x"])]


@pytest.mark.parametrize("value", [None, "true"])
def test_tool_search_must_stay_off(case, value) -> None:
    env = case.settings["effective"]["env"]
    if value is None:
        del env["ENABLE_TOOL_SEARCH"]
    else:
        case.user_layer(("env", "ENABLE_TOOL_SEARCH"), value)
    assert [m.item for m in case.compare()] == ["F9"]


def test_forbidden_env_is_refused_by_name_and_never_shows_its_value(case) -> None:
    case.user_layer(("env", "HTTPS_PROXY"), "http://secret@x")
    [mismatch] = case.compare()
    assert (mismatch.item, mismatch.actual, mismatch.source) == ("env", ["HTTPS_PROXY"], rb.Source.USER)
    assert "secret" not in rb.notice([mismatch], "2.1.332")


def test_an_extra_writable_path_is_refused(case) -> None:
    case.sandbox["restrictions"]["fs_allow_write"] = ["/srv/extra/secrets"]
    assert [m.item for m in case.compare()] == ["F3"]
    case.sandbox["restrictions"]["fs_allow_write"] = []
    case.sandbox["lists"]["rows"][0]["entries"].append("/private/tmp/shared")
    assert [m.item for m in case.compare()] == ["F3.effective"]


def test_the_permission_mode_must_be_bypass_when_it_was_read(case) -> None:
    case.mode = "default"
    assert [m.item for m in case.compare()] == ["permissions.defaultMode"]
    case.mode = None  # before a later turn it is not read again
    assert case.compare() == []


@pytest.mark.parametrize("source,refused", [("userSettings", False), ("projectSettings", True), ("localSettings", True)])
def test_no_agent_from_the_task_directory(case, source, refused) -> None:
    case.context["agents"].append({"agentType": "pagent", "source": source, "path": "p"})
    assert [(m.item, m.actual) for m in case.compare()] == ([("agents", ["pagent"])] if refused else [])


def test_an_extra_or_missing_mcp_server_is_a_set_mismatch(case) -> None:
    case.mcp["mcpServers"].append({"name": "stray", "status": "connected", "config": {"type": "stdio"}})
    [extra] = case.compare()
    assert (extra.reason, extra.item, extra.actual) == (Reason.MCP_SET_MISMATCH, "set", ["docs", "looper", "stray"])
    case.mcp["mcpServers"] = case.mcp["mcpServers"][:1]
    assert [m.item for m in case.compare()] == ["set"]


def test_a_changed_definition_is_a_fingerprint_mismatch_and_the_notice_hides_it(case) -> None:
    server = next(s for s in case.mcp["mcpServers"] if s["name"] == "docs")
    server["config"]["args"] = [*server["config"]["args"][:-1], "--evil-flag"]
    [mismatch] = case.compare()
    assert (mismatch.reason, mismatch.item) == (Reason.MCP_SET_MISMATCH, "fingerprint:docs")
    text = rb.notice([mismatch], "2.1.332")
    assert "docs" in text and "evil" not in text and "usr/bin" not in text


@pytest.mark.parametrize("change", ["unwrapped", "other_dir", "altered_head"])
def test_a_wrapped_server_must_have_exactly_the_launch_shape(case, change) -> None:
    server = next(s for s in case.mcp["mcpServers"] if s["name"] == "docs")
    config = server["config"]
    if change == "unwrapped":
        config["command"], config["args"] = config["args"][4], config["args"][5:]
    elif change == "other_dir":
        config["args"][3] = "/elsewhere/mcp-cwd"
    else:
        config["args"][1] = 'cd "$1" && shift && exec "$@" # evil'
    assert [(m.reason, m.item) for m in case.compare()] == [(Reason.MCP_SET_MISMATCH, "wrapper:docs")]


def test_supported_false_or_a_dependency_error_is_sandbox_unavailable(case) -> None:
    case.sandbox["supported"] = False
    case.sandbox["dependencies"]["errors"] = ["ripgrep missing"]
    assert [(m.reason, m.item) for m in case.compare()] == [(Reason.SANDBOX_UNAVAILABLE, "supported"),
                                                            (Reason.SANDBOX_UNAVAILABLE, "dependencies")]
    case.sandbox.update(supported=True, dependencies={"errors": [], "warnings": ["slow"]})
    assert case.compare() == []


def test_the_notice_names_the_user_key_to_narrow(case) -> None:
    case.user_layer(("sandbox", "excludedCommands"), ["git"])
    text = rb.notice(case.compare(), "2.1.332")
    assert "`sandbox.excludedCommands`" in text and "user settings" in text


# --- reading ---


def _read(case: Case, phase: str, **overrides: Any) -> tuple[FakeResident, Any]:
    resident = FakeResident({**case.responses(), **overrides})
    return resident, asyncio.run(rb.read(resident, phase))


@pytest.mark.parametrize("phase", ["launch", "resume"])
def test_a_launch_reads_initialize_first_with_the_startup_wait(case, phase) -> None:
    resident, readback = _read(case, phase)
    subtypes = [request["subtype"] for request, _ in resident.requests]
    assert subtypes[0] == "initialize" and subtypes.count("initialize") == 1
    assert [timeout for _, timeout in resident.requests][:2] == [CONTROL_STARTUP_SEC, CONTROL_TIMEOUT_SEC]
    assert readback.permission_mode == "bypassPermissions"
    assert rb.compare(readback, case.expected) == []


def test_a_later_turn_does_not_send_initialize_and_has_no_mode(case) -> None:
    resident, readback = _read(case, "turn")
    assert "initialize" not in [request["subtype"] for request, _ in resident.requests]
    assert {timeout for _, timeout in resident.requests} == {CONTROL_TIMEOUT_SEC}
    assert readback.permission_mode is None
    assert {request["subtype"] for request, _ in resident.requests} == {
        "get_settings", "get_sandbox_dialog", "get_hooks_listing", "mcp_status", "list_permission_rules",
        "get_context_usage"}
    assert next(r for r, _ in resident.requests if r["subtype"] == "get_sandbox_dialog")["lists"] is True
    assert next(r for r, _ in resident.requests if r["subtype"] == "get_context_usage")["detail"] == "summary"


def test_pending_mcp_servers_are_polled_until_connected(case, monkeypatch) -> None:
    monkeypatch.setattr(rb, "MCP_PENDING_POLL_SEC", 0)
    answers = [{"mcpServers": [{"name": "docs", "status": "pending", "config": {}}]}] * 2 + [case.mcp]
    resident, readback = _read(case, "turn", mcp_status=lambda request: answers.pop(0))
    assert [r["subtype"] for r, _ in resident.requests].count("mcp_status") == 3
    assert readback.mcp["mcpServers"][0]["status"] == "connected"


def test_a_server_that_stays_pending_is_unavailable(case, monkeypatch) -> None:
    monkeypatch.setattr(rb, "MCP_PENDING_POLL_SEC", 0)
    monkeypatch.setattr(rb, "MCP_PENDING_WAIT_SEC", 0.01)
    with pytest.raises(rb.ReadbackUnavailable) as caught:
        _read(case, "turn", mcp_status={"mcpServers": [{"name": "docs", "status": "pending", "config": {}}]})
    assert caught.value.item == "mcp_status.pending"


@pytest.mark.parametrize("subtype", ["initialize", "get_settings", "get_sandbox_dialog", "get_hooks_listing",
                                     "mcp_status", "list_permission_rules", "get_context_usage"])
@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_a_request_that_fails_names_its_subtype(case, subtype, failure) -> None:
    error = ControlUnavailable(subtype, "error response" if failure == "error" else "no answer within 5 s")
    with pytest.raises(rb.ReadbackUnavailable) as caught:
        _read(case, "launch", **{subtype: error})
    assert caught.value.item == subtype


@pytest.mark.parametrize("subtype,path", [
    ("initialize", ("current_permission_mode",)), ("get_settings", ("effective",)), ("get_settings", ("sources",)),
    ("get_sandbox_dialog", ("supported",)), ("get_sandbox_dialog", ("dependencies", "errors")),
    ("get_sandbox_dialog", ("restrictions", "fs_allow_write")), ("get_sandbox_dialog", ("lists", "rows")),
    ("get_hooks_listing", ("hooks",)), ("mcp_status", ("mcpServers",)), ("list_permission_rules", ("state", "rules")),
    ("get_context_usage", ("agents",))])
def test_a_missing_or_mistyped_field_names_its_path(case, subtype, path) -> None:
    for damage in ("missing", "mistyped"):
        body = copy.deepcopy(case.responses()[subtype])
        node = body
        for key in path[:-1]:
            node = node[key]
        if damage == "missing":
            del node[path[-1]]
        else:
            node[path[-1]] = 7
        with pytest.raises(rb.ReadbackUnavailable) as caught:
            _read(case, "launch", **{subtype: body})
        assert caught.value.item == f"{subtype}.{'.'.join(path)}"


def test_the_effective_write_set_is_not_found_without_its_row(case) -> None:
    body = copy.deepcopy(case.sandbox)
    body["lists"]["rows"] = [row for row in body["lists"]["rows"] if row["id"] != "writes_allowed"]
    with pytest.raises(rb.ReadbackUnavailable) as caught:
        _read(case, "turn", get_sandbox_dialog=body)
    assert caught.value.item == "get_sandbox_dialog.lists.rows.writes_allowed"


def test_the_write_set_rule_follows_home_uid_and_the_claude_temp_root(tmp_path) -> None:
    entries = rb.writes_allowed("/h", 7, str(tmp_path))
    assert f"{tmp_path.resolve()}/claude-7/" in entries and "/h/.claude/debug" in entries and "." in entries
    assert f"{os.path.realpath('/tmp')}/claude-7/" in rb.writes_allowed("/h", 7)  # /private/tmp on macOS, /tmp on Linux


# --- the positive check of a turn's system/init ---

MCP = frozenset({"mcp__docs__do", "mcp__looper__do"})


@pytest.mark.parametrize("tools,ok", [
    (["Bash", *sorted(MCP)], True),
    (["Bash", "EndConversation", "WaitForMcpServers", *sorted(MCP)], True),
    (["Bash", "EndConversation", *sorted(MCP)], True),
    (["Bash", *sorted(MCP), "Read"], False),
    (["Bash", *sorted(MCP), "ToolSearch"], False),
    (["Bash", "mcp__docs__do"], False),  # a server's tool is missing: not what was launched
    (["Bash", *sorted(MCP), "mcp__stray__x"], False),
    ([*sorted(MCP)], False),
    (None, False),
    ("Bash", False),
])
def test_init_tools_are_the_allowlist_plus_the_servers_tools(tools, ok) -> None:
    event = {"type": "system", "subtype": "init", "tools": tools, "permissionMode": "bypassPermissions"}
    found = rb.check_init(event, MCP)
    assert (found == []) is ok
    assert all(m.item == "tools" and m.reason is WIDER for m in found)


@pytest.mark.parametrize("mode", ["default", "acceptEdits", None])
def test_init_permission_mode_must_be_bypass(mode) -> None:
    event = {"tools": ["Bash"], "permissionMode": mode}
    assert [m.item for m in rb.check_init(event, frozenset())] == ["permissions.defaultMode"]


def test_the_mcp_tool_names_come_from_mcp_status(case) -> None:
    assert rb.mcp_tool_names(case.readback()) == {"mcp__docs__do", "mcp__looper__do"}


# --- the first answer after a start is slow, later ones are not allowed to be ---


def test_a_slow_first_answer_is_waited_for_and_a_slow_later_one_is_not(case, monkeypatch) -> None:
    from tests.test_resident_control import _idle

    monkeypatch.setattr(rb, "CONTROL_STARTUP_SEC", 2.0)
    monkeypatch.setattr(rb, "CONTROL_TIMEOUT_SEC", 0.2)
    bodies = case.responses()

    async def run(slow_later: bool) -> None:
        resident, writer, output = _idle()
        delays = []

        async def after() -> None:
            request = json.loads(writer.lines[-1])
            delay = 0.6 if (not delays or (slow_later and len(delays) == 2)) else 0.0
            delays.append(delay)

            async def answer() -> None:
                await asyncio.sleep(delay)
                await output._dispatch({"type": "control_response", "response": {
                    "request_id": request["request_id"], "subtype": "success",
                    "response": bodies[request["request"]["subtype"]]}})

            asyncio.get_running_loop().create_task(answer())

        writer.after_write = after
        await rb.read(resident, "launch")

    asyncio.run(run(False))  # the first answer took 0.6 s, three times the later timeout: no refusal
    with pytest.raises(rb.ReadbackUnavailable):
        asyncio.run(run(True))
