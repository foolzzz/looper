"""Exact model identity, legacy context suffixes and runtime parameters."""

import json
import re
from pathlib import Path

import pytest

from looper.agents.agent_settings import MODEL_MAX_CHARS, SettingsError, model_name
from looper.agents.model_names import parse_model_name
from looper.agents.provider_presets import PRESETS, _without_context_suffix, PresetModel
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
from looper.agents.runtimes.pricing import BUILTIN_PRICES, base_model, pricer
from looper.agents.profile_registry import BUILTIN_PROFILE


PARAMETERIZED = "grok-4.7[context=500k,reasoning_effort=high,fast=true]"


@pytest.mark.parametrize("name", [
    PARAMETERIZED, "name[a=b]", "name[k_1=v-2.3,k.2=_]", "opus[1m]", "sonnet[200k]",
    "Exact.Model[1m]", "org/Model:20261020", "name[legacy:/value]", "a" * MODEL_MAX_CHARS,
])
def test_model_names_are_preserved(name):
    assert model_name(name) == name


@pytest.mark.parametrize("name", [
    "", " ", " name", "name ", "two words", "name\t", "name\n", "na\nme", 'name"', "name'",
    "name;", "name$", "name$(id)", "name`id`", "name(", "name)", "name|", "name&", "name<", "name>",
    "name[", "name[]", "name[a=b]x", "name[a b=c]", "name[a=]", "name[=b]", "name[a=b,]",
    "name[a=b,,c=d]", "name[a=b=c]", "name[a=b][c=d]", "name[[a=b]]", "name]", "-flag",
    "a" * (MODEL_MAX_CHARS + 1),
])
def test_invalid_model_names_keep_the_existing_error(name):
    with pytest.raises(SettingsError, match="is not a model name: use an alias such as opus or sonnet"):
        model_name(name)


def test_all_builtin_and_suggested_names_still_validate():
    names = set(BUILTIN_PRICES)
    for preset in PRESETS:
        for models in (preset.models, *(preset.models_for(api) for api in preset.apis)):
            for model in models:
                names.add(model.name)
                if model.background_model:
                    names.add(model.background_model)
    seeds = (Path(__file__).resolve().parents[1] / "looper/db/seeds.sql").read_text()
    names.update(re.findall(r"\('model-[^']+', 'prov-[^']+', '([^']+)'", seeds))
    assert "glm-5.3[1m]" in names and "grok-4.7" in names
    for fixture in Path(__file__).with_name("fixtures").glob("model_launch_parity_v2_*.json"):
        for captures in json.loads(fixture.read_text())["captures"].values():
            for capture in captures:
                names.add(capture["config"]["model"])
    for name in names:
        assert model_name(name) == name


@pytest.mark.parametrize("name", [PARAMETERIZED, "opus[context=500k]"])
def test_parameters_are_not_context_suffixes_or_claude_aliases(name):
    assert parse_model_name(name) == (name, None)
    assert ClaudeCodeRuntime().api_model(name) == name
    assert base_model(name) == name
    assert _without_context_suffix((PresetModel(name, 500000, name),)) == (PresetModel(name, 500000, name),)
    assert not pricer(BUILTIN_PROFILE.provider).model_price(name).complete
