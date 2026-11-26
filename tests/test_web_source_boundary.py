"""Source upgrades export web/ without the rest of the repository's tests."""

import re
from pathlib import Path


# Skip comments and unrelated strings; cover static/side-effect imports,
# re-exports, dynamic imports and CommonJS require calls.
SPACE = r"(?:\s|/\*[\s\S]*?\*/|//[^\n]*)*"
SPECIFIERS = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/"
    rf"|\b(?:from{SPACE}|import{SPACE}(?:\({SPACE})?|require{SPACE}\({SPACE})"
    r"(?P<quote>[\"'`])(?P<specifier>[^\"'`\n]+)(?P=quote)"
    r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
)


def test_web_source_imports_stay_inside_web():
    root = Path(__file__).resolve().parents[1]
    web = root / "web"
    violations = []
    checked = 0
    for source in sorted((web / "src").rglob("*")):
        if not source.is_file():
            continue
        for match in SPECIFIERS.finditer(source.read_text(encoding="utf-8", errors="ignore")):
            specifier = match["specifier"]
            if specifier is None:
                continue
            checked += 1
            path = re.split(r"[?#]", specifier, maxsplit=1)[0]
            if path.startswith("@/"):
                target = web / "src" / path[2:]
            elif path.startswith((".", "/")):
                target = source.parent / path
            else:
                continue  # Bare package and virtual module specifiers are not source paths.
            if not target.resolve().is_relative_to(web):
                violations.append(f"{source.relative_to(root)}: {specifier}")
    assert checked, "No web source imports were checked"
    assert not violations, "Imports outside the standalone web build:\n" + "\n".join(violations)
