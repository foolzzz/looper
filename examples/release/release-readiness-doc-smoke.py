#!/usr/bin/env python3
"""Validate a GitHub release body: its bilingual release decision and capability usage.

The release procedure and the rules this enforces are in docs/releases/README.md.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import tempfile


ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_PUBLIC_STRINGS = [
    "/Users/",
    "/private/tmp/",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "raw_thread",
    "session_history",
    "verifier_output_tail",
    "ACTIVE_GOAL_STATE.md:",
]

ENGLISH_USAGE_HEADING = "## Optional Capability Activation & Use"
CHINESE_USAGE_HEADING = "### 可选能力启用与使用"
ENGLISH_DECISION_HEADING = "## Release Decision"
CHINESE_DECISION_HEADING = "### 升级决策"
ENGLISH_DECISION_FIELDS = (
    "**Who should upgrade:**",
    "**What this release solves:**",
    "**Breaking changes:**",
    "**How to verify:**",
    "**Contributors:**",
)
CHINESE_DECISION_FIELDS = (
    "**谁需要升级：**",
    "**解决了什么：**",
    "**是否有破坏性变更：**",
    "**如何验证：**",
    "**贡献者：**",
)
ENGLISH_NO_CHANGES = "No new optional capability activation is introduced in this release."
CHINESE_NO_CHANGES = "本版本未新增可选能力启用入口。"
ENGLISH_USAGE_FIELDS = (
    "**Activation:**",
    "**Validation:**",
    "**Disable / rollback:**",
    "**Authority boundary:**",
    "**Docs:**",
)
CHINESE_USAGE_FIELDS = (
    "**启用：**",
    "**验证：**",
    "**停用 / 回退：**",
    "**权限边界：**",
    "**文档：**",
)


def read(path: Path) -> str:
    if not path.exists():
        raise AssertionError(f"missing expected file: {path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def compact(text: str) -> str:
    return " ".join(text.split())


def assert_contains(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} missing {needle!r}")


def validate_boundary(path: Path) -> None:
    text = read(path)
    try:
        label = str(path.relative_to(ROOT))
    except ValueError:
        label = path.name
    for forbidden in FORBIDDEN_PUBLIC_STRINGS:
        if forbidden in text:
            raise AssertionError(f"{label} contains forbidden string {forbidden!r}")


def heading_level(line: str) -> int | None:
    match = re.match(r"^(#+)\s+", line)
    return len(match.group(1)) if match else None


def section(text: str, heading: str) -> str:
    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise AssertionError(f"release notes missing heading {heading!r}") from exc

    level = heading_level(heading)
    assert level is not None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        candidate_level = heading_level(lines[index])
        if candidate_level is not None and candidate_level <= level:
            end = index
            break
    return "\n".join(lines[start + 1 : end])


def validate_surface_entry(
    *,
    usage_section: str,
    heading: str,
    required_fields: tuple[str, ...],
    language: str,
) -> None:
    entry = section(usage_section, heading)
    for field in required_fields:
        assert_contains(entry, field, f"{language} usage entry {heading!r}")
    if "```bash" not in entry:
        raise AssertionError(f"{language} usage entry {heading!r} has no runnable bash example")
    docs_field = re.escape(required_fields[-1])
    if not re.search(rf"{docs_field}[^\n]*https://[^)\s]+", entry):
        raise AssertionError(
            f"{language} usage entry {heading!r} has no canonical link in its docs field"
        )


def decision_field_value(decision: str, field: str, language: str) -> str:
    match = re.search(rf"^{re.escape(field)}\s*(.+)$", decision, re.MULTILINE)
    if not match:
        raise AssertionError(f"{language} release decision missing a value for {field!r}")
    value = match.group(1).strip()
    if len(value) < 8 or "<" in value or ">" in value:
        raise AssertionError(
            f"{language} release decision has an empty or placeholder value for {field!r}"
        )
    return value


def validate_release_decision(body: str) -> None:
    english = section(body, ENGLISH_DECISION_HEADING)
    english_values = {
        field: decision_field_value(english, field, "English")
        for field in ENGLISH_DECISION_FIELDS
    }
    breaking = english_values["**Breaking changes:**"].casefold()
    if not re.match(r"^(no|yes)\b", breaking):
        raise AssertionError("English breaking decision must start with 'No.' or 'Yes.'")
    contributors = english_values["**Contributors:**"].casefold()
    if "@" not in contributors and "no community contribution" not in contributors:
        raise AssertionError(
            "English contributors decision must name a handle or explicitly state "
            "that there was no community contribution"
        )
    if "```bash" not in english:
        raise AssertionError("English release decision has no runnable verification block")

    chinese_summary = section(body, "## 中文摘要")
    chinese = section(chinese_summary, CHINESE_DECISION_HEADING)
    chinese_values = {
        field: decision_field_value(chinese, field, "Chinese")
        for field in CHINESE_DECISION_FIELDS
    }
    breaking_zh = chinese_values["**是否有破坏性变更：**"]
    if not breaking_zh.startswith(("无", "有", "否", "是")):
        raise AssertionError("Chinese breaking decision must start with 无/有/否/是")
    contributors_zh = chinese_values["**贡献者：**"]
    if "@" not in contributors_zh and not any(
        phrase in contributors_zh for phrase in ("无社区贡献", "没有社区贡献")
    ):
        raise AssertionError(
            "Chinese contributors decision must name a handle or explicitly state "
            "that there was no community contribution"
        )


def validate_release_notes(
    path: Path,
    *,
    surfaces: list[str],
    expect_no_optional_capability_changes: bool,
) -> None:
    validate_boundary(path)
    body = read(path)
    if body.count("```") % 2:
        raise AssertionError("release notes contain an unbalanced fenced code block")

    validate_release_decision(body)
    english = section(body, ENGLISH_USAGE_HEADING)
    chinese_summary = section(body, "## 中文摘要")
    chinese = section(chinese_summary, CHINESE_USAGE_HEADING)

    if expect_no_optional_capability_changes:
        if surfaces:
            raise AssertionError(
                "--expect-no-optional-capability-changes cannot be combined with --surface"
            )
        assert_contains(english, ENGLISH_NO_CHANGES, "English no-change declaration")
        assert_contains(chinese, CHINESE_NO_CHANGES, "Chinese no-change declaration")
        return

    if not surfaces:
        raise AssertionError(
            "release-note validation requires at least one --surface or "
            "--expect-no-optional-capability-changes"
        )

    if len(set(surfaces)) != len(surfaces):
        raise AssertionError("release-note validation received duplicate --surface values")

    for surface in surfaces:
        validate_surface_entry(
            usage_section=english,
            heading=f"### {surface}",
            required_fields=ENGLISH_USAGE_FIELDS,
            language="English",
        )
        validate_surface_entry(
            usage_section=chinese,
            heading=f"#### {surface}",
            required_fields=CHINESE_USAGE_FIELDS,
            language="Chinese",
        )


def validate_release_notes_gate_self_test() -> None:
    def fixture(english_usage: str, chinese_usage: str) -> str:
        return f"""# Example

{ENGLISH_DECISION_HEADING}
**Who should upgrade:** Operators affected by the scheduler fix should upgrade now.
**What this release solves:** It prevents a due monitor from missing its next wake.
**Breaking changes:** No. Existing persisted state remains compatible.
**How to verify:** Run the following identity and behavior checks after updating.
**Contributors:** Maintainer @example prepared this release; no community contribution was included.
```bash
looper --version
looper doctor
```

{ENGLISH_USAGE_HEADING}

{english_usage}

## Validation

## 中文摘要

{CHINESE_DECISION_HEADING}
**谁需要升级：**受 scheduler 修复影响的 operator 应立即升级。
**解决了什么：**本版本避免到期 monitor 错过下一次唤醒。
**是否有破坏性变更：**无。已有持久状态保持兼容。
**如何验证：**升级后运行上方 identity 与 behavior 检查。
**贡献者：**由 maintainer @example 准备，本版本无社区贡献。

{CHINESE_USAGE_HEADING}

{chinese_usage}

### 验证
"""
    valid = fixture(
        """### Example Surface
**Activation:** Enable the example.
**Validation:** Read it back.
**Disable / rollback:** Disable the example.
**Authority boundary:** No authority is granted.
**Docs:** https://example.com/docs.
```bash
looper example --check
```""",
        """#### Example Surface
**启用：**启用示例。
**验证：**读回示例。
**停用 / 回退：**停用示例。
**权限边界：**不授予额外权限。
**文档：**https://example.com/docs。
```bash
looper example --check
```""",
    )
    no_changes = fixture(ENGLISH_NO_CHANGES, CHINESE_NO_CHANGES)

    def assert_rejected(path: Path, body: str, message: str) -> None:
        path.write_text(body, encoding="utf-8")
        try:
            validate_release_notes(
                path,
                surfaces=["Example Surface"],
                expect_no_optional_capability_changes=False,
            )
        except AssertionError:
            return
        raise AssertionError(message)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        valid_path = tmp_path / "valid.md"
        no_changes_path = tmp_path / "no-changes.md"
        valid_path.write_text(valid, encoding="utf-8")
        no_changes_path.write_text(no_changes, encoding="utf-8")
        validate_release_notes(
            valid_path,
            surfaces=["Example Surface"],
            expect_no_optional_capability_changes=False,
        )
        validate_release_notes(
            no_changes_path,
            surfaces=[],
            expect_no_optional_capability_changes=True,
        )
        assert_rejected(
            tmp_path / "missing-rollback.md",
            valid.replace("**Disable / rollback:**", "", 1),
            "release-note gate accepted a missing rollback field",
        )
        assert_rejected(
            tmp_path / "ambiguous-breaking.md",
            valid.replace(
                "**Breaking changes:** No.",
                "**Breaking changes:** Review compatibility notes.",
                1,
            ),
            "release-note gate accepted an ambiguous breaking decision",
        )
        assert_rejected(
            tmp_path / "missing-contributors.md",
            valid.replace(
                "**Contributors:** Maintainer @example prepared this release; "
                "no community contribution was included.",
                "**Contributors:** See the commit history.",
                1,
            ),
            "release-note gate accepted an unspecified contributor decision",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release-notes",
        type=Path,
        help="Validate an exact final GitHub release body stored in this file.",
    )
    parser.add_argument(
        "--surface",
        action="append",
        default=[],
        help="New or materially changed optional capability/workflow/host surface. Repeatable.",
    )
    parser.add_argument(
        "--expect-no-optional-capability-changes",
        action="store_true",
        help="Require explicit bilingual declarations that this release adds no optional surface.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    validate_release_notes_gate_self_test()
    if args.release_notes is not None:
        validate_release_notes(
            args.release_notes,
            surfaces=args.surface,
            expect_no_optional_capability_changes=(
                args.expect_no_optional_capability_changes
            ),
        )
    elif args.surface or args.expect_no_optional_capability_changes:
        raise AssertionError(
            "--surface and --expect-no-optional-capability-changes require --release-notes"
        )

    print("release-readiness-doc-smoke ok")


if __name__ == "__main__":
    main()
