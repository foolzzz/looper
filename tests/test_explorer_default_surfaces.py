"""Existing roles keep the pre-explorer runtime inputs, item by item.

``fixtures/explorer_default_surfaces.json`` freezes each argv token, MCP name, prompt block and config line recorded
before the explorer implementation (main 9e416f798214). A token, block, section or config entry added since is not
compared. A changed, reordered or removed recorded item fails.
"""
from dataclasses import replace
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, adapter
from tests.prompt_sections import assert_sections
from tests.runtime_model_helpers import builtin_launch, resolved_profile


BASELINE_SHA = "9e416f798214f86ca22097021ee20aa9bd7951ff"
# main before #1554 merged; update only with an intentional off-path change, and say why in the PR.


def surfaces(kind, role):
    runtime = adapter(kind)
    if kind == 'claude-code':
        launch = replace(builtin_launch(), role=AgentType(role))
    else:
        login = kind in ('coco', 'grok')
        protocol = ApiProtocol.ANTHROPIC_MESSAGES if kind == 'dsh' else ApiProtocol.OPENAI_RESPONSES
        provider = LegacyProviderSpec('account', 'account', AuthKind.OAUTH if login else AuthKind.API_KEY,
            frozenset({protocol}), oauth_runtime_type=kind if login else None,
            api_key=None if login else 'synthetic-key',
            base_url='' if login else 'https://endpoint.example.invalid/v1')
        profile = resolved_profile('profile', 'profile', RuntimeSpec('runtime', kind, kind,
            capabilities=runtime.capabilities), provider, 'deepseek-flash' if kind == 'dsh' else 'selected')
        launch = Launch(AgentSettings(profile=profile), role=AgentType(role))
    args = (runtime.launch_args(launch, cwd='/project') if kind == 'claude-code' else launch.args())
    result = {'argv': args, 'tools': list(runtime.loaded_sources(launch).mcp_servers),
        'prompts': [vars(block) for block in runtime.prompt_blocks(launch, 'Harness\nRole\nContract')]}
    if kind in ('codex', 'coco'):
        from looper.agents.runtimes.codex import CODEX, config_toml
        from looper.agents.runtimes.coco import COCO
        result['config'] = config_toml(launch, shell_home=Path('/shell'), home=COCO if kind == 'coco' else CODEX)
    elif kind == 'dsh':
        from looper.agents.runtimes.dsh import _launch_patch
        result['config'] = _launch_patch(launch, '/project')
    text = json.dumps(result, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return text.replace(str(Path(__file__).resolve().parents[1]), '<checkout>').encode()


_SURFACES = json.loads((Path(__file__).parent / "fixtures" / "explorer_default_surfaces.json").read_text())


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _in_order(expected, actual) -> None:
    """Every recorded item is still present, in the same order; items added around them are ignored."""
    at = 0
    for item in expected:
        try:
            found = actual.index(item, at)
        except ValueError:
            found = -1
        assert found >= 0, item
        at = found + 1


def _assert_config(frozen, current, label) -> None:
    kind = frozen["kind"]
    if kind == "none":
        return
    if kind == "lines":
        assert isinstance(current, str), label
        _in_order(frozen["lines"], current.splitlines())
        return
    if kind == "items":
        assert isinstance(current, list), label
        _in_order(frozen["items"], [_sha(item) for item in current])
        return
    assert kind == "keys", label
    assert isinstance(current, dict), label
    each = frozen["each"]
    assert [key for key in current if key in each] == frozen["order"], label
    assert {key: _sha(current[key]) if key in current else None for key in each} == each, label


def _assert_surface(kind, role) -> None:
    got = json.loads(surfaces(kind, role))
    frozen = _SURFACES["surfaces"][f"{kind}/{role}"]
    label = f"{kind}/{role}"
    _in_order(frozen["argv"], got["argv"])
    _in_order(frozen["tools"], got["tools"])
    blocks = got["prompts"]
    each = frozen["prompts"]["each"]
    assert [block["name"] for block in blocks if block["name"] in each] == frozen["prompts"]["order"], label
    for name, spec in each.items():
        block = next(item for item in blocks if item["name"] == name)
        assert block["owner"] == spec["owner"], name
        assert block["note"] == spec["note"], name
        assert_sections(block["text"] or "", spec["sections"], label=f"{label} {name}")
    _assert_config(frozen["config"], got.get("config"), label)


@pytest.mark.parametrize('kind', ['claude-code', 'codex', 'coco', 'omp', 'grok', 'pi', 'dsh'])
@pytest.mark.parametrize('role', ['manager', 'planner', 'worker'])
def test_existing_role_runtime_inputs_byte_identical(kind, role):
    _assert_surface(kind, role)


@pytest.mark.parametrize('role', ['manager', 'planner'])
def test_resident_process_signature_inputs_match_main(role):
    """Together with argv/prompt snapshots, pin the resident restart boundary to recorded main."""
    path = f'looper/agents/tiers/{role}.py'
    baseline = subprocess.run(['git', 'show', f'{BASELINE_SHA}:{path}'],
                              capture_output=True, check=True, text=True).stdout
    current = (Path(__file__).resolve().parents[1] / path).read_text()

    def signatures(source):
        return [ast.dump(node.value) for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'signature' for target in node.targets)]

    expected = signatures(baseline)
    assert expected, role
    _in_order(expected, signatures(current))


@pytest.mark.parametrize("probe", ["looper/_component_probe.py", "tests/fixtures/component_probe_v2_14_69.py"])
def test_component_probe_reads_the_unchanged_seat_tool_catalog(probe):
    from looper.agents.ops import room, vault
    from looper.agents.room import MemberKind

    root = Path(__file__).resolve().parents[1]
    paths = ('looper/agents/ops/__init__.py', 'looper/agents/room.py',
             'looper/agents/ops/room.py', 'looper/agents/ops/vault.py')
    completed = subprocess.run(
        [sys.executable, '-I', str(root / probe)],
        input=json.dumps({path: (root / path).read_text() for path in paths}),
        capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    expected = {role: [tool.listing() for tool in (*room.tools_for(kind), *vault.TOOLS)]
                for role, kind in (('worker', MemberKind.WORK), ('reviewer', MemberKind.REVIEW))}
    assert json.loads(completed.stdout) == expected
    read = next(tool for tool in expected['worker'] if tool['name'] == room.READ)
    _in_order(_SURFACES["read_kinds"], read['inputSchema']['properties']['kind']['enum'])
