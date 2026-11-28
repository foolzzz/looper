"""Desktop RPC message rules, with no socket and no driver."""

import pytest

from looper.agents.browser.desktop.contract import (
    KINDS,
    MAX_FRAME_BYTES,
    NON_RETRIED,
    PROTOCOL_VERSION,
    TIMEOUT_S,
    ContractError,
    LeaseNotInstalled,
    acquire_machine_lease,
    dumps,
    encode_request,
    in_use_code,
    loads,
    parse_request,
)

_NONCE = "n" * 16
_SESSION = "session-1"
_CAPABILITY = "c" * 16


def _call(kind="health", body=None, **extra):
    value = {
        "v": PROTOCOL_VERSION,
        "kind": kind,
        "nonce": _NONCE,
        "session_id": _SESSION,
        "capability": _CAPABILITY,
        "body": {} if body is None else body,
    }
    value.update(extra)
    return value


def test_protocol_version_frame_cap_and_timeouts_are_fixed():
    assert PROTOCOL_VERSION == 1
    assert MAX_FRAME_BYTES == 4 * 1024 * 1024
    assert set(TIMEOUT_S) == set(KINDS)
    assert TIMEOUT_S["act"] == 15.0
    assert {"act", "grant", "revoke", "stop"} <= set(NON_RETRIED)
    assert "observe" not in NON_RETRIED


def test_a_request_round_trips():
    raw = encode_request("health", _NONCE, {"window": "main"}, session_id=_SESSION, capability=_CAPABILITY)
    parsed = parse_request(loads(raw))
    assert parsed.kind == "health" and parsed.body == {"window": "main"}
    assert parsed.session_id == _SESSION and parsed.capability == _CAPABILITY


def test_session_open_rejects_a_capability_and_a_body():
    with pytest.raises(ContractError) as claimed:
        parse_request({"v": 1, "kind": "session.open", "nonce": _NONCE, "capability": _CAPABILITY, "body": {}})
    assert claimed.value.code == "bad_request"
    with pytest.raises(ContractError) as carried:
        parse_request({"v": 1, "kind": "session.open", "nonce": _NONCE, "body": {"approved": False}})
    assert carried.value.code == "untrusted_identity"


def test_an_action_requires_a_session_capability():
    value = _call("act", {"name": "press"})
    del value["capability"]
    with pytest.raises(ContractError) as exc:
        parse_request(value)
    assert exc.value.code == "bad_request"


def test_a_self_reported_identity_is_rejected_even_nested():
    with pytest.raises(ContractError) as top:
        parse_request(_call(caller_uid=0))
    assert top.value.code == "untrusted_identity"
    with pytest.raises(ContractError) as nested:
        parse_request(_call(body={"target": {"identity": "operator"}}))
    assert nested.value.code == "untrusted_identity"


def test_an_unknown_field_and_a_bad_version_are_rejected():
    with pytest.raises(ContractError) as extra:
        parse_request(_call(note="hello"))
    assert extra.value.code == "bad_request"
    with pytest.raises(ContractError) as version:
        parse_request(_call(v=2))
    assert version.value.code == "version_mismatch"


def test_a_short_nonce_is_rejected():
    with pytest.raises(ContractError) as exc:
        parse_request(_call(nonce="short"))
    assert exc.value.code == "bad_request"


def test_an_oversize_payload_is_rejected_before_it_is_sent():
    payload = _call(body={"blob": "x" * MAX_FRAME_BYTES})
    with pytest.raises(ContractError) as exc:
        dumps(payload)
    assert exc.value.code == "frame_too_large"


def test_in_use_names_the_holding_session():
    assert in_use_code("other-session") == "in use by other-session"
    with pytest.raises(ContractError):
        in_use_code("bad session")


def test_the_machine_lease_is_not_installed():
    with pytest.raises(LeaseNotInstalled):
        acquire_machine_lease("session-1", "owner", 30.0)
