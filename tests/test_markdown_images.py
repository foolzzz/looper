"""Remote Markdown images in agent text become links before they are sent to Lark."""
from __future__ import annotations

import json

import pytest

from looper.agents.card_actions import Card, lark_card
from looper.agents.channels import lark_cli
from looper.agents.channels.lark_api import LarkApi
from looper.agents.channels.markdown_images import remote_images_as_links

LINK = "[image: chart (example.invalid)](https://example.invalid/a.png?d=secret)"


@pytest.mark.parametrize("source, expected", [
    ("![chart](https://example.invalid/a.png?d=secret)", LINK),
    ('![chart](https://example.invalid/a.png?d=secret "title")', LINK),
    ("![chart](<https://example.invalid/a.png?d=secret>)", LINK),
    ("![chart][r]\n\n[r]: https://example.invalid/a.png?d=secret", LINK + "\n\n[r]: https://example.invalid/a.png?d=secret"),
    ("![chart][]\n[chart]: https://example.invalid/a.png?d=secret", LINK + "\n[chart]: https://example.invalid/a.png?d=secret"),
    ('<img src="https://example.invalid/a.png?d=secret" alt="chart">', LINK),
    ("<img alt='chart' src='https://example.invalid/a.png?d=secret' />", LINK),
    ("see ![x](HTTP://example.invalid/a.png) now", "see [image: x (example.invalid)](HTTP://example.invalid/a.png) now"),
    ("![](https://example.invalid/a.png)", "[image: (example.invalid)](https://example.invalid/a.png)"),
])
def test_remote_images_become_links(source: str, expected: str) -> None:
    assert remote_images_as_links(source) == expected


@pytest.mark.parametrize("source", [
    "![chart](img_v3_02abc)",
    "![chart][r]\n[r]: img_v3_02abc",
    "![chart][missing]",
    "[docs](https://example.invalid/page) and <https://example.invalid/x>",
    "`![x](https://example.invalid/a.png)`",
    "``![x](https://example.invalid/a.png)``",
    "```\n![x](https://example.invalid/a.png)\n<img src=\"https://example.invalid/a.png\">\n```",
    "~~~md\n![x](https://example.invalid/a.png)\n~~~",
    "<img src=\"img_v3_02abc\">",
    "![x](file:///etc/passwd)",
    "plain text without images",
])
def test_everything_else_is_untouched(source: str) -> None:
    assert remote_images_as_links(source) == source


def test_text_around_code_is_still_rewritten() -> None:
    source = "`a` ![x](https://example.invalid/a.png)\n```\n![y](https://example.invalid/b.png)\n```\n![z](https://example.invalid/c.png)"
    out = remote_images_as_links(source)
    assert "[image: x (example.invalid)](https://example.invalid/a.png)" in out
    assert "![y](https://example.invalid/b.png)" in out
    assert "[image: z (example.invalid)](https://example.invalid/c.png)" in out


def test_choke_points_rewrite() -> None:
    md = "![chart](https://example.invalid/a.png)"
    want = "[image: chart (example.invalid)](https://example.invalid/a.png)"
    body = json.loads(LarkApi._message(None, md)["content"])
    assert body["zh_cn"]["content"][0][0]["text"] == want
    assert lark_cli.send_command(chat_id="oc_x", markdown=md)[-1] == want
    assert lark_cli.reply_command("om_x", markdown=md)[-1] == want
    assert lark_cli.edit_command("om_x", markdown=md)[-1] == want
    card = lark_card(Card(text=md))
    assert card["body"]["elements"][0]["content"] == want
    # plain text is not Markdown: left alone
    assert json.loads(LarkApi._message(md, None)["content"]) == {"text": md}
