"""The Manager's /profile command: parsing, the effect of a choice, and the list of choices."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from looper.agents import profile_registry, session_profile
from looper.agents.agent_settings import AgentSettings
from looper.agents.profile_registry import (
    Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
)
from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType, adapter
from looper.agents.session_profile import (
    Effect, ProfileChoice, ProfileCommand, ProfileCommandKind as Kind, ProfileRefError, choices, display_name,
    effect, format_list, parse_profile_command, resolve_ref,
)
from tests.test_agent_settings import _row


def _profile(profile_id: str = "agent-codex", runtime_type: str = "codex", provider_id: str = "prov-cpa",
             model: str = "gpt-5.5", name: str | None = None, effort: str | None = None) -> ResolvedAgentProfile:
    provider = ProviderSpec(provider_id, provider_id, VendorType.CPA, AuthKind.API_KEY)
    protocol = ApiProtocol.OPENAI_RESPONSES if runtime_type == "codex" else ApiProtocol.ANTHROPIC_MESSAGES
    spec = ModelSpec(f"model-{model}", provider.id, model, protocol)
    resolved = ResolvedModel(spec, provider, Endpoint(provider.id, spec.protocol, "http://127.0.0.1:8317/v1"))
    runtime = RuntimeSpec(f"rt-{runtime_type}", runtime_type, runtime_type, "x", adapter(runtime_type).capabilities)
    return ResolvedAgentProfile(profile_id, name or f"{runtime_type}/{model}", runtime, spec.id, effort,
                                resolved_model=resolved)


@pytest.mark.parametrize("text, expected", [
    ("/profile", ProfileCommand(Kind.LIST)),
    (" /PROFILE ", ProfileCommand(Kind.LIST)),
    ("/profile 2", ProfileCommand(Kind.SELECT, "2")),
    ("/profile 0", ProfileCommand(Kind.FOLLOW_SETTINGS)),
    ("/profile\t2", ProfileCommand(Kind.SELECT, "2")),
    ("/profile\n2", ProfileCommand(Kind.SELECT, "2")),
    ("/profile\n", ProfileCommand(Kind.LIST)),
    ("/profile claude-code/opus · xhigh", ProfileCommand(Kind.SELECT, "claude-code/opus · xhigh")),
    ("/profiles", None), ("/profile2", None), ("hello /profile", None), ("", None),
])
def test_parse(text, expected):
    assert parse_profile_command(text) == expected


def test_effect():
    current = _profile()
    session = {"session_id": "s1", "runtime_type": "codex", "provider_id": "prov-cpa"}
    assert effect(None, current.id, _profile("a2")) is Effect.STARTS
    assert effect({"session_id": None}, current.id, _profile("a2")) is Effect.STARTS
    assert effect(session, current.id, current) is Effect.CURRENT
    assert effect(session, current.id, _profile("a2", model="gpt-6")) is Effect.RESUMES
    assert effect(session, current.id, _profile("a3", runtime_type="claude-code")) is Effect.NEW_SESSION
    assert effect(session, current.id, _profile("a4", provider_id="prov-other")) is Effect.NEW_SESSION


def _choice(number: int, profile_id: str, name: str, raw: str = "") -> ProfileChoice:
    return ProfileChoice(number, profile_id, name, "rt", "prov", "m", Effect.STARTS, None, raw or name)


def test_resolve_ref():
    listed = [_choice(1, "a1", "Alpha"), _choice(2, "a2", "Beta · high", "Beta"), _choice(3, "a3", "beta2")]
    assert resolve_ref("2", listed).profile_id == "a2"
    assert resolve_ref("a3", listed).profile_id == "a3"
    assert resolve_ref("Beta · high", listed).profile_id == "a2"
    assert resolve_ref("Beta", listed).profile_id == "a2"
    assert resolve_ref("alpha", listed).profile_id == "a1"
    for ref in ("9", "nope"):
        with pytest.raises(ProfileRefError, match=r"1\. Alpha\n2\. Beta · high"):
            resolve_ref(ref, listed)
    dup = listed + [_choice(4, "a4", "ALPHA")]
    with pytest.raises(ProfileRefError, match="more than one"):
        resolve_ref("aLpHa", dup)


def test_display_name():
    assert display_name({"name": "codex/gpt", "effort": "high"}) == "codex/gpt · high"
    assert display_name({"name": "codex/gpt high", "effort": "high"}) == "codex/gpt high"
    assert display_name({"name": "codex/gpt", "effort": None}) == "codex/gpt"


def test_format_list():
    listed = [replace(_choice(1, "a1", "Alpha"), effect=Effect.CURRENT),
              replace(_choice(2, "a2", "Beta"), problem="no key")]
    text = format_list(listed, settings_name="Alpha", selected=None).splitlines()
    assert text[0] == "0. 跟随设置（现在是 Alpha）（当前）"
    assert text[1] == "1. Alpha · rt · prov"
    assert text[2] == "2. Beta · rt · prov——不可用：no key"
    selected = format_list(listed, settings_name="Alpha", selected="a1").splitlines()
    assert selected[0] == "0. 跟随设置（现在是 Alpha）" and selected[1].endswith("（当前）")
    assert text[-1].startswith("发送 /profile <编号> 切换")


def _view(profile: ResolvedAgentProfile, roles: list[str]) -> dict[str, Any]:
    return {"profile_id": profile.id, "name": profile.name, "effort": profile.effort, "roles": roles,
            "runtime_name": profile.runtime.name, "provider_name": profile.resolved_model.provider.name,
            "model": {"name": profile.model}}


def test_choices(monkeypatch):
    from looper.db import profile_registry as db

    zeta, alpha, worker_only = _profile("p1", name="zeta"), _profile("p2", name="alpha"), _profile("p3", name="w")
    rows = {p.id: _row(p) for p in (zeta, alpha, worker_only)}

    async def view(**_):
        return {"profiles": [_view(zeta, ["manager", "worker"]), _view(alpha, ["manager"]),
                             _view(worker_only, ["worker"])]}

    async def get_profile(profile_id, **_):
        return rows[profile_id]

    async def no_key(*_, **__):
        return None

    monkeypatch.setattr(profile_registry, "registry_view", view)
    monkeypatch.setattr(db, "get_profile", get_profile)
    monkeypatch.setattr(db, "get_api_key", no_key)
    listed = asyncio.run(choices(None, replace(AgentSettings(), profile=zeta)))
    assert [(c.number, c.name) for c in listed] == [(1, "alpha"), (2, "zeta")]
    assert [c.effect for c in listed] == [Effect.STARTS, Effect.CURRENT]
    assert all("no API key" in (c.problem or "") for c in listed)
