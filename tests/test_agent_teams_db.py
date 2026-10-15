"""An Agent Profile's agent teams against a real PostgreSQL: their default, what the registry takes, the upgrade and
what each role starts with.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own. Runs use stand-in ``claude``
executables that write down what they were started with.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import agent_launch_parity as parity
from fake_claude import fake_claude
from tests.test_catalog_db import DSN
from tests.test_profile_registry_db import _call, _launch, _ok, _run, _runtime, stand_in  # noqa: F401

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TEAMS = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"


async def _profile(name: str, **fields: Any) -> tuple[int, Any]:
    return await _call("POST", "/api/agent-profiles", {"name": name, "runtime_id": "rt-claude-code",
                                                       "provider_id": "prov-claude-subscription", "model": "sonnet",
                                                       **fields})


def test_agent_teams_are_on_by_default_for_claude_code_and_only_claude_code_takes_them(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,  # noqa: F811
) -> None:
    async def _test(_pool: Any) -> None:
        # An API key provider, which a Claude Code runtime and the stand-in runtime can both run with.
        key = (await _ok("POST", "/api/providers", {"name": "key", "protocols": ["anthropic-messages"],
                                                    "auth": "api_key"}))["provider"]["provider_id"]
        other = await _runtime(stand_in, name="si")
        created = {name: (await _ok("POST", "/api/agent-profiles", {
            "name": name, "runtime_id": runtime, "provider_id": key, "model": "sonnet", **fields}))["profile"]
            for name, runtime, fields in [("cc/default", "rt-claude-code", {}),
                                          ("cc/off", "rt-claude-code", {"agent_teams": False}),
                                          ("si/default", other, {})]}
        listed = {p["name"]: p["agent_teams"] for p in (await _ok("GET", "/api/registry"))["profiles"]}
        assert listed == {"claude-code/opus": True, "cc/default": True, "cc/off": False, "si/default": False}

        off = created["cc/off"]["profile_id"]
        assert (await _ok("PUT", f"/api/agent-profiles/{off}", {"agent_teams": True}))["profile"]["agent_teams"]
        await _ok("PUT", f"/api/agent-profiles/{off}", {"agent_teams": False})
        assert (await _ok("PUT", f"/api/agent-profiles/{off}", {"model": "opus"}))["profile"]["agent_teams"] is False

        # Another runtime type refuses them when asked; a move that does not say takes the new type's default.
        status, payload = await _profile("si/teams", runtime_id=other, provider_id=key, agent_teams=True)
        assert status == 400 and "agent teams are a Claude Code feature" in payload["error"]
        on = created["cc/default"]["profile_id"]
        status, payload = await _call("PUT", f"/api/agent-profiles/{on}", {"runtime_id": other, "agent_teams": True})
        assert status == 400 and "agent teams are a Claude Code feature" in payload["error"]
        moved = (await _ok("PUT", f"/api/agent-profiles/{on}", {"runtime_id": other}))["profile"]
        assert (moved["runtime_type"], moved["agent_teams"]) == (stand_in, False)
        back = (await _ok("PUT", f"/api/agent-profiles/{on}", {"runtime_id": "rt-claude-code"}))["profile"]
        assert (back["runtime_type"], back["agent_teams"]) == ("claude-code", True)
        for value in ("yes", 1, None):
            status, payload = await _profile("cc/bad", agent_teams=value)
            assert status == 400 and "agent_teams must be true or false" in payload["error"]

    _run(_test, monkeypatch)


def test_the_upgrade_turns_agent_teams_on_once_for_every_claude_code_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        await _runtime("codex")
        await pool.execute("INSERT INTO llm_providers (id, name, protocols, auth) "
                           "VALUES ('prov-cpa', 'cpa', ARRAY['openai-responses'], 'api_key')")
        # A database from before agent teams, with a profile on each runtime type.
        await pool.execute("ALTER TABLE agent_profiles DROP COLUMN agent_teams; "
                           "DELETE FROM schema_migrations WHERE name = 'agent_profiles_agent_teams_on'")
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                           "('agent-cc', 'cc/sonnet', 'rt-claude-code', 'prov-claude-subscription', 'sonnet'), "
                           "('agent-cx', 'codex/gpt', 'rt-codex', 'prov-cpa', 'gpt-5.5')")

        async def teams() -> dict[str, bool]:
            rows = await pool.fetch("SELECT name, agent_teams FROM agent_profiles")
            return {r["name"]: r["agent_teams"] for r in rows}

        await ensure_schema(pool)
        assert await teams() == {"claude-code/opus": True, "cc/sonnet": True, "codex/gpt": False}
        # Turned off since: a later start leaves it off.
        await pool.execute("UPDATE agent_profiles SET agent_teams = false WHERE id = 'agent-cc'")
        await ensure_schema(pool)
        assert await teams() == {"claude-code/opus": True, "cc/sonnet": False, "codex/gpt": False}

    _run(_test, monkeypatch)


async def _seen(role: str, task_id: str, root: Path) -> dict[str, Any]:
    """What ``role`` started its stand-in claude with (``argv``, ``env``), run the way the server runs it."""
    if role != "manager":
        return await _launch(role, task_id, root / "w", root)
    out = root / "manager.json"
    await parity._manager(fake_claude(root / "claude-manager", parity._CLAUDE.format(out=str(out))), root)
    return json.loads(out.read_text())


def _teammate_mode(argv: list[str]) -> str | None:
    return argv[argv.index("--teammate-mode") + 1] if "--teammate-mode" in argv else None


def test_the_manager_workers_and_reviewers_start_with_agent_teams_only_when_their_profile_has_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # A server started from a Claude Code shell inherits the variable: only the profile decides.
    monkeypatch.setenv(TEAMS, "1")

    async def _test(_pool: Any) -> None:
        on = (await _profile("cc/default"))[1]["profile"]["profile_id"]  # on by default
        off = (await _profile("cc/off", agent_teams=False))[1]["profile"]["profile_id"]
        project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]
        task = (await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                        "work_dir": str(tmp_path / "w")}))["task_id"]
        roles = ("manager", "planner", "worker", "reviewer")

        await _ok("PUT", "/api/agent-settings/system", {"profile_id": on})
        for role in roles:
            seen = await _seen(role, task, tmp_path)
            # A Planner never has them: its decisions are read from its own turn's result.
            expected = (None, False) if role == "planner" else ("in-process", True)
            assert (_teammate_mode(seen["argv"]), seen["env"].get(TEAMS) == "1") == expected, role
            assert role != "planner" or TEAMS not in seen["env"]

        # Off, the variable is gone even when the server's environment or the settings' variables set it.
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": off, "env": [{"key": TEAMS, "value": "1"}]})
        for role in roles:
            seen = await _seen(role, task, tmp_path)
            assert (_teammate_mode(seen["argv"]), TEAMS in seen["env"]) == (None, False), role

    _run(_test, monkeypatch)
