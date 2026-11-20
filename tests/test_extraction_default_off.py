"""With no extraction subscription, a push subscription's processor run is what it was before extraction existed:
its system prompt, its command line and the confirmation version a live push subscription is confirmed under are
pinned to a frozen sample taken from ``main`` before extraction. Pure: no database, no model."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from looper.agents import processor, subscriptions
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
    for kind in ("leads", "intents"):  # an extraction's version binds its kind and its chats too
        other = {**schedule, "subscription": {**schedule["subscription"], "output_kind": kind}}
        assert asyncio.run(subscriptions.config_version(other, profile=facts)) != version
