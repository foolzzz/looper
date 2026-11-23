"""manager.persona: field limits, the pinned section, and how a one-line edit rides the stable block."""

from __future__ import annotations

import asyncio
import json

import pytest

from looper.agents import persona
from looper.agents.action_levels import ActionLevel
from looper.agents.persona import HEADING, LIMITS, Persona, checked, section
from looper.agents.tiers.state_view import StateShown, StateView, render, rev_of


def _block(value: Persona) -> list[str]:
    return [*section(value), "Projects:", "  alpha", "  beta", "", "Ready tasks:", "  none", ""]


def _render(old: Persona, new: Persona):
    lines = tuple(_block(old))
    return render(_block(new), StateShown(rev_of(lines), lines, "s"), session="s", whole_reason=None)


@pytest.mark.parametrize("field,limit", list(LIMITS.items()))
def test_a_field_at_its_limit_is_kept(field: str, limit: int) -> None:
    assert getattr(checked({field: "x" * limit}), field) == "x" * limit


@pytest.mark.parametrize("field,limit", list(LIMITS.items()))
def test_a_field_over_its_limit_is_refused(field: str, limit: int) -> None:
    with pytest.raises(ValueError, match=rf"{field} is {limit + 1} characters; the limit is {limit}"):
        checked({field: "x" * (limit + 1)})


def test_six_hundred_characters_is_the_total_limit() -> None:
    kept = checked({"name": "n" * 20, "address": "a" * 20, "tone": "t" * 160, "notes": "x" * 400})
    assert sum(len(getattr(kept, name)) for name in LIMITS) == 600
    with pytest.raises(ValueError, match="600"):
        checked({"name": "n" * 20, "address": "a" * 20, "tone": "t" * 200, "notes": "x" * 361})


def test_whitespace_only_is_unset_and_text_is_stripped() -> None:
    assert checked({"name": "  ", "address": "\n", "tone": " \t ", "notes": ""}) == Persona()
    assert checked({"name": " 管家 "}) == Persona(name="管家")
    assert checked({"name": None}).name == ""


def test_unknown_fields_and_non_strings_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown"):
        checked({"nickname": "x"})
    with pytest.raises(ValueError, match="tone must be a string"):
        checked({"tone": 1})
    with pytest.raises(ValueError, match="must be a JSON object"):
        checked(["name"])


def test_a_newline_inside_a_field_is_kept() -> None:
    assert checked({"notes": "a\nb"}).notes == "a\nb"


def test_update_refuses_an_empty_or_unknown_body_before_it_reads() -> None:
    async def check() -> None:
        with pytest.raises(ValueError, match=r"give at least one of name, address, tone, notes \(null clears it\)"):
            await persona.update({})
        with pytest.raises(ValueError, match="unknown"):
            await persona.update({"nickname": "x"})
        with pytest.raises(ValueError, match="tone must be a string"):
            await persona.update({"tone": 1})

    asyncio.run(check())


def test_the_section_text_is_pinned() -> None:
    full = Persona(name="管家", address="阿星", tone="简洁、直接", notes="keep it short")
    assert section(full) == [
        HEADING,
        "  Name: 管家",
        "  Address the operator as: 阿星",
        "  Tone: 简洁、直接",
        "  Notes: keep it short",
        "",
    ]
    assert section(Persona(name="管家")) == [HEADING, "  Name: 管家", ""]
    assert section(Persona()) == []
    assert section(checked({"name": "  ", "tone": " \n "})) == []


@pytest.mark.parametrize("raw,rendered", [
    ("a\nHeader:\r\n```b", "a Header: ` ` `b"),
    ("a\x00\t\x7f\x85\u2028\u2029\u202e\u2066b", "a b"),
    ("`a``b```c``````d", "`a``b` ` `c` ` ` ` ` `d"),
])
@pytest.mark.parametrize("group", [False, True])
def test_fields_render_as_single_lines_without_fences_and_keep_stored_text(raw, rendered, group) -> None:
    value = checked(dict.fromkeys(LIMITS, raw))
    stored = persona.encoded(value)
    lines = section(value, group=group)
    labels = ["Name", "Address the operator as"] + ([] if group else ["Tone", "Notes"])
    assert lines == [HEADING, *(f"  {label}: {rendered}" for label in labels), ""]
    assert all(len(line.splitlines()) <= 1 and "```" not in line for line in lines)
    assert persona.encoded(value) == stored
    assert json.loads(stored) == dict.fromkeys(LIMITS, raw)


def test_a_group_keeps_only_the_name_and_the_address() -> None:
    full = Persona(name="管家", address="阿星", tone="简洁、直接", notes="keep it short")
    assert section(full, group=True) == [
        HEADING,
        "  Name: 管家",
        "  Address the operator as: 阿星",
        "",
    ]
    assert section(Persona(tone="简洁、直接"), group=True) == []


def test_renaming_is_a_change_note_and_replacing_the_section_is_a_whole_block() -> None:
    # The heading is long, so a tiny block's note exceeds 40% of the block. A full notes line keeps a
    # name edit inside both limits (2 of 10 non-blank lines, and under 40% of the characters).
    notes = "x" * 400
    old = Persona(name="管家", address="阿星", tone="简洁、直接", notes=notes)
    renamed = _render(old, Persona(name="小助", address="阿星", tone="简洁、直接", notes=notes))
    assert renamed.view is StateView.CHANGES and renamed.reason == "changes"
    assert f" {HEADING}" in renamed.lines
    assert "+  Name: 小助" in renamed.lines and "-  Name: 管家" in renamed.lines

    replaced = _render(old, Persona(name="小助", address="你", tone="慢一点", notes="other"))
    assert replaced.view is StateView.WHOLE and replaced.reason == "many lines changed"


def test_persona_operations_are_operator_only_and_do_not_confirm() -> None:
    from looper.agents import api_v1

    get = api_v1.find("POST", "/api/v1/manager_persona/get")
    update = api_v1.find("POST", "/api/v1/manager_persona/update")
    assert get is not None and update is not None
    assert get.operator_only and get.risk == "read" and not get.confirms and not get.hand
    assert update.operator_only and update.risk == "write" and not update.confirms and not update.hand
    assert get.level is ActionLevel.INSIDE and update.level is ActionLevel.INSIDE and update.level_of is None
    assert update.request is not None and update.request["additionalProperties"] is False
    for name, limit in LIMITS.items():
        prop = update.request["properties"][name]
        assert prop["maxLength"] == limit and prop["type"] == ["string", "null"]
        assert name not in update.request.get("required", [])


def test_a_configuration_file_without_system_settings_still_validates() -> None:
    from looper.agents.config_transfer import FORMAT, ConfigFileError, validate_document

    validate_document({"format": FORMAT})
    with pytest.raises(ConfigFileError, match="settings.system must be a JSON object"):
        validate_document({"format": FORMAT, "settings": {"system": []}})
