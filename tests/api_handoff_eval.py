"""Focused API handoff comparison using isolated Claude keychain login or explicit credentials.

Only public synthetic conversation is used. Reports and raw events belong in
ignored local output; neither operator configuration nor runtime state is read.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any

from evals.prompt_regression.runner import TaskError, install_claude
from looper.agents import claude_cli
from looper.agents.agent_settings import AgentType
from looper.agents.runtimes import ADAPTERS
from looper.agents.system_prompt_mode import SystemPromptMode
from tests.db_support import fresh_schema
from tests.fake_model_api import ResponseScript, text_events
from tests.runtime_model_helpers import builtin_launch
from tests.api_manager_support import _manager, _message

USER = ("Keep every change read-only. Deletion has not run and needs explicit user confirmation. "
        "Which region should receive the first rollout?")
ANSWER = ("The inventory audit finished and found exactly 17 stale entries; evidence: audit-17. "
          "I promised to prepare a rollback checklist.")
SUMMARY = USER + " " + ANSWER
HIGH_USAGE = {"input_tokens": 150_001, "output_tokens": 3}
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def legacy_handoff_prompt(reason: str, max_chars: int) -> str:
    """The unchanged Looper control prompt, before API-specific summarization."""
    return (f"[HANDOFF] This session ends here ({reason}), and the next one cannot read this "
            "conversation. Write a compact handoff for it: what you and the user are working on in each chat, "
            "what you promised or are waiting for, the decisions and preferences the user gave, and open "
            f"questions. At most {max_chars} characters. Reply with the handoff only; the user does not "
            "see it.")


def score(summary: str) -> dict[str, Any]:
    """Score independent facts and explicit false claims about the pending deletion."""
    text = summary.lower()
    facts = {
        "constraint": bool(re.search(r"read.only", text)),
        "completed_evidence": "17" in text and "audit-17" in text,
        "pending_confirmation": ("delet" in text and "confirm" in text and
                                 bool(re.search(r"not (?:yet )?(?:run|executed|performed|done)|pending|await|unexecuted", text))),
        "promise": "rollback" in text and "checklist" in text,
        "open_question": "region" in text and bool(re.search(r"which|unresolved|unknown|undecided|open|question", text)),
    }
    invented = bool(re.search(r"(?:deleted|removed) (?:the |all )?(?:17|stale)|deletion (?:is |was |has been )?(?:complete|executed|performed)", text))
    return {"facts": facts, "fact_score": f"{sum(facts.values())}/5", "invented_action": invented,
            "outcome": "pass" if all(facts.values()) and not invented else "fail"}


def inputs(body: dict[str, Any]) -> dict[str, str]:
    """Translate the actual neutral message history, including the final handoff, to CLI text."""
    messages = []
    for item in body["input"]:
        assert item.get("type", "message") == "message", "synthetic conversation must contain only messages"
        content = item["content"]
        text = content if isinstance(content, str) else "".join(block["text"] for block in content)
        messages.append(f"[{item['role']}]\n{text}")
    return {"instructions": body["instructions"], "history": "\n\n".join(messages)}


def prompt_hash(value: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def context_switch(root: Path, monkeypatch: Any, summary: str, *, control: bool = False) -> dict[str, Any]:
    """Two production Manager turns on a fresh schema; the middle request is the handoff."""
    from looper.agents.tiers import manager as manager_module
    from looper.db import connection

    async with fresh_schema() as pool:
        await connection.ensure_schema(pool)
        with monkeypatch.context() as scoped:
            scoped.setattr(connection, "_pool", pool)
            if control:
                scoped.setattr(manager_module, "api_handoff_prompt", legacy_handoff_prompt)
            scripts = [ResponseScript(events=text_events(ANSWER, usage=HIGH_USAGE)),
                       ResponseScript(events=text_events(summary)),
                       ResponseScript(events=text_events("Ready to continue."))]
            async with _manager(pool, root, scoped, scripts) as chat:
                await chat.manager._handle(_message(USER, 0))
                await chat.manager._handle(_message("Continue from the saved facts.", 1))
                assert len(chat.model.requests) == 3
                handoff, resume = (request.body for request in chat.model.requests[1:])
                assert handoff["tools"] == []
                resume_text = inputs(resume)["history"]
                assert "[RESUME]" in resume_text and "context full:" in resume_text
                assert summary in resume_text
                return {"inputs": inputs(handoff), "resume": resume_text}


def real_available() -> str | None:
    if not shutil.which("claude"):
        return "Claude CLI is unavailable"
    if not any(os.environ.get(name) for name in AUTH_ENV) and not (
        sys.platform == "darwin" and (Path.home() / "Library" / "Keychains").is_dir()
    ):
        return "no macOS Claude keychain login path or explicit Claude credential environment"
    return None


async def summarize(root: Path, captured: dict[str, str], *, model: str, effort: str,
                    max_budget_usd: float, timeout: float = 180) -> dict[str, Any]:
    """Reuse the production launch, process cleanup and recorder with a temporary HOME."""
    root.mkdir(parents=True, exist_ok=True)
    prompt = root / "instructions.md"
    prompt.write_text(captured["instructions"])
    launch = builtin_launch(role=AgentType.MANAGER)
    launch = replace(launch, settings=replace(launch.settings, system_prompt_mode=SystemPromptMode.REPLACE,
                     max_budget_usd=max_budget_usd, profile=replace(launch.profile, effort=effort,
                                                                   agent_teams=False)))
    from tests.runtime_model_helpers import with_model_name
    launch = replace(launch, settings=replace(launch.settings, profile=with_model_name(launch.profile, model)))
    executable = install_claude(root / "bin", mode="real", real_claude=shutil.which("claude"))
    command = ADAPTERS["claude-code"].run_command(str(executable), cwd=root, prompt_file=prompt, launch=launch)
    settings_index = command.index("--settings") + 1
    settings = json.loads(command[settings_index])
    # This comparison measures only the captured inputs, without repository instructions.
    settings["claudeMdExcludes"] += [str(parent / name) for parent in (root.resolve(), *root.resolve().parents)
                                   for name in ("CLAUDE.md", "AGENTS.md", ".claude/rules/**")]
    command[settings_index] = json.dumps(settings)
    command += ["--tools", "", "--no-session-persistence"]
    home = root / "home"
    home.mkdir()
    keychains = Path.home() / "Library" / "Keychains"
    if sys.platform == "darwin" and keychains.is_dir():
        # Match evals/stop_realpath.py: share only the login keychain. Claude's
        # .claude and .claude.json remain inside the throwaway HOME.
        (home / "Library").mkdir()
        (home / "Library" / "Keychains").symlink_to(keychains)
    env = {name: os.environ[name] for name in ("PATH", "TMPDIR", *AUTH_ENV) if name in os.environ}
    env.update(HOME=str(home))
    session = claude_cli.ResidentClaude(label="synthetic-api-handoff")
    events: list[dict[str, Any]] = []
    recorder = claude_cli.ClaudeRunRecorder(agent_id="manager", task_id=None, command=command,
                                           cwd=str(root), input_text=captured["history"], launch=launch)
    try:
        async with recorder:
            async def record(event: dict[str, Any]) -> None:
                events.append(event)
                await recorder.record(event)
            await session.start(command, cwd=str(root), signature=1, env=env)
            turn = await asyncio.wait_for(session.run_turn(captured["history"], record), timeout)
            result = turn.result_event
            summary = claude_cli.answer_text(result, [])
            await recorder.finish(output_text=summary, result_event=result, usage=turn.usage)
            if not result or result.get("is_error") or not summary:
                raise TaskError("real Claude returned no completed summary", cost_usd=turn.usage.cost_usd or 0)
            return {"summary": summary, "cost_usd": turn.usage.cost_usd, "events": events}
    finally:
        await session.stop()
