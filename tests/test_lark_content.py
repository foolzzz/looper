"""Rendered Lark content is normalized without changing plain-text semantics."""

import pytest

from looper.agents.channels.lark_content import ContentKind, ResourceKind, ResourceRef, parse

BOT = {"id": "ou_bot0000001", "name": "HelperBot", "key": "@_user_1"}


def test_post_preserves_formatting_and_numbers_resources_in_appearance_order() -> None:
    content = ('Weekly\n**Hello** [Spec](https://example.com/spec)\n![Image](img_v3_a1)\n\n'
               '```go\n    fmt.Println(1)\n```\n<file key="file_v3_b2" name="a \\"q\\".pdf"/>')
    inbound = parse("post", content, [])
    assert inbound.kind is ContentKind.RICH
    assert inbound.text == ('Weekly\n**Hello** [Spec](https://example.com/spec)\n[image 1]\n\n'
                            '```go\n    fmt.Println(1)\n```\n[file 2: a "q".pdf]')
    assert inbound.resources == (ResourceRef(1, ResourceKind.IMAGE, "img_v3_a1"),
                                 ResourceRef(2, ResourceKind.FILE, "file_v3_b2", 'a "q".pdf'))


@pytest.mark.parametrize(("content", "expected"), [
    ("@@HelperBot 看看这个", "看看这个"),
    ('<at user_id="ou_bot0000001">HelperBot</at> hi '
     '<at user_id="ou_zyxwvut987">Alan Turing</at> <at user_id="all"></at>', "hi @Alan Turing @all"),
    ('<at user_id="ou_someone"></at>', "@ou_someone"),
    (f"@@{BOT['key']} hi", "hi"),
    ("@_user_1 hi", "hi"),
    ("@HelperBot hi @HelperBot", "hi @HelperBot"),
    ("@HelperBot ```py\n    a =  1\n\n    b = 2\n```", "```py\n    a =  1\n\n    b = 2\n```"),
    ("[Media: file_v3_c] <video key=\"file_video\"/>",
     "[video: not supported yet] [video: not supported yet]"),
    ('<audio key="file_audio"/> <folder key="folder_x"/> <folder key="folder_y">\na\n</folder>',
     "[voice message: not supported yet] [folder: not supported yet] [folder: not supported yet]"),
    ("![Image](img_../x)", "![Image](img_../x)"),
    (":SMILE:", ":SMILE:"),
    ("[Rich text message]", "[Rich text message]"),
    ("@@HelperBot ", ""),
])
def test_rich_mentions_and_non_downloadable_markers(content: str, expected: str) -> None:
    inbound = parse("post", content, [BOT])
    assert inbound.text == expected
    assert inbound.resources == ()


@pytest.mark.parametrize(("marker", "name"), [
    ('<file key="file_x" name="a\\nb\\rc\\td\\\\e\\q"/>', "a b c d\\eq"),
    ('<file key="file_x"/>', "file_x"),
    ('<file key="file_x" name=""/>', "file_x"),
])
def test_file_names_are_unescaped_and_default_to_the_key(marker: str, name: str) -> None:
    inbound = parse("post", '[Image: img_x]\n' + marker, [])
    assert inbound.resources == (ResourceRef(1, ResourceKind.IMAGE, "img_x"),
                                 ResourceRef(2, ResourceKind.FILE, "file_x", name))
    assert inbound.text == f"[image 1]\n[file 2: {name}]"


def test_text_keeps_literal_resource_markers_and_existing_space_normalization() -> None:
    inbound = parse("text", "@HelperBot see  ![Image](img_x)", [BOT])
    assert inbound.kind is ContentKind.TEXT
    assert inbound.text == "see ![Image](img_x)"
    assert inbound.resources == ()


@pytest.mark.parametrize("kind", ["sticker", "", "unknown"])
def test_other_types_are_unsupported(kind: str) -> None:
    inbound = parse(kind, "anything", [])
    assert inbound.kind is ContentKind.UNSUPPORTED
    assert inbound.text == "" and inbound.resources == ()
