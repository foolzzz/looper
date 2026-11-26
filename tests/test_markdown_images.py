"""Remote images in agent Markdown are neutralized before the text is sent to Lark."""
from __future__ import annotations

import json
import random
import re
import time

import pytest

from looper.agents.card_actions import Card, lark_card
from looper.agents.channels import lark_cli
from looper.agents.channels.lark_api import LarkApi
from looper.agents.channels.markdown_images import neutralize_images

U = "https://example.invalid/a.png?d=secret"
LINK = f"image: [chart (example.invalid)]({U})"
KEY_IMAGE = re.compile(r"!\[[^\[\]\\\n]{0,300}\]\(img_[A-Za-z0-9_-]{1,120}\)")


def has_live_image(text: str) -> bool:
    """True when ``text`` holds an image marker that is not a complete image-key construct, or an ``<img`` tag."""
    if re.search(r"<(?:img|picture|source)\b", text, re.I):
        return True
    return any(not KEY_IMAGE.match(text, m.start()) for m in re.finditer(r"!\[", text))


@pytest.mark.parametrize("source, expected", [
    (f"![chart]({U})", LINK),
    (f"x ![chart]({U}) y", f"x {LINK} y"),
    (f"!![chart]({U})", f"!{LINK}"),  # the bang before the new link cannot make it an image
    (f"![]({U})", "image: [(example.invalid)](" + U + ")"),
])
def test_plain_remote_images_become_links(source: str, expected: str) -> None:
    assert neutralize_images(source) == expected
    assert not has_live_image(neutralize_images(source))


@pytest.mark.parametrize("source", [
    f"![a [b] c]({U})",
    f"![a\\]b]({U})",
    f"![![k](img_v3_02abc)]({U})",
    f"![chart]({U} \"title\")",
    f"![chart]({U} (title))",
    f"![chart](<{U}>)",
    "![a](<https://h.invalid/a b.png>)",
    "![a](//h.invalid/x.png)",
    "![a](https&#58;//h.invalid/x)",
    "![a](&#104;ttps://h.invalid/x)",
    "![a](https\\://h.invalid/x)",
    "![a](data:image/png;base64,AAAA)",
    "![chart][r]\n\n[r]: https://h.invalid/a.png",
    "![chart][]\n[chart]: https://h.invalid/a.png",
    "![chart]\n[chart]: https://h.invalid/a.png",
    "![unterminated](https://h.invalid/a.png",
    "![a](img_v3_02abc extra)",
    "![a](IMG_v3)",
    "![a](img_)",
    "![x](file:///etc/passwd)",
])
def test_other_image_forms_lose_the_image_marker(source: str) -> None:
    out = neutralize_images(source)
    assert out != source
    assert not has_live_image(out)


@pytest.mark.parametrize("source", [
    '<img src="https://h.invalid/a.png">',
    "<IMG SRC=https://h.invalid/a.png>",
    '<img data-src="x" src="https://h.invalid/a.png">',
    '<img alt="a>b" src="https://h.invalid/a.png">',
    '<img srcset="https://h.invalid/a.png 1x">',
    '<picture><source srcset="https://h.invalid/a.png"></picture>',
    "<img/src=//h.invalid/a.png>",
    '<video poster="https://h.invalid/a.png"></video>',
    '<svg><image href="https://h.invalid/a.png"/></svg>',
])
def test_resource_html_tags_are_broken(source: str) -> None:
    out = neutralize_images(source)
    assert out != source and not re.search(r"<(?:img|picture|source|video|svg|image)\b", out, re.I)


@pytest.mark.parametrize("source", [
    "![chart](img_v3_02abc-def_1)",
    "![](img_v3_02abc)",
    "!![chart](img_v3_02abc)",
    "[docs](https://example.invalid/page) and <https://example.invalid/x> and [a]: https://example.invalid",
    '<at user_id="ou_x"></at> <at id=all></at> <b>bold</b> <font color="red">x</font>',
    "`" + f"![x]({U})" + "`",
    "``" + f"![x]({U})" + "``",
    f"```\n![x]({U})\n<img src=\"{U}\">\n```",
    f"~~~md\n![x]({U})\n~~~",
    f"````\n```\n![x]({U})\n````",
    f"```\n![x]({U})",  # an unclosed fence runs to the end, as in CommonMark
    "plain text, 1 < 2 and a > b",
])
def test_safe_and_code_content_is_untouched(source: str) -> None:
    assert neutralize_images(source) == source


@pytest.mark.parametrize("source", [
    f"a ` b\n\n![x]({U})\n\nc ` d",  # a code span cannot cross a blank line
    f"\\`![x]({U})\\`",  # escaped backticks open nothing
    f"```a`b\n![x]({U})\n```",  # a backtick fence has no backtick in its info string
    f"    ![x]({U})",  # indented code is not recognised either
    f"``` \n```\n![x]({U})",  # closed fence, then prose
])
def test_stray_code_markers_do_not_hide_images(source: str) -> None:
    assert not has_live_image(neutralize_images(source))


def test_text_around_code_is_still_neutralized() -> None:
    source = f"`a` ![x]({U})\n```\n![y]({U})\n```\n![z]({U})"
    out = neutralize_images(source)
    assert f"image: [x (example.invalid)]({U})" in out
    assert f"![y]({U})" in out
    assert f"image: [z (example.invalid)]({U})" in out


def test_choke_points_neutralize() -> None:
    md = f"![chart]({U})"
    body = json.loads(LarkApi._message(None, md)["content"])
    assert body["zh_cn"]["content"][0][0]["text"] == LINK
    assert lark_cli.send_command(chat_id="oc_x", markdown=md)[-1] == LINK
    assert lark_cli.reply_command("om_x", markdown=md)[-1] == LINK
    assert lark_cli.edit_command("om_x", markdown=md)[-1] == LINK
    card = lark_card(Card(text=md))
    assert card["body"]["elements"][0]["content"] == LINK
    assert json.loads(LarkApi._message(md, None)["content"]) == {"text": md}  # plain text is not Markdown


ADVERSARIAL = ["![", "<img ", "`", "[", "]", "(", ")", "![a](", "![a](https://h.invalid/", "\\", "\n", "```\n", "<", "!"]


@pytest.mark.parametrize("unit", ADVERSARIAL)
@pytest.mark.parametrize("size", [200_000, 1_000_000])
def test_linear_time_on_adversarial_input(unit: str, size: int) -> None:
    text = unit * (size // len(unit))
    started = time.perf_counter()
    neutralize_images(text)
    assert time.perf_counter() - started < 0.5


def test_random_inputs_never_leave_a_live_image() -> None:
    tokens = ["![", "!", "[", "]", "(", ")", "<img ", "<IMG", "src=", "img_v3_0a", "https://h.invalid/x", "//h/x",
              " ", "\n", "\n\n", "\\", "&#58;", "<", ">", "\"", "alt", "<picture>", "title"]
    rng = random.Random(7)
    for _ in range(3000):
        text = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 40)))
        out = neutralize_images(text)
        assert not has_live_image(out), (text, out)
