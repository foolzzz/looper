"""An Agent Profile's agent teams against a real PostgreSQL: what the registry takes and what each role starts with.

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


def test_agent_teams_are_off_unless_set_and_only_a_claude_code_profile_takes_them(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,  # noqa: F811
) -> None:
    async def _test(_pool: Any) -> None:
        # An API key provider, which a Claude Code runtime and the stand-in runtime can both run with.
        key = (await _ok("POST", "/api/providers", {"name": "key", "protocols": ["anthropic-messages"],
                                                    "auth": "api_key"}))["provider"]["provider_id"]
        status, plain = await _profile("cc/plain")
        assert status == 201 and plain["profile"]["agent_teams"] is False
        status, teams = await _profile("cc/teams", provider_id=key, agent_teams=True)
        assert status == 201 and teams["profile"]["agent_teams"] is True
        listed = {p["name"]: p["agent_teams"] for p in (await _ok("GET", "/api/registry"))["profiles"]}
        assert listed == {"claude-code/opus": False, "cc/plain": False, "cc/teams": True}
        teams_id = teams["profile"]["profile_id"]
        assert (await _ok("PUT", f"/api/agent-profiles/{teams_id}", {"agent_teams": False}))["profile"][
            "agent_teams"] is False
        assert (await _ok("PUT", f"/api/agent-profiles/{teams_id}", {"agent_teams": True}))["profile"][
            "agent_teams"] is True

        # Another runtime type refuses them, at creation and when a profile moves to it with teams on.
        other = await _runtime(stand_in, name="si")
        status, payload = await _profile("si/teams", runtime_id=other, provider_id=key, agent_teams=True)
        assert status == 400 and "agent teams are a Claude Code feature" in payload["error"]
        status, payload = await _call("PUT", f"/api/agent-profiles/{teams_id}", {"runtime_id": other})
        assert status == 400 and "agent teams are a Claude Code feature" in payload["error"]
        moved = await _ok("PUT", f"/api/agent-profiles/{teams_id}", {"runtime_id": other, "agent_teams": False})
        assert (moved["profile"]["runtime_type"], moved["profile"]["agent_teams"]) == (stand_in, False)
        for value in ("yes", 1, None):
            status, payload = await _profile("cc/bad", agent_teams=value)
            assert status == 400 and "agent_teams must be true or false" in payload["error"]

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


def test_every_role_starts_with_agent_teams_only_when_its_profile_has_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # A server started from a Claude Code shell inherits the variable: only the profile decides.
    monkeypatch.setenv(TEAMS, "1")

    async def _test(_pool: Any) -> None:
        on = (await _profile("cc/teams", agent_teams=True))[1]["profile"]["profile_id"]
        off = (await _profile("cc/plain"))[1]["profile"]["profile_id"]
        project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]
        task = (await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                        "work_dir": str(tmp_path / "w")}))["task_id"]
        roles = ("manager", "planner", "worker", "reviewer")

        await _ok("PUT", "/api/agent-settings/system", {"profile_id": on})
        for role in roles:
            seen = await _seen(role, task, tmp_path)
            assert (_teammate_mode(seen["argv"]), seen["env"].get(TEAMS)) == ("in-process", "1"), role

        # Off, the variable is gone even when the server's environment or the settings' variables set it.
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": off, "env": [{"key": TEAMS, "value": "1"}]})
        for role in roles:
            seen = await _seen(role, task, tmp_path)
            assert (_teammate_mode(seen["argv"]), TEAMS in seen["env"]) == (None, False), role

    _run(_test, monkeypatch)
