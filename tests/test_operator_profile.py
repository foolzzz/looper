"""The operator profile is compiled from notes, with no model and no clock."""

from pathlib import Path

import pytest

from looper.agents.operator_profile import CAP, ProfileError, compile_profile


def _note(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _about(root: Path, extra: str = "") -> None:
    _note(root, "关于我.md", """# 关于我

## 名字
Ada

## 称呼
Ada 老师

## 回复语言
中文

## 时区
Asia/Shanghai

## 工作时间
工作日 10:00-19:00

## 沟通偏好
- 先给结论
- 用短句
""" + extra)


def _rules(root: Path) -> None:
    _note(root, "偏好/format.md", """---
summary: 不要这条
---

## 格式
- 一条一段
- 先标题
""")
    _note(root, "偏好/reply.md", """---
summary: "回复先给结论"
---

# 回复

正文不进规则。
""")


def test_typed_fields_keep_provenance_and_document_order(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _about(root, "\n## 爱好\n下棋\n")
    _rules(root)
    _note(root, "偏好/nested/tone.md", "## 语气\n- 第二条\n- 第一条\n")
    compiled = compile_profile(root)
    again = compile_profile(root)

    assert compiled == again
    assert compiled.text == "\n".join((
        "Name: Ada",
        "Address: Ada 老师",
        "Reply language: 中文",
        "Time zone: Asia/Shanghai",
        "Working hours: 工作日 10:00-19:00",
        "Communication:",
        "- 先给结论",
        "- 用短句",
        "Standing rules:",
        "- 一条一段",
        "- 先标题",
        "- 第二条",
        "- 第一条",
        "- 回复先给结论",
    ))
    assert compiled.fields["name"] == {"value": "Ada", "path": "关于我.md", "heading": "名字", "line": 3}
    assert compiled.fields["address"]["heading"] == "称呼"
    assert compiled.fields["reply_language"]["value"] == "中文"
    assert compiled.fields["time_zone"]["heading"] == "时区"
    assert compiled.fields["working_hours"]["value"] == "工作日 10:00-19:00"
    assert [item["value"] for item in compiled.fields["communication_preferences"]] == ["先给结论", "用短句"]
    assert [item["value"] for item in compiled.fields["standing_rules"]] == [
        "一条一段", "先标题", "第二条", "第一条", "回复先给结论"]
    assert compiled.fields["standing_rules"][4] == {
        "value": "回复先给结论", "path": "偏好/reply.md", "heading": "回复", "line": 5}
    assert compiled.fields["standing_rules"][2]["path"] == "偏好/nested/tone.md"
    assert "不要这条" not in compiled.text and "下棋" not in compiled.text and "正文不进规则" not in compiled.text
    assert compiled.omitted == () and compiled.skipped == ()
    assert compiled.sources == ("偏好/format.md", "偏好/nested/tone.md", "偏好/reply.md", "关于我.md")
    assert str(root) not in compiled.text
    dumped = repr(compiled.payload())
    assert str(root) not in dumped


def test_english_headings_and_a_lower_heading_stay_in_the_section(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "记忆/关于我.md", """# About

## Name
Ada
### aka
Lovelace

## Address
teacher
""")
    compiled = compile_profile(root)
    assert compiled.fields["name"] == {
        "value": "Ada ### aka Lovelace", "path": "记忆/关于我.md", "heading": "Name", "line": 3}
    assert compiled.fields["address"]["heading"] == "Address"
    assert compiled.text.startswith("Name: Ada ### aka Lovelace\nAddress: teacher")


def test_prose_is_one_value_and_an_empty_section_is_left_out(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "关于我.md", """# 关于我

## 名字
Ada   Lovelace

## 沟通偏好
- 先给结论
补充一句

## 工作时间

""")
    compiled = compile_profile(root)
    assert compiled.fields["name"]["value"] == "Ada Lovelace"
    assert compiled.fields["communication_preferences"] == [{
        "value": "- 先给结论 补充一句", "path": "关于我.md", "heading": "沟通偏好", "line": 6}]
    assert compiled.fields["working_hours"] is None
    assert "Working hours" not in compiled.text and compiled.text.count("Communication:") == 1


def test_a_heading_without_a_summary_is_skipped_and_outside_notes_are_ignored(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _about(root)
    _note(root, "偏好/_index.md", "---\nsummary: 索引\n---\n# 索引\n")
    _note(root, "偏好/_draft/hidden.md", "---\nsummary: 草稿\n---\n# 草稿\n")
    _note(root, "偏好/.secret.md", "---\nsummary: 秘密\n---\n# 秘密\n")
    _note(root, "偏好/empty.md", "\n\n")
    _note(root, "偏好/title.md", "# 只有标题\n")
    _note(root, "偏好/orphan.md", "---\nsummary: 没有标题\n---\n\n只有正文\n")
    _note(root, "README.md", "---\nsummary: 说明\n---\n# 说明\n")
    _note(root, "其他/note.md", "---\nsummary: 别的\n---\n# 别的\n")
    compiled = compile_profile(root)
    assert compiled.skipped == (
        {"path": "偏好/orphan.md", "reason": "no heading to cite"},
        {"path": "偏好/title.md", "reason": "no summary"},
    )
    blob = compiled.text + repr(compiled.skipped) + repr(compiled.sources)
    for hidden in ("索引", "草稿", "秘密", "说明", "别的"):
        assert hidden not in blob
    assert "偏好/empty.md" not in repr(compiled.skipped)
    assert "偏好/title.md" not in compiled.sources and "偏好/orphan.md" not in compiled.sources


def test_over_the_cap_drops_the_lowest_priority_and_keeps_its_source(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "关于我.md", "## 名字\nAda\n")
    _note(root, "偏好/rule.md", "---\nsummary: " + ("规则" * 40) + "\n---\n# 规则\n")
    name = "Name: Ada"
    compiled = compile_profile(root, cap=len(name))
    assert compiled.text == name and len(compiled.text) <= len(name)
    assert compiled.fields["name"]["value"] == "Ada"
    assert compiled.fields["standing_rules"] == []
    assert compiled.omitted == (
        {"field": "standing_rules", "path": "偏好/rule.md", "heading": "规则", "line": 4},)
    assert compiled.sources == ("偏好/rule.md", "关于我.md")


def test_one_field_over_the_cap_is_truncated_with_an_ellipsis(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "关于我.md", "## 名字\n" + ("名" * 80) + "\n")
    cap = len("Name: ") + 10
    compiled = compile_profile(root, cap=cap)
    assert len(compiled.text) <= cap and compiled.text.endswith("…")
    assert compiled.fields["name"]["truncated"] is True
    assert compiled.omitted == () and compiled.sources == ("关于我.md",)
    with pytest.raises(ProfileError, match="smaller than a field label"):
        compile_profile(root, cap=3)


def test_duplicate_headings_two_about_notes_and_a_bad_preference_fail(tmp_path: Path) -> None:
    both = tmp_path / "both"
    _note(both, "关于我.md", "## 名字\nAda\n")
    _note(both, "记忆/关于我.md", "## Name\nAda\n")
    with pytest.raises(ProfileError, match="two about-me notes") as raised:
        compile_profile(both)
    assert str(both) not in str(raised.value)

    twice = tmp_path / "twice"
    _note(twice, "关于我.md", "## 名字\nAda\n## Name\nBee\n")
    with pytest.raises(ProfileError, match="two headings for name"):
        compile_profile(twice)

    closed = tmp_path / "closed"
    _note(closed, "偏好/rule.md", "---\nsummary: 一条\nsummary: 两条\n---\n# 规则\n")
    with pytest.raises(ProfileError, match="two summary fields"):
        compile_profile(closed)

    block = tmp_path / "block"
    _note(block, "偏好/rule.md", "---\nsummary: |\n  折行\n---\n# 规则\n")
    with pytest.raises(ProfileError, match="single line"):
        compile_profile(block)

    open_note = tmp_path / "open"
    _note(open_note, "偏好/rule.md", "---\nsummary: 一条\n# 规则\n")
    with pytest.raises(ProfileError, match="frontmatter never closes"):
        compile_profile(open_note)


def test_a_note_outside_the_folder_a_file_named_preferences_and_a_bad_file_fail(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ProfileError, match="source folder does not exist"):
        compile_profile(missing)

    preferences = tmp_path / "file"
    preferences.mkdir()
    (preferences / "偏好").write_text("不是目录", encoding="utf-8")
    with pytest.raises(ProfileError, match="偏好 must be a directory"):
        compile_profile(preferences)

    outside = tmp_path / "outside.md"
    outside.write_text("## 名字\n泄露\n", encoding="utf-8")
    linked = tmp_path / "linked"
    _note(linked, "关于我.md", "## 名字\nAda\n")
    (linked / "偏好").mkdir()
    (linked / "偏好" / "leak.md").symlink_to(outside)
    with pytest.raises(ProfileError, match="偏好/leak.md resolves outside") as raised:
        compile_profile(linked)
    assert "泄露" not in str(raised.value) and str(outside) not in str(raised.value)

    folder = tmp_path / "outer-folder"
    (folder / "偏好").mkdir(parents=True)
    (folder / "偏好" / "secret.md").write_text("---\nsummary: 外部\n---\n# 外部\n", encoding="utf-8")
    pointed = tmp_path / "pointed"
    pointed.mkdir()
    (pointed / "偏好").symlink_to(folder / "偏好")
    with pytest.raises(ProfileError, match="偏好 resolves outside"):
        compile_profile(pointed)

    about = tmp_path / "about-link"
    about.mkdir()
    (about / "关于我.md").symlink_to(outside)
    with pytest.raises(ProfileError, match="关于我.md resolves outside"):
        compile_profile(about)

    binary = tmp_path / "binary"
    binary.mkdir()
    (binary / "偏好").mkdir()
    (binary / "偏好" / "bad.md").write_bytes(b"\xff\xfe")
    with pytest.raises(ProfileError, match="偏好/bad.md is not utf-8"):
        compile_profile(binary)

    huge = tmp_path / "huge"
    huge.mkdir()
    (huge / "偏好").mkdir()
    (huge / "偏好" / "big.md").write_bytes(b"a" * (256 * 1024 + 1))
    with pytest.raises(ProfileError, match="larger than"):
        compile_profile(huge)

    many = tmp_path / "many"
    (many / "偏好").mkdir(parents=True)
    for index in range(201):
        (many / "偏好" / f"{index:03d}.md").write_text("---\nsummary: x\n---\n# H\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="more than 200 notes"):
        compile_profile(many)


def test_preferences_alone_compile_and_an_inside_symlink_keeps_the_named_path(tmp_path: Path) -> None:
    root = tmp_path / "only"
    _note(root, "偏好/rule.md", "---\nsummary: 短句\n---\n# 规则\n")
    compiled = compile_profile(root)
    assert compiled.fields["name"] is None
    assert compiled.text == "Standing rules:\n- 短句"
    assert "Name:" not in compiled.text

    linked = tmp_path / "named"
    _note(linked, "notes/about.md", "## 名字\nAda\n")
    (linked / "关于我.md").symlink_to(linked / "notes" / "about.md")
    compiled = compile_profile(linked)
    assert compiled.fields["name"]["path"] == "关于我.md"
    assert compiled.fields["name"]["value"] == "Ada"


def test_configured_file_reuses_frontmatter_handling_and_cap(tmp_path: Path) -> None:
    _note(tmp_path, "关于我.md", "## 名字\nIgnored\n")
    _note(tmp_path, "偏好/rule.md", "## Rule\nIgnored preference\n")
    _note(tmp_path, "记忆/画像.md", "---\nsummary: Ignored metadata\n---\n## 名字\nAda\n"
          "- Working on a compiler [kind:: fact]\n")
    compiled = compile_profile(tmp_path, source="记忆/画像.md")
    assert compiled.text == "Name: Ada\nStanding rules:\n- Working on a compiler"
    assert compiled.sources == ("记忆/画像.md",)
    assert compiled.fields["name"]["line"] == 4
    _note(tmp_path, "记忆/画像.md", "---\nsummary: metadata\n---\n## Name\n" + "名" * 1600)
    capped = compile_profile(tmp_path, source="记忆/画像.md")
    assert len(capped.text) == CAP and capped.text.endswith("…")


_ENTRY = "- 回复用中文 [kind:: preference] [source:: 对话 2026-11-20] [confidence:: high]"


def test_inline_entries_are_classified_by_kind_and_cite_the_line(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "关于我.md", f"""# 关于我

{_ENTRY}
- 工作日下午开会 [kind:: habit] [source:: 对话 2026-11-20] [confidence:: high]
- 在上海 [kind:: fact] [source:: 对话 2026-11-20] [confidence:: high]
- 旧称呼 [kind:: preference] [source:: 对话 2026-11-20] [confidence:: high] [status:: superseded]
- 神秘 [kind:: mystery] [source:: 对话 2026-11-20] [confidence:: high]
""")
    _note(root, "偏好/nested/reply.md", _ENTRY + "\n")
    compiled = compile_profile(root)

    assert compiled.fields["communication_preferences"][0] == {
        "value": "回复用中文", "path": "关于我.md", "heading": "关于我", "line": 3, "kind": "preference"}
    assert compiled.fields["communication_preferences"][1]["kind"] == "habit"
    assert compiled.fields["communication_preferences"][1]["line"] == 4
    assert compiled.fields["standing_rules"] == [{
        "value": "在上海", "path": "关于我.md", "heading": "关于我", "line": 5, "kind": "fact"}]
    assert compiled.fields["communication_preferences"][-1] == {
        "value": "回复用中文", "path": "偏好/nested/reply.md", "heading": "preference", "line": 1,
        "kind": "preference"}
    assert compiled.skipped == ({"path": "关于我.md", "reason": "unknown kind"},)
    assert compiled.sources == ("偏好/nested/reply.md", "关于我.md")
    blob = compiled.text + repr(compiled.payload())
    for hidden in ("旧称呼", "神秘", "[kind::", "[source::", "对话 2026-11-20", "`"):
        assert hidden not in blob


def test_a_preference_note_does_not_need_a_summary_when_it_has_entries(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "偏好/reply.md", """---
summary: 不要把摘要再算一条
---

# 回复

""" + _ENTRY + "\n")
    compiled = compile_profile(root)
    assert compiled.fields["name"] is None
    assert compiled.skipped == ()
    assert compiled.fields["communication_preferences"] == [{
        "value": "回复用中文", "path": "偏好/reply.md", "heading": "回复", "line": 7, "kind": "preference"}]
    assert compiled.text == "Communication:\n- 回复用中文"
    assert "不要把摘要再算一条" not in compiled.text


def test_an_all_superseded_preference_note_does_not_revive_its_summary(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "偏好/old.md", """---
summary: 重新启用失效偏好
---

# 回复

- 旧称呼 [kind:: preference] [source:: 对话 2026-11-20] [confidence:: high] [status:: superseded]
- 旧习惯 [kind:: habit] [source:: 对话 2026-11-20] [confidence:: high] [status:: superseded]
""")
    compiled = compile_profile(root)
    assert compiled.fields["communication_preferences"] == []
    assert compiled.fields["standing_rules"] == []
    assert compiled.skipped == ()
    assert compiled.sources == ()
    assert compiled.text == ""
    blob = compiled.text + repr(compiled.payload())
    for hidden in ("重新启用失效偏好", "旧称呼", "旧习惯"):
        assert hidden not in blob


def test_code_fences_are_not_fields_and_backticks_are_normalized(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "关于我.md", """## 名字
Ada

```markdown
## 名字
NotAda
""" + _ENTRY + """
```

~~~
## 称呼
Fenced
~~~

## 称呼
call me `Ada` ~~~ now
""")
    _note(root, "偏好/sample.md", """---
summary: 真实规则
---

# 规则

```
""" + _ENTRY + """
```
""")
    compiled = compile_profile(root)
    assert compiled.fields["name"]["value"] == "Ada"
    assert compiled.fields["address"]["value"] == "call me 'Ada' --- now"
    assert compiled.fields["communication_preferences"] == []
    blob = compiled.text + repr(compiled.payload())
    for hidden in ("NotAda", "Fenced", "回复用中文", "`"):
        assert hidden not in blob
    assert "真实规则" in compiled.text


def test_a_single_value_field_rejects_a_second_bullet(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _note(root, "关于我.md", """## 名字
- Ada
- Bee
""")
    with pytest.raises(ProfileError, match="name has more than one value") as raised:
        compile_profile(root)
    assert raised.value.reason == "multiple_values"
    assert str(root) not in str(raised.value)


_SHARED_NOTE = """# Profile

## Name
Ada Example

## Work
- Builds the Atlas billing service at Example Corp [kind:: fact] [share:: hook] [status:: superseded]
- Builds the Beacon service at Example Corp [kind:: fact]
- Pairs on reviews every Friday [kind:: habit] [share:: hook]

## Time
- UTC+2; read times as local [kind:: fact] [source:: chat 2026-01-02] [share:: hook] [confidence:: high]
- Quiet from 21:00 to 07:00 [kind:: decision] [share:: Hook]

## Talk
- Lead with the conclusion [kind:: preference] [share:: hook]
- Mentions the Gamma team channel [kind:: preference] [share:: hooks]
- Mystery entry [kind:: mystery] [share:: hook]
"""


def _without_markers(text: str) -> str:
    for marker in (" [share:: hook]", " [share:: Hook]", " [share:: hooks]"):
        text = text.replace(marker, "")
    return text


def test_only_inline_entries_marked_for_the_hook_are_shared(tmp_path: Path) -> None:
    _note(tmp_path, "记忆/画像.md", _SHARED_NOTE)
    shared = compile_profile(tmp_path, source="记忆/画像.md", shared_only=True)
    assert shared.text == (
        "Communication:\n"
        "- Pairs on reviews every Friday\n"
        "- Lead with the conclusion\n"
        "Standing rules:\n"
        "- UTC+2; read times as local")
    for hidden in ("Ada Example", "Atlas", "Beacon", "21:00", "Gamma", "Mystery", "[share::"):
        assert hidden not in shared.text + repr(shared.payload())
    assert shared.fields["name"] is None
    assert [entry["line"] for entry in shared.fields["communication_preferences"]] == [9, 16]
    assert shared.sources == ("记忆/画像.md",)


def test_markers_do_not_change_what_the_manager_reads(tmp_path: Path) -> None:
    _note(tmp_path, "marked/画像.md", _SHARED_NOTE)
    _note(tmp_path, "plain/画像.md", _without_markers(_SHARED_NOTE))
    marked = compile_profile(tmp_path, source="marked/画像.md")
    plain = compile_profile(tmp_path, source="plain/画像.md")
    assert marked.text == plain.text
    assert marked.text.startswith("Name: Ada Example\n")
    assert "Beacon" in marked.text and "21:00" in marked.text and "Gamma" in marked.text
    assert repr(marked.payload()).replace("marked/", "") == repr(plain.payload()).replace("plain/", "")


def test_heading_fields_are_never_shared(tmp_path: Path) -> None:
    _note(tmp_path, "记忆/画像.md", "## 时区\nEurope/Berlin\n\n## Communication\n- Short answers [share:: hook]\n")
    assert compile_profile(tmp_path, source="记忆/画像.md").text.startswith("Time zone: Europe/Berlin\n")
    assert compile_profile(tmp_path, source="记忆/画像.md", shared_only=True).text == ""


def test_a_note_without_markers_shares_nothing(tmp_path: Path) -> None:
    _about(tmp_path, "- Lead with the conclusion [kind:: preference]\n")
    shared = compile_profile(tmp_path, shared_only=True)
    assert (shared.text, shared.sources, shared.omitted) == ("", (), ())
    assert compile_profile(tmp_path).text.startswith("Name: Ada\n")


def test_an_invalid_note_shares_nothing_even_when_the_conflict_is_unmarked(tmp_path: Path) -> None:
    _note(tmp_path, "记忆/画像.md", "## Name\nAda\n\n## 名字\nBee\n\n"
          "- Lead with the conclusion [kind:: preference] [share:: hook]\n")
    with pytest.raises(ProfileError, match="two headings for name"):
        compile_profile(tmp_path, source="记忆/画像.md", shared_only=True)
    _note(tmp_path, "记忆/画像.md", "## 名字\n- Ada\n- Bee\n\n"
          "- Lead with the conclusion [kind:: preference] [share:: hook]\n")
    with pytest.raises(ProfileError, match="name has more than one value"):
        compile_profile(tmp_path, source="记忆/画像.md", shared_only=True)


def test_shared_entries_over_the_cap_drop_from_the_end(tmp_path: Path) -> None:
    lines = "".join(f"- Preference {index} {'x' * 80} [kind:: preference] [share:: hook]\n" for index in range(30))
    _note(tmp_path, "记忆/画像.md", lines + "- Unshared rule [kind:: fact]\n")
    shared = compile_profile(tmp_path, source="记忆/画像.md", shared_only=True)
    assert len(shared.text) <= CAP and "Preference 0 " in shared.text and "Preference 29" not in shared.text
    assert shared.omitted and all(item["field"] == "communication_preferences" for item in shared.omitted)
    assert "Unshared" not in repr(shared.payload())
