"""A scripted task Manager case; protocol evidence, not a model-quality evaluation."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

CASE = "task_manager_status_and_requirement"
REQUIREMENT = "Build a greeting command; done means the command prints hello and its test passes."
DESCRIPTIONS = {
    "task_status": "Read this task now: its title, status and whether the background is working on it "
        "(`interaction_mode`), its requirement, its plan and steps with their results when it has run in the "
        "background, open user gates, the latest activity, and the workspace (directory, branch, changed and "
        "uncommitted files, HEAD). Use it when the user asks how the task is going.",
    "task_requirement_set": "Write a new version of this task's requirement: what it is for and what done looks "
        "like, in the user's words where you have them. Only while the task is a draft or ready; the old text is "
        "kept as a version. Use it when the user agrees on what the task is.",
}


async def run_case(rpc: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Script a status question, followed by an agreed requirement, through MCP JSON-RPC."""
    problems: list[str] = []
    listing = await rpc("tools/list", {})
    tools = {tool["name"]: tool for tool in listing.get("result", {}).get("tools", [])}
    if set(tools) != set(DESCRIPTIONS):
        problems.append("the direct task's tool catalog differs")
    for name, description in DESCRIPTIONS.items():
        tool = tools.get(name, {})
        schema = tool.get("inputSchema", {})
        if tool.get("description") != description:
            problems.append(f"{name} description differs")
        expected = {} if name == "task_status" else {
            "requirement": {"type": "string", "minLength": 1, "maxLength": 20000}}
        if schema.get("properties") != expected or schema.get("additionalProperties") is not False:
            problems.append(f"{name} argument contract differs")

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        reply = await rpc("tools/call", {"name": name, "arguments": arguments})
        return json.loads(reply["result"]["content"][0]["text"])

    before = await call("task_status", {})
    status = before.get("result", {})
    if before.get("status") != "ok" or status.get("interaction_mode") != "direct":
        problems.append("status did not report a direct task")
    if not str(status.get("requirement", "")).startswith("<untrusted"):
        problems.append("status requirement is not fenced")
    if set(status.get("workspace", {})) != {"dir", "mode", "branch", "head", "changed", "uncommitted"}:
        problems.append("workspace shape differs")
    write = await call("task_requirement_set", {"requirement": REQUIREMENT})
    if write.get("status") != "ok" or write.get("result", {}).get("requirement") != REQUIREMENT:
        problems.append("the agreed requirement was not written")
    after = await call("task_status", {})
    if REQUIREMENT not in str(after.get("result", {}).get("requirement", "")):
        problems.append("the new requirement was not read back")
    return {"case": CASE, "mode": "fake", "outcome": "fail" if problems else "pass", "problems": problems,
            "task_id": status.get("task_id"), "requirement": REQUIREMENT}
