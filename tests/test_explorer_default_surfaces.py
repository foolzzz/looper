"""Existing roles keep the exact pre-explorer runtime inputs.

Digests were captured from the pinned main revision before the explorer implementation. They
cover argv, generated configuration, reported catalog tools and prompt blocks.
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


# Captured from BASELINE_SHA; updating these requires an intentional existing-role change.
BASELINE = {
    "claude-code/manager": "4043e724214ec5ccf03576be7371e9eebdfe483e7b5c572f01baa2d53eba1f65",
    "claude-code/planner": "e3a8c00cca444fb0eea5a8a0e6c4e4e10a65faabc72fa512a280efa3ca1cc9da",
    "claude-code/worker": "4043e724214ec5ccf03576be7371e9eebdfe483e7b5c572f01baa2d53eba1f65",
    "codex/manager": "3daf2046cdd88a2c0937720cefaeb6b1a20152a6a532710fed938aa8d5933704",
    "codex/planner": "bed394815a4fd2500ae16322569c2949d94c116d397bdb5a01dceb08e224aad0",
    "codex/worker": "3daf2046cdd88a2c0937720cefaeb6b1a20152a6a532710fed938aa8d5933704",
    "coco/manager": "13bde977edc76c7c9b7ccb12a248b2616a3fbd3a73121e709cd6a58d67c47c73",
    "coco/planner": "76add1798c04d68b0d3301986bc5612dedbf3e1b10fc12d2f28eba3ca84c48ce",
    "coco/worker": "13bde977edc76c7c9b7ccb12a248b2616a3fbd3a73121e709cd6a58d67c47c73",
    "omp/manager": "ab7cb50b3d94726cdbdaadeec66780d5f0f4cf67733674b2c49d19a2d7c9e3f4",
    "omp/planner": "ec694ec8f0aea9fbe68d4a6f4962503103bade7f7d0bcb04f695fd25c974ae66",
    "omp/worker": "ab7cb50b3d94726cdbdaadeec66780d5f0f4cf67733674b2c49d19a2d7c9e3f4",
    "grok/manager": "b76b1d008a4377ce59592d80018b0e677a5d433132a0901d0605a718c7e57a2f",
    "grok/planner": "1fa1afa7a4f553903adcabcfe073befb7f28927720525648ddd0e73e788eefb2",
    "grok/worker": "b76b1d008a4377ce59592d80018b0e677a5d433132a0901d0605a718c7e57a2f",
    "pi/manager": "6732642d1f015ce45aee6df57271d0b46642046dc116c3ad4d5e88483c3eac08",
    "pi/planner": "a3dbbd21a6f15af36510717a2300d98b57fabede9d439b0d8adb84b07f17bca2",
    "pi/worker": "6732642d1f015ce45aee6df57271d0b46642046dc116c3ad4d5e88483c3eac08",
    "dsh/manager": "a10d56abc966c375f378d46d0e27a08f82150022d3548b3b72b278ecf9626a4d",
    "dsh/planner": "a10d56abc966c375f378d46d0e27a08f82150022d3548b3b72b278ecf9626a4d",
    "dsh/worker": "a10d56abc966c375f378d46d0e27a08f82150022d3548b3b72b278ecf9626a4d"
}


@pytest.mark.parametrize('kind', ['claude-code', 'codex', 'coco', 'omp', 'grok', 'pi', 'dsh'])
@pytest.mark.parametrize('role', ['manager', 'planner', 'worker'])
def test_existing_role_runtime_inputs_byte_identical(kind, role):
    assert hashlib.sha256(surfaces(kind, role)).hexdigest() == BASELINE[f'{kind}/{role}']


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
    assert expected and signatures(current) == expected


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
    assert read['inputSchema']['properties']['kind']['enum'] == [
        'info', 'question', 'answer', 'interface', 'announce', 'notice', 'escalation']
