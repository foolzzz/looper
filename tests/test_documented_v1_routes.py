"""Keep the migrated operator route families in living docs aligned with OpenAPI.

Historical plans/RFCs and unrelated API families are deliberately outside this check.
"""

import json
from pathlib import Path
import re

import pytest

from looper.agents import api_v1


ROOT = Path(__file__).resolve().parents[1]
SHARED = {
    "system": "status",
    "roles": "list,get,update,reset_prompt",
    "agent_settings": "get,update_system,update_role",
    "project_agent_settings": "get,update,update_role",
    "allowed_profiles": "get,replace",
    "profiles": "create,update,delete,prompt_preview",
}
ARCHITECTURE = {
    **SHARED,
    "role_templates": "list,put,delete",
    "project_roles": "stats",
    "ops/gates": "list,messages,reply,end",
    "tasks": "role_update,room_view",
    "waits": "resume,cancel",
    "clients": "list,create,revoke",
}
DOCUMENTS = {
    "README.md": SHARED,
    "README.zh-CN.md": SHARED,
    "docs/design.md": {"system": "status", "profiles": SHARED["profiles"]},
    "docs/architecture/agent-system.md": ARCHITECTURE,
}
LEGACY = re.compile(
    r"/api/(?:status\b|agent-settings\b|roles\b|agent-profiles\b|clients\b|gates\b"
    r"|projects/[^/\s`]+/(?:agent-settings|allowed-profiles|role-templates|role-stats)\b"
    r"|tasks/[^/\s`]+/(?:gates|roles|waits|room)\b)"
)
REFERENCE = re.compile(r"`(?:(GET|POST|PUT|DELETE)\s+)?(/api/v1/[^`\s]+)`")
LIVING_SECTIONS = {
    "HTTP API", "4. Run as a service", "4. 作为服务常驻", "Web", "AgentSupervisor",
    "The supervisor: restarting resident agents", "Runtimes, LLM providers and Agent Profiles",
    "Step workspaces", "A task's Planner's tools", "Roles a task's Planner defines", "User gates",
    "A task's agent room", "Migrated settings and interaction requests", "The public API (v1)",
}


def _living_text(filename):
    # Check current operator explanations, not historical design/progress elsewhere.
    sections = re.split(r"(?m)^#{2,3} (.+)\n", (ROOT / filename).read_text())
    return "\n".join(body for title, body in zip(sections[1::2], sections[2::2])
                     if title in LIVING_SECTIONS)


def _paths(families):
    return {f"/api/v1/{family}/{action}"
            for family, actions in families.items() for action in actions.split(",")}


def _claims(text):
    claims = []
    for line in text.splitlines():
        for match in REFERENCE.finditer(line):
            method, path = match.groups()
            if method is None and line.startswith("| "):
                method = line.split("|")[2].strip()
            group = re.search(r"\{([^}]+)\}", path)
            paths = ([path[:group.start()] + action + path[group.end():]
                      for action in group[1].split(",")] if group else [path])
            claims.extend((method, path) for path in paths)
    return claims


@pytest.mark.parametrize("filename,families", DOCUMENTS.items())
def test_living_migrated_routes_match_registered_post_operations(filename, families):
    text = _living_text(filename)
    legacy = LEGACY.search(text)
    assert legacy is None, f"removed live route in {filename}: {legacy[0]}"
    required = _paths(families)
    affected = _paths(ARCHITECTURE)
    claims = [(method, path) for method, path in _claims(text)
              if path in affected or any(path.startswith(f"/api/v1/{family}/")
                                        for family in ARCHITECTURE if family not in ("system", "tasks"))]
    assert required <= {path for _, path in claims}
    document = api_v1.document()
    assert document["servers"] == [{"url": "/api/v1"}]
    for method, path in claims:
        assert path in affected, f"unexpected migrated operation: {path}"
        assert method == "POST", (filename, method, path)
        operation = document["paths"][path.removeprefix("/api/v1")]["post"]
        responses = operation["responses"]
        assert responses["200"]["content"]["application/json"]["schema"]["allOf"][0] == {
            "$ref": "#/components/schemas/OpOk"}
        assert responses["default"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/OpError"}


def test_readme_route_tables_agree():
    tables = [(ROOT / name).read_text().split("### HTTP API\n", 1)[1].split("\n## ", 1)[0]
              for name in ("README.md", "README.zh-CN.md")]
    assert [claim for claim in _claims(tables[0]) if claim[1] in _paths(SHARED)] == [
        claim for claim in _claims(tables[1]) if claim[1] in _paths(SHARED)]


def test_documented_json_requests_match_current_schemas():
    text = (ROOT / "docs/architecture/agent-system.md").read_text()
    examples = re.findall(r"`POST (/api/v1/[^`]+)`:\n\n```json\n(.*?)\n```", text, re.S)
    examples = [(path, body) for path, body in examples if path in _paths(ARCHITECTURE)]
    assert len(examples) >= 7, "keep examples for the migrated request shapes"
    for path, body in examples:
        operation = api_v1.find("POST", path)
        assert api_v1.validate_arguments(operation.request, json.loads(body)) == json.loads(body)
