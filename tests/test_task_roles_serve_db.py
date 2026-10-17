"""Roles a task's Planner defines over MCP, on a real ``looper serve`` with a stand-in ``claude``.

One disposable ``looper serve`` on a new database runs one task per variant, each in a project of its own. The
stand-in Planner starts the ``looper`` MCP server from its ``--mcp-config`` and calls its tools in its [PLAN] turn:

- ``roles``: the project lets its Planner define roles and allows ``claude-code/sonnet``. The Planner defines
  ``tester`` (work, on sonnet, with a harness of its own) and ``security`` (review), and plans ``a`` (reviewed, as
  tester, reviewed by security too) and ``b`` (reviewed, built-in only). Security fails ``a``'s first attempt; the
  Planner rewrites tester and retries; then both reviews pass;
- ``off``: the project's switch is off: no role tools, a definition is refused, and a step naming a role gets a
  repair turn;
- ``restart``: ``a`` runs as tester and fails, the Planner fails the task, and the user starts it again (going on
  from its plan): its roles stay and ``a`` runs as tester again;
- ``startover``: the same failed task started over: its roles are gone, and the new plan has none.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database (the run creates and drops a database
beside it).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression.runner import fresh_database, running_server
from looper.agents.agent_settings import HARNESS_BOUNDARIES, WORKING_STYLE
from looper.agents.ops.planner import ROLES_GUIDE
from looper.agents.system_prompt import BOUNDARIES_LEAD, REVIEW_CONTRACT, WORK_CONTRACT

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

VARIANTS = ("roles", "off", "restart", "startover")
TESTER = "TESTER-ROLE: you write the tests of the step's code."
TESTER_V2 = "TESTER-ROLE: you write the tests first, the injection case included."
TESTER_HARNESS = "## How you work\nTESTER-HARNESS: test first, then report."
SECURITY = "SECURITY-ROLE: you check the step's code for security problems."

_FAKE_CLAUDE = r'''
import json, os, re, subprocess, sys
STATE = sys.argv[1]
args = sys.argv[2:]
if args[:1] in (["--version"], ["auth"]):  # the server's startup self-check, not an agent run
    sys.exit(0)
prompt = ""
if "--append-system-prompt-file" in args:
    prompt = open(args[args.index("--append-system-prompt-file") + 1], encoding="utf-8").read()
variant = re.search(r"variant=(\w+)", prompt).group(1)
TESTER, TESTER_V2, TESTER_HARNESS, SECURITY = __ROLE_TEXTS__

def log(name, value):
    with open(os.path.join(STATE, name), "a") as f:
        f.write(json.dumps({"variant": variant, **value}) + "\n")

def logged(name):
    path = os.path.join(STATE, name)
    return [r for r in (json.loads(line) for line in open(path)) if r["variant"] == variant] \
        if os.path.exists(path) else []

def answer(session, text):
    for event in ({"type": "system", "subtype": "init", "session_id": session, "model": "fake"},
                  {"type": "assistant", "session_id": session,
                   "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
                  {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session,
                   "total_cost_usd": 0.001, "usage": {"input_tokens": 10, "output_tokens": 10}}):
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

def spec(step_id, review=False, **roles):
    return {"id": step_id, "instruction": f"do {step_id}", "acceptance_criteria": [f"{step_id} works"],
            "depends_on": [], "review": review, **roles}

work = re.search(r"^Your step: (\S+)$", prompt, re.MULTILINE)
review = re.search(r"^The step under review: (\S+)$", prompt, re.MULTILINE)
if work or review:
    sys.stdin.readline()
    label = (work or review).group(1)
    who = "security" if SECURITY in prompt else "tester" if TESTER[:12] in prompt else "builtin"
    kind = "review" if review else "work"
    earlier = [r for r in logged("runs.jsonl") if (r["kind"], r["step"], r["who"]) == (kind, label, who)]
    first_work = not [r for r in logged("runs.jsonl") if r["kind"] == "work"]
    log("runs.jsonl", {"kind": kind, "step": label, "who": who, "model": args[args.index("--model") + 1],
                       "prompt": prompt})
    if review:
        passed = not (who == "security" and label == "a" and not earlier)
        verdict = {"passed": passed, "findings": [] if passed else ["the input reaches the shell"],
                   "summary": "checked"}
        answer("fake-reviewer", "Checked.\n" + json.dumps(verdict))
    else:
        failed = variant in ("restart", "startover") and first_work  # their first Worker fails
        report = {"status": "failed" if failed else "completed", "changed_files": {}, "deleted_files": [],
                  "summary": f"{label} {'failed' if failed else 'done'}"}
        answer("fake-worker", "Done.\n" + json.dumps(report))
    sys.exit(0)

config = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["looper"]
server = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          env={**os.environ, **config["env"]}, text=True)
ids = iter(range(1, 10000))

def rpc(method, params):
    server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params}) + "\n")
    server.stdin.flush()
    return json.loads(server.stdout.readline())

def call(name, arguments):
    result = json.loads(rpc("tools/call", {"name": name, "arguments": arguments})["result"]["content"][0]["text"])
    log("calls.jsonl", {"tool": name, "arguments": arguments, "answer": result})
    return result

rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "stand-in"}})
log("planner.jsonl", {"tools": [t["name"] for t in rpc("tools/list", {})["result"]["tools"]], "prompt": prompt})
FINAL = {"delivered": "the steps", "how_to_use": "run it", "verification": "each check passed", "open_issues": "none"}
for line in sys.stdin:
    if not line.strip():
        continue
    content = json.loads(line)["message"]["content"]
    text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content)
    log("turns.jsonl", {"text": text})
    plans = [t for t in logged("turns.jsonl") if t["text"].startswith("[PLAN] Plan the task")]
    if "Your last reply broke the reply contract" in text:
        reply = {"steps": [spec("a")]}
    elif text.startswith("[PLAN]") and variant == "off":
        call("looper_task_role_set", {"id": "tester", "kind": "work", "role_prompt": TESTER})
        reply = {"steps": [spec("a", role="tester")]}
    elif text.startswith("[PLAN]") and variant == "startover" and len(plans) > 1:
        call("looper_task_roles", {})
        reply = {"steps": [spec("a")]}
    elif text.startswith("[PLAN]"):
        call("looper_task_role_set", {"id": "worker", "kind": "work", "role_prompt": TESTER})  # refused
        call("looper_task_role_set", {"id": "tester", "kind": "work", "role_prompt": TESTER,
                                      "harness": TESTER_HARNESS, "profile_id": "claude-code/sonnet",
                                      "items": ["role-notes"], "reason": "the tests need their own author"})
        if variant == "roles":
            call("looper_task_role_set", {"id": "security", "kind": "review", "role_prompt": SECURITY})
            call("looper_task_roles", {})
            reply = {"steps": [spec("a", True, role="tester", review_roles=["security"]), spec("b", True)]}
        else:
            reply = {"steps": [spec("a", role="tester")]}
    elif text.startswith("[RESTART]"):
        reply = {"reply": "going on", "action": "none"}
    elif text.startswith("[DECISION]"):
        step = re.search(r"^\[DECISION\] Step (\S+)", text).group(1)
        final = "the task's last open step" in text
        if "status: failed" in text:
            reply = {"decision": "fail", "reason": f"{step} failed"}
        elif "needs a passing review" in text:
            reply = {"decision": "review"}
        elif "- passed: no" in text:
            call("looper_task_role_set", {"id": "tester", "kind": "work", "role_prompt": TESTER_V2,
                                          "harness": TESTER_HARNESS, "profile_id": "claude-code/sonnet",
                                          "reason": "cover the injection case"})
            reply = {"decision": "retry", "feedback": "keep the input out of the shell"}
        else:
            reply = {"decision": "accept", "summary": "done", **({"final": FINAL} if final else {})}
    else:
        reply = {"reviews": []}
    answer(f"fake-planner-{variant}", "Done.\n\n```json\n" + json.dumps(reply) + "\n```")
'''.replace("__ROLE_TEXTS__", repr((TESTER, TESTER_V2, TESTER_HARNESS, SECURITY)))


def _install(root: Path) -> tuple[Path, Path]:
    state = root / "state"
    state.mkdir()
    (root / "fake_claude.py").write_text(_FAKE_CLAUDE, encoding="utf-8")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(root / 'fake_claude.py'))} "
                      f"{shlex.quote(str(state))} \"$@\"\n")
    claude.chmod(0o755)
    return bin_dir, state


def _log(state: Path, name: str, variant: str) -> list[dict[str, Any]]:
    path = state / name
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row["variant"] == variant]


def _wait(api: Any, ids: dict[str, str], variants: tuple[str, ...], ended: tuple[str, ...]) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while True:
        details = {variant: api.get(f"/api/tasks/{ids[variant]}") for variant in variants}
        if all(d["status"] in ended for d in details.values()):
            return details
        assert time.monotonic() < deadline, {v: d["status"] for v, d in details.items()}
        time.sleep(0.2)


async def _layers(database_url: str, task_ids: list[str]) -> list[dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch("SELECT task_id, agent_id, metadata FROM llm_records WHERE task_id = ANY($1::text[]) "
                                "AND (agent_id LIKE 'worker-%' OR agent_id LIKE 'reviewer-%') ORDER BY id", task_ids)
    finally:
        await conn.close()
    return [{"task_id": r["task_id"], "agent_id": r["agent_id"], **json.loads(r["metadata"])} for r in rows]


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Every variant's task once it ended (``restart`` and ``startover`` after they were started again), with what
    the stand-in logged and the run records."""
    root = tmp_path_factory.mktemp("task-roles").resolve()
    bin_dir, state = _install(root)
    runtime, home = root / "runtime", root / "home"
    for path in (runtime, home):
        path.mkdir()
    with fresh_database(str(DSN)) as (database_url, _):
        server = {"database_url": database_url, "runtime_root": runtime, "bin_dir": bin_dir, "home": home}
        with running_server(**server, log_path=root / "serve.log") as api:
            sonnet = api.post("/api/agent-profiles", {"name": "claude-code/sonnet", "runtime_id": "rt-claude-code",
                                                      "provider_id": "prov-claude-subscription", "model": "sonnet"})
            # A skill installed for the roles a Planner defines only: it loads for a run of a role naming it.
            notes = api.post("/api/catalog/skills", {"name": "role-notes", "description": "Notes for roles.",
                                                     "content": "Reply ROLE-NOTES.\n"})["item"]
            api.post("/api/installs", {"item_id": notes["item_id"], "scope": "global", "roles": ["task_role"]})
            ids: dict[str, str] = {}
            for variant in VARIANTS:
                work = root / f"work-{variant}"
                work.mkdir()
                project = api.post("/api/projects", {"name": f"calc-{variant}", "work_dir": str(work)})
                if variant != "off":
                    api.put(f"/api/projects/{project['project_id']}", {"planner_roles": "on"})
                    api.put(f"/api/projects/{project['project_id']}/allowed-profiles",
                            {"profile_ids": [sonnet["profile"]["profile_id"]]})
                ids[variant] = api.post("/api/tasks/create", {"requirement": f"build calc variant={variant}",
                                                              "project_id": project["project_id"]})["task_id"]
                api.post("/api/tasks/start", {"task_id": ids[variant]})
            first = _wait(api, ids, VARIANTS, ("completed", "failed", "stopped", "cancelled"))
            before = {variant: first[variant]["roles"] for variant in ("restart", "startover")}
            # Where the dashboard lists runs: the Runs page and a History page (their records), and the Chat page.
            listed = {"records": {variant: api.get("/api/llm-records", task_id=ids[variant], limit=500)["records"]
                                  for variant in ("roles", "off")},
                      "targets": api.get("/api/chat/targets")["targets"]}
            api.post("/api/tasks/start", {"task_id": ids["restart"]})
            api.post("/api/tasks/start", {"task_id": ids["startover"], "start_over": True})
            time.sleep(0.5)
            again = _wait(api, ids, ("restart", "startover"), ("completed", "failed", "stopped", "cancelled"))
        layers = asyncio.run(_layers(database_url, list(ids.values())))
    return {"first": first, "again": again, "before": before, "ids": ids, "state": state, "layers": layers,
            "listed": listed, "log": (root / "serve.log").read_text()}


def _calls(ran: dict[str, Any], variant: str) -> list[dict[str, Any]]:
    return _log(ran["state"], "calls.jsonl", variant)


def _runs(ran: dict[str, Any], variant: str) -> list[tuple[str, str, str, str]]:
    return [(r["kind"], r["step"], r["who"], r["model"]) for r in _log(ran["state"], "runs.jsonl", variant)]


def test_the_planner_defines_roles_with_its_tools_and_a_refused_definition_changes_nothing(ran: dict[str, Any]) -> None:
    detail = ran["first"]["roles"]
    assert detail["status"] == "completed", ran["log"][-3000:]
    listed = _log(ran["state"], "planner.jsonl", "roles")[0]
    assert listed["tools"][-2:] == ["looper_task_roles", "looper_task_role_set"]
    assert "looper_task_role_set" in listed["prompt"] and ROLES_GUIDE in listed["prompt"]
    assert '{"role": "<id>", "required": false} (its verdict is advice to\n  you)' in listed["prompt"]
    answers = [(c["arguments"].get("id"), c["answer"]["status"]) for c in _calls(ran, "roles")
               if c["tool"] == "looper_task_role_set"]
    assert answers == [("worker", "error"), ("tester", "ok"), ("security", "ok"), ("tester", "ok")]
    assert [(r["id"], r["kind"], r["version"], r["profile"] and r["profile"]["name"], r["steps"])
            for r in detail["roles"]] == [("tester", "work", 2, "claude-code/sonnet", ["a"]),
                                          ("security", "review", 1, None, ["a"])]
    assert [e["kind"] for e in detail["journal"]].count("role_defined") == 3


def test_a_role_step_runs_as_its_role_and_needs_both_reviews(ran: dict[str, Any]) -> None:
    runs = [r for r in _runs(ran, "roles") if r[1] == "a"]
    # tester on sonnet; the built-in Reviewer first, then security, which fails the first attempt; then again.
    assert runs == [("work", "a", "tester", "sonnet"), ("review", "a", "builtin", "opus"),
                    ("review", "a", "security", "opus"), ("work", "a", "tester", "sonnet"),
                    ("review", "a", "builtin", "opus"), ("review", "a", "security", "opus")]
    assert [r for r in _runs(ran, "roles") if r[1] == "b"] == [("work", "b", "builtin", "opus"),
                                                               ("review", "b", "builtin", "opus")]
    steps = {s["step_key"]: s for s in ran["first"]["roles"]["steps"]}
    assert (steps["a"]["status"], steps["a"]["retries"], steps["a"]["role_version"]) == ("verified", 1, 2)
    stats = {r["id"]: r["stats"] for r in ran["first"]["roles"]["roles"]}
    assert (stats["tester"]["runs"], stats["tester"]["contract_violations"]) == (2, 0)
    assert stats["security"]["reviews"] == {"passed": 1, "not_passed": 1}
    assert ran["first"]["roles"]["builtin_role_stats"]["reviewer"]["runs"] == 3


def test_every_role_run_ends_with_the_boundaries_and_a_built_in_run_is_as_before(ran: dict[str, Any]) -> None:
    boundaries = f"## Boundaries\n{BOUNDARIES_LEAD}\n{HARNESS_BOUNDARIES}"
    prompts = {(r["kind"], r["step"], r["who"]): r["prompt"] for r in _log(ran["state"], "runs.jsonl", "roles")}
    tester = [r["prompt"] for r in _log(ran["state"], "runs.jsonl", "roles") if r["who"] == "tester"]
    assert TESTER in tester[0] and TESTER_V2 in tester[1]  # the retry ran the version current when it started
    for prompt, contract in ((tester[1], WORK_CONTRACT), (prompts[("review", "a", "security")], REVIEW_CONTRACT)):
        assert prompt.count("## Boundaries") == 1
        assert prompt.index("## Task") < prompt.index(boundaries) < prompt.index(contract)
        assert prompt.endswith(contract)
    assert tester[1].startswith(TESTER_HARNESS) and WORKING_STYLE not in tester[1]
    assert prompts[("review", "a", "security")].startswith(WORKING_STYLE)
    # Its role's checks decide the review role's verdict too; the built-in Reviewer is told nothing of the kind.
    assert "You review it as the role security:" in prompts[("review", "a", "security")]
    assert "You review it as the role" not in prompts[("review", "a", "builtin")]
    for key in (("work", "b", "builtin"), ("review", "b", "builtin")):
        assert prompts[key].startswith("## How you work") and BOUNDARIES_LEAD not in prompts[key]
    # Each run records the role and the version it ran as, and where its boundaries came from.
    roles_task = ran["ids"]["roles"]
    recorded = [(m["prompt_layers"].get("task_role") or {}).get("version") for m in ran["layers"]
                if m["task_id"] == roles_task and m["agent_id"].startswith("worker-")]
    assert sorted(recorded, key=lambda v: v or 0) == [None, 1, 2]
    role_runs = [m for m in ran["layers"] if m["task_id"] == roles_task and m["prompt_layers"].get("task_role")]
    assert {m["prompt_layers"]["boundaries"]["source"] for m in role_runs} == {"builtin"}
    assert {m["outcome"] for m in ran["layers"] if m["task_id"] == roles_task} == {"completed", "passed", "not_passed"}


def test_a_role_run_loads_the_items_its_role_names_and_no_other_run_does(ran: dict[str, Any]) -> None:
    roles_task = ran["ids"]["roles"]
    loaded = {(m["agent_id"], (m["prompt_layers"].get("task_role") or {}).get("version")):
              [item["name"] for item in (m.get("library") or {}).get("items") or []]
              for m in ran["layers"] if m["task_id"] == roles_task and m["agent_id"].startswith("worker-")}
    by_version = {version: names for (_, version), names in loaded.items()}
    # tester v1 names the skill; v2 (the Planner's rewrite) names none, and the built-in Worker never loads it.
    assert by_version == {1: ["role-notes"], 2: [], None: []}
    read = next(c["answer"]["result"] for c in _calls(ran, "roles") if c["tool"] == "looper_task_roles")
    assert [item["name"] for item in read["role_items"]] == ["role-notes"]
    assert read["roles"][0]["items"]  # tester v1, by the item's id


def test_a_run_as_a_role_is_named_by_it_wherever_runs_are_listed_and_a_built_in_run_as_before(
    ran: dict[str, Any],
) -> None:
    records = ran["listed"]["records"]["roles"]
    step_runs = [r for r in records if r["agent_id"].startswith(("worker-", "reviewer-"))]
    recorded = {r["agent_id"]: r["metadata"]["prompt_layers"].get("task_role") for r in step_runs}
    named = {r["agent_id"]: r["task_role"] for r in step_runs if "task_role" in r}
    # Each run as a role names the role and the version it recorded; tester ran twice (v1, then v2), security
    # reviewed twice, and the built-in runs (Planner, Worker, Reviewer) have no task_role at all.
    assert named == {agent: {key: role[key] for key in ("id", "kind", "version")}
                     for agent, role in recorded.items() if role}
    assert sorted((r["id"], r["kind"], r["version"]) for r in named.values()) == [
        ("security", "review", 1), ("security", "review", 1), ("tester", "work", 1), ("tester", "work", 2)]
    assert len(step_runs) > len(named) and all("task_role" not in r for r in records if r["agent_id"] not in named)
    # The task page's Agent Timeline reads the same names by agent id; a task without role runs gets none.
    assert ran["first"]["roles"]["run_roles"] == named
    assert "run_roles" not in ran["first"]["off"]
    assert all("task_role" not in r for r in ran["listed"]["records"]["off"])
    # The Chat page's step agents: a role run names its role, a built-in one is as before.
    targets = {t["id"]: t for t in ran["listed"]["targets"] if t.get("task_id") in (ran["ids"]["roles"],
                                                                                   ran["ids"]["off"])}
    assert {agent: t["task_role"] for agent, t in targets.items() if "task_role" in t} == named
    assert {t["kind"] for t in targets.values() if "task_role" not in t} == {"planner", "worker", "reviewer"}


def test_where_the_switch_is_off_the_planner_has_no_role_tools_and_no_step_runs_as_a_role(ran: dict[str, Any]) -> None:
    detail = ran["first"]["off"]
    assert detail["status"] == "completed" and detail["roles"] == []
    listed = _log(ran["state"], "planner.jsonl", "off")[0]
    assert "looper_task_role_set" not in listed["tools"] and "review_roles" not in listed["prompt"]
    assert "define one work role per discipline" not in listed["prompt"]  # the roles guide: only with roles on
    (refused,) = _calls(ran, "off")
    assert refused["answer"]["error"]["code"] == "forbidden"
    turns = [t["text"] for t in _log(ran["state"], "turns.jsonl", "off")]
    assert "planner_roles is off" in turns[1]
    assert _runs(ran, "off") == [("work", "a", "builtin", "opus")]


def test_going_on_from_the_plan_keeps_the_roles_and_starting_over_drops_them(ran: dict[str, Any]) -> None:
    assert ran["first"]["restart"]["status"] == ran["first"]["startover"]["status"] == "failed"
    assert [r["id"] for r in ran["before"]["restart"]] == [r["id"] for r in ran["before"]["startover"]] == ["tester"]
    assert ran["again"]["restart"]["status"] == "completed"
    assert [r["id"] for r in ran["again"]["restart"]["roles"]] == ["tester"]
    assert _runs(ran, "restart") == [("work", "a", "tester", "sonnet"), ("work", "a", "tester", "sonnet")]
    assert ran["again"]["startover"]["status"] == "completed" and ran["again"]["startover"]["roles"] == []
    assert _runs(ran, "startover") == [("work", "a", "tester", "sonnet"), ("work", "a", "builtin", "opus")]
