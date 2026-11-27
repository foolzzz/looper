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


TAG_OPENER = re.compile(r"<(?=[A-Za-z]|/[A-Za-z]|[!?])(?!/?(?:at|a|br|hr|person|local_datetime|text_tag|number_tag|raw|font|u)"
                        r"(?![A-Za-z0-9_-]))(?!(?:https?://|mailto:)\S*>)", re.I)


def has_live_image(text: str) -> bool:
    """True when ``text`` holds a construct a renderer could load: a tag opener outside Lark's tags, or an image
    marker that is not a complete image-key construct and is followed by a target, a reference, a defined
    label, or no closing bracket within the label window."""
    if TAG_OPENER.search(text):
        return True
    defined = {" ".join(m[1].split()).casefold()
               for m in re.finditer(r"^[ \t>]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)*\[([^\[\]\\]+)\]:", text, re.M)}
    if re.search(r"<(?:at|a|font|text_tag|person|number_tag|local_datetime|raw|u|br|hr)\b[^>]*\b(?:style|background|src|srcset)\s*=", text, re.I):
        return True
    for m in re.finditer(r"!\[", text):
        if KEY_IMAGE.match(text, m.start()):
            continue
        if (len(text[:m.start()]) - len(text[:m.start()].rstrip("\\"))) % 2:
            continue  # an escaped bang is a literal bang
        end, depth, k = -1, 1, m.end()
        while k < min(len(text), m.end() + 300):
            if text[k] == "\\":
                k += 1
            elif text[k] == "[":
                depth += 1
            elif text[k] == "]":
                depth -= 1
                if depth == 0:
                    end = k
                    break
            k += 1
        if end == -1 or text[end + 1:end + 2] in ("(", "["):
            return True
        if " ".join(text[m.end():end].split()).casefold() in defined:
            return True
    return False


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
    '<div style="background:url(https://h.invalid/a.png)">x</div>',
    '<table background="https://h.invalid/a.png"><tr><td>x</td></tr></table>',
    '<body background="https://h.invalid/a.png">',
    '<meta http-equiv="refresh" content="0;url=https://h.invalid/">',
    '<!-- note -->', '<?php x ?>', '<textarea>x</textarea>', '<pre>x</pre>',
    '<link rel="stylesheet" href="https://h.invalid/a.css">', '<audio src="https://h.invalid/a.mp3"></audio>',
    'Vec<String> and <b>bold</b>',
])
def test_resource_html_tags_are_broken(source: str) -> None:
    out = neutralize_images(source)
    assert out != source and not TAG_OPENER.search(out) and "&#60;" in out


@pytest.mark.parametrize("source", [
    "![chart](img_v3_02abc-def_1)",
    "![](img_v3_02abc)",
    "!![chart](img_v3_02abc)",
    "[docs](https://example.invalid/page) and <https://example.invalid/x> and [a]: https://example.invalid",
    '<at user_id="ou_x"></at> <at id=all></at> <font color="red">x</font><br/><hr> <person id=ou_x></person> <u>x</u>',
    "<https://example.invalid/x> <mailto:a@example.invalid> a < b > c, 1<2, x <- y",
    "vec![1, 2, 3]", "Done![ok]", "echo ![a]", "assert!(x); println!(\"hi\"); ", "!", "![]", "wow!![x]",
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
    # a backslash escapes only the first backtick of an opener; a closer matches on its raw length
    "x```\\```![a](https://e.example/y)```",
    "x``\\``![a](https://e.example/y)``",
    "``\\`` ![a](https://e.example/y) ``",
    "`\\`![a](https://e.example/y)`",
    "`a\n``\\`` ![a](https://e.example/y)\n`",  # a stray backtick on another line: no spans in the paragraph
    # a code span never crosses a newline: these block boundaries end a paragraph, so no span forms
    "`a\n---\n![](https://e.example/y)\n`",
    "`a\n# ![](https://e.example/y)\n`",
    "`a\n> ![](https://e.example/y)\n> `",
    "`a\n- ![](https://e.example/y)\n`",
    "[r]: https://e.example/x``\n![r]``",
    "![r]\n\n[r]: https://e.example/y",  # a bare label with a definition is an image
    "- [r]: https://e.example/y\n\n![R]",
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


@pytest.mark.parametrize("opener, closer", [("<pre>", "</pre>"), ("<!--", "-->"), ("<textarea>", "</textarea>")])
def test_html_blocks_cannot_desync_the_fence_state(opener: str, closer: str) -> None:
    out = neutralize_images(f"{opener}\n```\n{closer}\n![a](https://e.example/y)")
    # no HTML block starts, so the ``` line opens a real fence that runs to the end: the rest is code, as a renderer reads it
    assert out.startswith("&#60;") and out.endswith(f"{closer}\n![a](https://e.example/y)")


def test_unterminated_label_fails_closed() -> None:
    assert neutralize_images("see ![ and more") == "see &#33;[ and more"


@pytest.mark.parametrize("source", [
    '<font style="background-image:url(https://evil.example/a.png)">x</font>',
    '<font color="red" style="x">x</font>',
    '<font background="https://evil.example/a.png">x</font>',
    '<font color="url(https://evil.example/a.png)">x</font>',
    '<font color=red/x>x</font>',
    '<at id=all style="list-style-image:url(https://evil.example/a.png)"></at>',
    '<at id="https://evil.example/a.png"></at>',
    '<a href="//evil.example/a">x</a>', '<a href="javascript:alert(1)">x</a>', '<a href="https&#58;//evil.example/">x</a>',
    '<a href="https://evil.example/a" style="border-image:url(https://e.example/a.png)">x</a>',
    '<a href="https://evil.example/a" background="x">x</a>',
    '<person id=ou_1 style="background:url(https://e.example/a.png)"></person>',
    '<text_tag src="https://e.example/a.png">x</text_tag>',
    '<br style="background:url(https://e.example/a.png)">', '<hr background=x>', '<u style=x>x</u>',
    '</font style=x>', '<font color="red"', '<math><mglyph src="https://e.example/a.png"></mglyph></math>',
])
def test_lark_tags_keep_only_their_own_attributes(source: str) -> None:
    out = neutralize_images(source)
    assert out.startswith("&#60;") or "&#60;" in out
    assert not re.search(r"<(?:font|at|a|person|text_tag|br|hr|u|math|mglyph)\b[^>]*(?:style|background|src|url\(|//evil|javascript)", out, re.I)


@pytest.mark.parametrize("source", [
    '<font color="red">x</font>', "<font color='green'>x</font>", "<font color=grey>x</font>",
    '<at id=all></at>', '<at user_id="ou_abc123"></at>', '<at email="a@example.invalid"></at>',
    '<a href="https://example.invalid/page?q=1">x</a>', '<person id="ou_1" show_name=true show_avatar=false></person>',
    '<text_tag color="blue">tag</text_tag>', '<local_datetime millisecond="1700000000000" format_type="date"></local_datetime>',
    '<number_tag background_color=grey font_color=white url="https://example.invalid/n">1</number_tag>',
    '<br/>', '<br>', '<hr/>', '<u>x</u>', '<raw>x</raw>', '</at>',
])
def test_lark_tags_with_their_own_attributes_pass(source: str) -> None:
    assert neutralize_images(source) == source


def test_escaped_bang_is_a_literal_bang_before_a_link() -> None:
    assert neutralize_images("\\![a](https://h.invalid/x)") == "\\![a](https://h.invalid/x)"
    assert neutralize_images("\\\\![a](https://h.invalid/x)") == "\\\\image: [a (h.invalid)](https://h.invalid/x)"


def test_key_with_a_title_is_not_a_key() -> None:
    assert neutralize_images('![k](img_v3_a1 "title")') == '&#33;[k](img_v3_a1 "title")'


def test_less_than_in_a_link_target_is_escaped_too() -> None:
    # a link target cannot be told from raw HTML without a full parser (``a](https://x/<img/src=U>)`` has no
    # link), so the escape stays; the entity is decoded inside a CommonMark target
    assert neutralize_images("[a](https://x.invalid/q=<img)") == "[a](https://x.invalid/q=&#60;img)"
    assert neutralize_images("a](https://x.invalid/<img/src=https://e.invalid/a.png>)").count("&#60;") == 1


def test_ordinary_text_with_brackets_is_not_mangled() -> None:
    text = "let v = vec![1, 2, 3]; println!(\"x\"); Done![ok]; echo ![a]; vec![vec![1], 2]"
    assert neutralize_images(text) == text


def test_label_nested_in_a_remote_image_keeps_its_own_key_image() -> None:
    out = neutralize_images("![![k](img_v3_0a)](https://h.invalid/x)")
    assert out.startswith("&#33;[") and "![k](img_v3_0a)" in out


ADVERSARIAL = ["![", "<img ", "`", "[", "]", "(", ")", "![a](", "![a](https://h.invalid/", "\\", "\n", "```\n", "<", "!", "![a]", "![a][", "![[]", "<at a ", "<font x=\"", "\\![a](", "<a:", "<![", "[r]:"]


@pytest.mark.parametrize("unit", ADVERSARIAL)
@pytest.mark.parametrize("size", [200_000, 1_000_000])
def test_linear_time_on_adversarial_input(unit: str, size: int) -> None:
    text = unit * (size // len(unit))
    started = time.perf_counter()
    neutralize_images(text)
    assert time.perf_counter() - started < (0.5 if size <= 200_000 else 3.0)


def test_random_inputs_never_leave_a_live_image() -> None:
    tokens = ["<font ", "<at style=", "<a href=", "style=", "![", "!", "[", "]", "[r]: u", "![r]", "---", "# ", "> ", "- ", "<div>", "<!--", "(", ")", "<img ", "<IMG", "src=", "img_v3_0a", "https://h.invalid/x", "//h/x",
              " ", "\n", "\n\n", "\\", "&#58;", "<", ">", "\"", "alt", "<picture>", "title"]
    rng = random.Random(7)
    for _ in range(6000):
        text = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 40)))
        out = neutralize_images(text)
        assert not has_live_image(out), (text, out)


def test_ampersand_in_a_plain_url_attribute_is_kept_but_entities_are_not() -> None:
    assert neutralize_images('<a href="https://example.invalid/p?a=1&b=2">x</a>') == '<a href="https://example.invalid/p?a=1&b=2">x</a>'
    assert neutralize_images('<a href="https&#58;//example.invalid/">x</a>').startswith("&#60;")
    assert neutralize_images('<a href="https://example.invalid/&#47;x">x</a>').startswith("&#60;")
    assert neutralize_images('<a href="https://example.invalid/?&amp;x">x</a>').startswith("&#60;")


def test_paragraph_with_a_stray_backtick_has_no_code_spans() -> None:
    source = f"`code` and `![x]({U})`\nstray ` here"
    out = neutralize_images(source)
    assert out == f"`code` and `&#33;[x]({U})`\nstray ` here"  # every ![ is neutralized, no link rewrite
    assert neutralize_images(f"`![x]({U})`\n\nstray ` here").startswith(f"`![x]({U})`")  # a blank line ends the paragraph


def _oracle_leaks(markdown: str) -> list[str]:
    """What a CommonMark renderer with HTML enabled would load from ``markdown``."""
    from markdown_it import MarkdownIt

    leaks: list[str] = []
    stack = MarkdownIt("commonmark", {"html": True}).parse(markdown)
    while stack:
        token = stack.pop()
        stack.extend(token.children or [])
        if token.type == "image" and not KEY.fullmatch(str(token.attrs.get("src", ""))):
            leaks.append(f"image {token.attrs.get('src')}")
        if token.type in ("html_inline", "html_block") and RESOURCE_TAG.search(token.content):
            leaks.append(f"html {token.content[:40]!r}")
    return leaks


KEY = re.compile(r"img_[A-Za-z0-9_-]{1,120}")
RESOURCE_TAG = re.compile(r"<\s*(?:img|picture|source|video|audio|svg|embed|object|iframe|link|script|style|input|div|table|"
                          r"meta|body|math|mglyph|image|pre|textarea|!--)", re.I)
REVIEW_CORPUS = [
    "x```\\```![a](https://e.example/y)```", "x``\\``![a](https://e.example/y)``",
    "``\\`` ![a](https://e.example/y) ``", "`\\`![a](https://e.example/y)`",
    "`a\n---\n![](https://e.example/y)\n`", "`a\n# ![](https://e.example/y)\n`",
    "`a\n> ![](https://e.example/y)\n> `", "`a\n- ![](https://e.example/y)\n`",
    "[r]: https://e.example/x``\n![r]``", "    ```\n![a](https://e.example/y)\n    ```",
    "\t```\n![a](https://e.example/y)\n\t```", "`~~~\n<div><img src=https://e.example/y>",
    "<pre>\n```\n</pre>\n![a](https://e.example/y)", "<!--\n```\n-->\n![a](https://e.example/y)",
    "``'/\\``![a](https://e.example/y)]/\n`<svg><!--a<at id=1>\\`![a](https://e.example/y)",
    "<at id=1>\n`<img src=https://e.example/y>`",
    "![x `a]\n`](http://e.example/y)", "![a ![r] z `]\n`](http://e.example/y)", "![![r]\\``]>\n`](http://e.example/y)",
    "<at id=1>\n```\n<img src=https://e.example/y>\n```",  # a Lark tag line opens an HTML block: no fence in it
    "<br>\n~~~\n<!--\n<img src=https://e.example/y>",
]


@pytest.mark.parametrize("source", REVIEW_CORPUS)
def test_review_corpus_leaves_nothing_for_a_renderer_to_load(source: str) -> None:
    assert _oracle_leaks(source), "the corpus input must leak before the change"
    assert _oracle_leaks(neutralize_images(source)) == []


def test_alt_text_crossing_a_line_leaves_nothing_for_a_renderer_to_load() -> None:
    """An image whose label is on one line and ``](url)`` on the next, with backticks and brackets in between."""
    labels = ["x", "a ![r] z", "![r]\\`", "x `a", "`", "``", "a\\", "![k](img_v3_0a)", "[b]", "a [b", "![", "<at id=1>"]
    joints = ["]", "]>", "]\n", "`]", "]`", "] "]
    seconds = ["`](http://e.example/y)", "](http://e.example/y)", "`\n](http://e.example/y)", "``](http://e.example/y)", "\\`](http://e.example/y)",
               "](<http://e.example/y>)", "][r]\n\n[r]: http://e.example/y", "](\nhttp://e.example/y)"]
    rng = random.Random(18)
    for _ in range(4000):
        text = "![" + rng.choice(labels) + rng.choice(joints) + "\n" + rng.choice(seconds)
        out = neutralize_images(text)
        assert _oracle_leaks(out) == [], (text, out)


def test_seeded_random_inputs_leave_nothing_for_a_renderer_to_load() -> None:
    pieces = ["`", "``", "```", "\\", "\\`", "\n", "\n\n", " ", "![a](https://e.example/y)", "![a][r]", "![r]", "[r]: https://e.example/y",
              "<img src=https://e.example/y>", "<div>", "<!--", "-->", "<pre>", "</pre>", "<at id=1>", "<font style=x>", "#", "> ", "- ",
              "---", "~~~", "    ", "\t", "]", "[", "(", ")", "<", ">", "'", "/", "!", "a", "img_v3_0a", "![k](img_v3_0a)"]
    rng = random.Random(20260126)
    for _ in range(20_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 30)))
        out = neutralize_images(text)
        leaks = _oracle_leaks(out)
        assert leaks == [], (text, out, leaks)
