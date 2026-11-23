"""With no extraction subscription, a push subscription's processor run is what it was before extraction existed:
its system prompt, its command line and the confirmation version a live push subscription is confirmed under are
pinned to a frozen sample taken from ``main`` before extraction. With the leads check off (``check_items``), an
extraction's processor prompt and confirmation version are pinned to ``main`` before the check. Pure: no database,
no model."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from looper.agents import extraction, processor, subscriptions
from looper.agents.agent_settings import AgentType, Launch, resolve
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.runtimes import CLAUDE_CODE

INSTRUCTIONS = "Pick the funny things people said. Reply SKIP when there is nothing."


def _settings():
    return replace(resolve(AgentType.PROCESSOR, {}), profile=builtin_resolved_profile())


def test_a_push_subscriptions_system_prompt_is_byte_identical_without_a_contract():
    role = processor._Role(processor.PROCESSOR_ROLE_PROMPT.strip(), True, 0, 0, True)
    prompt = processor._system_prompt(_settings(), role, INSTRUCTIONS, "SKIP")
    # Frozen from main before extraction existed.
    assert hashlib.sha256(prompt.text.encode()).hexdigest() == (
        "173c06cacda721593cff1732dc80d76e003efe556aa739e0b5a3dc8d3c9cad81")
    assert prompt.layers["contract"] == {"name": "PROCESSOR_CONTRACT", "sha256": (
        "d0223396444cc7ddaba84db09c11d698997672c1a23a13936ff5fbf10f5f5d42")}
    assert processor._system_prompt(_settings(), role, INSTRUCTIONS, "SKIP", None).text == prompt.text
    # A caller's contract replaces only the reply section, under the same layer name.
    other = processor._system_prompt(_settings(), role, INSTRUCTIONS, "NONE", "## Your reply\nJSON only.")
    assert other.text == prompt.text.replace(processor.reply_contract("SKIP"), "## Your reply\nJSON only.")
    assert other.layers["contract"]["name"] == "PROCESSOR_CONTRACT"


def test_a_processor_command_line_is_unchanged():
    launch = Launch(replace(_settings(), max_budget_usd=0.5), role=AgentType.PROCESSOR)
    command = CLAUDE_CODE.run_command("claude", cwd=Path("/w/run"), prompt_file=Path("/w/p.md"), launch=launch,
                                      max_turns=0)
    home = str(Path.home())
    shown = [part.replace(home, "~") for part in command]
    settings = json.loads(shown[shown.index("--settings") + 1])
    assert settings == {"autoMemoryEnabled": False, "crossSessionInbound": "refuse", "disableAllHooks": True,
                        "claudeMdExcludes": [
                            "/w/CLAUDE.md", "/w/.claude/CLAUDE.md", "/w/.claude/rules/**", "/w/AGENTS.md",
                            "/w/.claude/AGENTS.md", "/CLAUDE.md", "/.claude/CLAUDE.md", "/.claude/rules/**",
                            "/AGENTS.md", "/.claude/AGENTS.md", "~/.claude/CLAUDE.md", "~/.claude/rules/**",
                            "~/.claude/AGENTS.md"]}
    shown[shown.index("--settings") + 1] = "<settings>"
    assert shown == ["claude", "--print", "--verbose", "--output-format", "stream-json", "--include-partial-messages",
                     "--input-format", "stream-json", "--setting-sources", "", "--strict-mcp-config", "--settings",
                     "<settings>", "--system-prompt-file", "/w/p.md", "--system-prompt-snapshot", "off",
                     "--permission-mode", "manual", "--permission-prompts", "none", "--tools", "",
                     "--disable-slash-commands", "--no-session-persistence", "--model", "opus", "--max-budget-usd",
                     "0.5"]


def test_a_push_subscriptions_confirmation_version_is_unchanged():
    """A live push subscription is confirmed under this version: extraction's facts must not change it."""
    schedule = {"id": "sched-x", "project_id": "p", "notify_channel": "lark", "notify_chat": "ou_operatorsynthetic",
                "cron": "5 * * * *", "timezone": "America/Los_Angeles",
                "subscription": {"source_id": "stream", "output_kind": "push", "include_chats": ["oc_a"],
                                 "exclude_chats": []}}
    facts = {"profile_id": "builtin", "effort": None, "hooks": None,
             "runtime": {"id": "r", "type": "claude_code", "launcher": "direct", "executable": None}}
    version = asyncio.run(subscriptions.config_version(schedule, profile=facts))
    assert version == "110d72fb4c2c2ce72110d17fa95e0be8ae3ee92b059f74218712f966b837b965"  # frozen from main
    unchecked = {**schedule, "subscription": {**schedule["subscription"], "check_items": False}}
    assert asyncio.run(subscriptions.config_version(unchecked, profile=facts)) == version
    for kind in ("leads", "intents"):  # an extraction's version binds its kind and its chats too
        other = {**schedule, "subscription": {**schedule["subscription"], "output_kind": kind}}
        assert asyncio.run(subscriptions.config_version(other, profile=facts)) != version


FACTS = {"profile_id": "builtin", "effort": None, "hooks": None,
         "runtime": {"id": "r", "type": "claude_code", "launcher": "direct", "executable": None}}


def test_with_the_check_off_an_extractions_prompt_and_version_are_unchanged():
    role = processor._Role(processor.PROCESSOR_ROLE_PROMPT.strip(), True, 0, 0, True)
    types = [{"key": "dev_task", "name": "开发任务", "guidance": "有人请用户做开发工作。"}]
    frozen = {"leads": "f88c0689036ce3d958d2fa9d001ee5fc79348cd89b279c19d00613ff77c63f4d",  # from main before the check
              "intents": "61c2a5642158ab27dfca8b9f7745d7a4dd87f820bf16a0e9b061629e2f56a49b"}
    for kind, requirement in (("leads", extraction.LEADS_INSTRUCTIONS), ("intents", extraction.INTENTS_INSTRUCTIONS)):
        prompt = processor._system_prompt(_settings(), role, extraction.instructions(kind, requirement, types), "NONE",
                                          extraction.CONTRACTS[kind])
        assert hashlib.sha256(prompt.text.encode()).hexdigest() == frozen[kind]
    schedule = {"id": "sched-x", "project_id": "p", "notify_channel": "lark", "notify_chat": "ou_operatorsynthetic",
                "cron": "*/10 * * * *", "timezone": "America/Los_Angeles",
                "subscription": {"source_id": "stream", "output_kind": "intents", "include_chats": [],
                                 "exclude_chats": ["oc_b"], "check_items": False}}
    version = asyncio.run(subscriptions.config_version(schedule, profile=FACTS))
    assert version == "8525737e5275fbceb34a52e69c33f83d45f55bf5d2d53771e73c26d2620219aa"  # frozen from main
    # Turning it on binds the check and its Profile: another Profile is another version.
    checked = {**schedule, "subscription": {**schedule["subscription"], "check_items": True}}
    on = asyncio.run(subscriptions.config_version(checked, profile=FACTS, checker=FACTS))
    other = asyncio.run(subscriptions.config_version(checked, profile=FACTS, checker={**FACTS, "effort": "high"}))
    assert len({version, on, other}) == 3


def test_leads_and_intents_instructions_and_contracts_match_the_parent_branch():
    # Frozen on the parent branch before triage execution was added.
    expected = {
        "LEADS_INSTRUCTIONS": "a9a52bf682a687f31d86f2262685a6ba3426cd6ac72231881c361cdc8500e9ed",
        "INTENTS_INSTRUCTIONS": "4a624ca0122ea382a62046dae151fd061509de12a6a6ef4806a493b3fd643466",
        "LEADS_CONTRACT": "652b522619f35c002ec63dd3cc29cfde1199c6e6514097390dc715b633eaa13f",
        "INTENTS_CONTRACT": "85519bbba55d7894333e571301e7ae23dfe87a24a3e71aa85f3612fb27f70e10",
    }
    for name, digest in expected.items():
        assert hashlib.sha256(getattr(extraction, name).encode()).hexdigest() == digest


def test_existing_subscription_rendered_inputs_match_the_parent_branch():
    rows = [{"seq": 11, "sent_at": datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc), "chat_id": "synthetic-chat",
             "chat_kind": "p2p", "chat_name": "Synthetic chat", "sender_id": "synthetic-peer", "sender_name": "Peer",
             "sender_kind": "user", "text": "Synthetic status update", "external_id": "synthetic-message",
             "parent_id": None}]
    for kind in ("leads", "intents", "push"):
        text = (subscriptions.processor_input(rows, text_max_chars=200, zone_name="UTC") if kind == "push" else
                extraction.render(kind, rows, context=[], participants=extraction.Participants(
                    {"synthetic-peer": "Peer"}, "synthetic-operator"), known=[], zone_name="UTC", text_max_chars=200)[0])
        expected = ("cb93601fa21feeb389636cc71aee891745b946e975b409b9cfd7ca3214810291" if kind == "push" else
                    "b1c69d64003a6f05951176d3ca7f623430e1bc5c1bd8c14e06b332b8173e50d8")
        assert hashlib.sha256(text.encode()).hexdigest() == expected
