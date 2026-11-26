"""Source upgrades export web/ without the rest of the repository's tests."""

import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import pytest


# Skip comments and unrelated strings; cover static/side-effect imports,
# re-exports, dynamic imports and CommonJS require calls.
SPACE = r"(?:\s|/\*[\s\S]*?\*/|//[^\n]*)*"
SPECIFIERS = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/"
    rf"|\b(?:from{SPACE}|import{SPACE}(?:\({SPACE})?|require{SPACE}\({SPACE})"
    r"(?P<quote>[\"'`])(?P<specifier>[^\"'`\n]+)(?P=quote)"
    r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
)

READERS = {"readFileSync", "readFile", "createReadStream", "existsSync", "statSync"}
# Tokenize strings as units so comments, examples and nested call arguments do
# not need a separate, increasingly complicated regex for each read form.
TOKENS = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`'
    r'|[a-zA-Z_$][\w$]*|[^\s]'
)
READ_ALLOWLIST = {
    ("src/lib/installable.test.ts", "`${web}/public${icon.src}`"):
        "Icon names come from the local manifest; its metadata test validates the referenced PNGs.",
    ("src/components/managers-settings.test.tsx", "file"):
        "Existing full-checkout documentation parity test, not a standalone build dependency.",
}


def call_arguments(tokens, opening):
    """Split arguments at top-level commas, retaining nested expressions."""
    depth, start, arguments = 0, opening + 1, []
    for index in range(opening, len(tokens)):
        token = tokens[index]
        if token in ("(", "[", "{"):
            depth += 1
        elif token in (")", "]", "}"):
            depth -= 1
            if depth == 0:
                return arguments + [tokens[start:index]], index
        elif token == "," and depth == 1:
            arguments.append(tokens[start:index])
            start = index + 1
    return [], opening  # Malformed or unsupported expressions fail closed.


def path_value(tokens, source, bindings):
    """Evaluate only literals, URL directory bindings and the supported path calls."""
    expression = "".join(tokens)
    if expression == "__dirname":
        return str(source.parent)
    if len(tokens) == 1:
        if expression in bindings:
            return bindings[expression]
        if expression[:1] not in ('"', "'", "`") or "\\" in expression:
            return None
        value = expression[1:-1]
        if expression.startswith("`"):
            value = re.sub(r"\$\{(\w+)\}", lambda m: bindings.get(m[1], m[0]), value)
            if "${" in value:
                return None
        return value
    opening = next((i for i, token in enumerate(tokens) if token == "("), None)
    if opening is None:
        return None
    arguments, closing = call_arguments(tokens, opening)
    if not arguments or closing != len(tokens) - 1:
        return None
    name = "".join(tokens[:opening])
    if name == "newURL" and len(arguments) == 2 and "".join(arguments[1]) == "import.meta.url":
        relative = path_value(arguments[0], source, bindings)
        if relative is not None:
            url = urlsplit(urljoin(source.as_uri(), relative))
            if url.scheme == "file" and not url.netloc:
                return unquote(url.path)
    if name == "fileURLToPath" and len(arguments) == 1:
        return path_value(arguments[0], source, bindings)
    if name in ("path.join", "path.resolve"):
        parts = [path_value(arg, source, bindings) for arg in arguments]
        if parts and all(part is not None for part in parts):
            # join concatenates even absolute later segments; resolve resets at them.
            return posixpath.normpath("/".join(parts)) if name == "path.join" else posixpath.join(*parts)
    return None


def file_reads(text, source):
    tokens = [match[0] for match in TOKENS.finditer(text) if not match[0].startswith(("//", "/*"))]
    bindings = {}
    for index, token in enumerate(tokens):
        tail = tokens[index:index + 7]
        # Only track const directory bases from URL/path calls, not general JS
        # dataflow. Other dynamic reads require an exact, reasoned exception.
        if token == "const" and len(tail) > 4 and tail[2] == "=":
            bindings.pop(tail[1], None)
            opening = next((index + i for i in range(3, len(tail)) if tail[i] == "("), None)
            if opening is not None:
                _, closing = call_arguments(tokens, opening)
                value = path_value(tokens[index + 3:closing + 1], source, bindings)
                if value is not None and tokens[closing + 1:closing + 2] == [";"]:
                    bindings[tail[1]] = value
        if token in READERS and tail[1:2] == ["("]:
            opening, is_read = index + 1, True
        elif tail[:3] == ["new", "URL", "("]:
            opening, is_read = index + 2, False
        elif tail[:3] in (["path", ".", "join"], ["path", ".", "resolve"]) and tail[3:4] == ["("]:
            opening, is_read = index + 3, False
        else:
            continue
        arguments, closing = call_arguments(tokens, opening)
        if not is_read and token == "new" and (
            len(arguments) != 2 or "".join(arguments[1]) != "import.meta.url"
        ):
            continue  # Browser/network URLs are not filesystem dependencies.
        expression = arguments[0] if is_read and arguments else tokens[index:closing + 1]
        yield "".join(expression), path_value(expression, source, bindings)


def specifier_form(text, match):
    """Name the syntactic form of a specifier match. Detection stays in SPECIFIERS."""
    head = text[match.start():match.start("quote")]
    if head.startswith("require"):
        return "require"
    if head.startswith("import"):
        if re.match(rf"import{SPACE}\(", head):
            return "dynamic-import"
        return "side-effect-import"
    statement = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", " ", text[:match.start()])
    statement = statement.rsplit(";", 1)[-1]
    if re.search(r"\bexport\s+\*\s+as\b", statement):
        return "export-star-as"
    if re.search(r"\bexport\s+\*", statement):
        return "export-star"
    if re.search(r"\bexport\s+type\b", statement):
        return "export-type"
    if re.search(r"\bexport\b", statement):
        return "export-from"
    if re.search(r"\bimport\s+type\b", statement):
        return "import-type"
    if re.search(r"\bimport\b", statement):
        return "import-from"
    return "from"


def specifier_shape(specifier):
    """Path class after query and hash stripping, plus which suffix was present."""
    path = re.split(r"[?#]", specifier, maxsplit=1)[0]
    if path.startswith("@/"):
        kind = "alias"
    elif path.startswith("/"):
        kind = "absolute"
    elif path.startswith("."):
        kind = "relative"
    else:
        kind = "bare"
    if "?" in specifier:
        return kind + "+query"
    if "#" in specifier:
        return kind + "+hash"
    return kind


def check_web_sources(web):
    web = web.resolve()
    root = web.parent
    violations = []
    checked = 0
    for source in sorted((web / "src").rglob("*")):
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8", errors="ignore")
        for match in SPECIFIERS.finditer(text):
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
                # Name the specifier line, syntactic form and path shape so each
                # import form can be asserted on its own.
                line = text.count("\n", 0, match.start("specifier")) + 1
                form = specifier_form(text, match)
                violations.append(
                    f"{source.relative_to(root)}:{line}: {form}/{specifier_shape(specifier)}: {specifier}"
                )
        for expression, value in file_reads(text, source):
            checked += 1
            if value is None:
                if (source.relative_to(web).as_posix(), expression) in READ_ALLOWLIST:
                    continue
                violations.append(f"{source.relative_to(root)}: unresolved file read {expression}")
            elif not (web / value).resolve().is_relative_to(web):
                # Node fs relative paths use the standalone web build's cwd;
                # URL and __dirname expressions have already become absolute.
                violations.append(f"{source.relative_to(root)}: file read {expression}")
    return checked, violations


def test_web_source_paths_stay_inside_web():
    checked, violations = check_web_sources(Path(__file__).resolve().parents[1] / "web")
    assert checked, "No web source paths were checked"
    assert not violations, "Paths outside the standalone web build:\n" + "\n".join(violations)


READ_FORMS = ["readFileSync", "readFile", "fs.promises.readFile", "createReadStream", "existsSync", "statSync"]


@pytest.mark.parametrize("expression", [
    'new URL("../../../fixtures/data.json", import.meta.url)',
    *[f'{reader}("../fixtures/data.json")' for reader in READ_FORMS],
    *[f'{reader}(`../fixtures/${{name}}.json`)' for reader in READ_FORMS],
    *[f'path.{method}({base}, "..", "..", "..", "fixtures/data.json")'
      for method in ("join", "resolve")
      for base in ('__dirname', 'fileURLToPath(new URL("./", import.meta.url))')],
    'readFileSync(dynamicPath)',
    'fs.readFileSync(fileURLToPath(new URL("../../../fixtures/data.json", import.meta.url)))',
    'readFileSync(path.join(__dirname, "..", "..", "..", "fixtures/data.json"))',
    'const base = fileURLToPath(new URL("../../", import.meta.url)) + "../fixtures/data.json"; readFile(base)',
])
def test_checker_reports_file_read_outside_web(tmp_path, expression):
    web = tmp_path / "web"
    source = web / "src" / "lib" / "probe.ts"
    source.parent.mkdir(parents=True)
    source.write_text(expression + ";", encoding="utf-8")
    checked, violations = check_web_sources(web)
    assert violations, expression
    assert checked
    assert all("web/src/lib/probe.ts:" in violation for violation in violations)


def test_checker_allows_reads_inside_web(tmp_path):
    web = tmp_path / "web"
    source = web / "src" / "lib" / "probe.ts"
    source.parent.mkdir(parents=True)
    source.write_text('''
        const web = fileURLToPath(new URL("../../", import.meta.url));
        readFileSync(`${web}/index.html`, "utf8");
        readFileSync(`${web}/public/manifest.webmanifest`, "utf8");
        readFile("./public/manifest.webmanifest");
        fs.promises.readFile(`./index.html`);
        new URL("../../public/manifest.webmanifest", import.meta.url);
        path.join(__dirname, "..", "..", "index.html");
        path.resolve(fileURLToPath(new URL("../../", import.meta.url)), "index.html");
        readFileSync(path.join(__dirname, "..", "..", "index.html"));
        readFileSync(new URL("../../index.html", import.meta.url));
        new URL("https://example.com", window.location.origin);
        // readFileSync("../fixtures/comment.json")
        /* new URL("../../../fixtures/comment.json", import.meta.url) */
        const example = 'readFileSync("../fixtures/string.json")';
    ''', encoding="utf-8")
    checked, violations = check_web_sources(web)
    assert checked
    assert violations == []


# One row per form the import matcher covers. `from` is shared by the static
# import, import-type and re-export statements; import(...) and import "..."
# share the import keyword; quotes differ so each quote class is represented.
# ABSOLUTE_* is filled with a real path under or beside the temporary web root.
IMPORT_PROBES = [
    ("import-from", "import-from", "relative",
     'import { widget } from "SPEC";',
     "../../../outside-import-from.js", "./inside-import-from.js"),
    ("side-effect-import", "side-effect-import", "relative",
     "import 'SPEC';",
     "../../../outside-side-effect.js", "./inside-side-effect.js"),
    ("export-from", "export-from", "relative",
     'export { widget } from "SPEC";',
     "../../../outside-export-from.js", "./inside-export-from.js"),
    ("export-star", "export-star", "relative",
     "export * from 'SPEC';",
     "../../../outside-export-star.js", "./inside-export-star.js"),
    ("export-star-as", "export-star-as", "relative",
     'export * as widget from "SPEC";',
     "../../../outside-export-star-as.js", "./inside-export-star-as.js"),
    ("export-type", "export-type", "relative",
     'export type { Widget } from "SPEC";',
     "../../../outside-export-type.js", "./inside-export-type.js"),
    ("dynamic-import", "dynamic-import", "relative",
     "import(`SPEC`);",
     "../../../outside-dynamic.js", "./inside-dynamic.js"),
    ("require", "require", "relative",
     'require("SPEC");',
     "../../../outside-require.js", "./inside-require.js"),
    ("import-type", "import-type", "relative",
     "import type { Widget } from 'SPEC';",
     "../../../outside-import-type.js", "./inside-import-type.js"),
    ("alias", "import-from", "alias",
     'import { widget } from "SPEC";',
     "@/../../outside-alias.js", "@/inside-alias.js"),
    ("absolute", "import-from", "absolute",
     'import { widget } from "SPEC";',
     "ABSOLUTE_OUTSIDE", "ABSOLUTE_INSIDE"),
    ("query", "import-from", "relative+query",
     'import { widget } from "SPEC";',
     "../../../outside-query.js?raw",
     "./inside-query.js?/../../../../outside-query.js"),
    ("hash", "import-from", "relative+hash",
     'import { widget } from "SPEC";',
     "../../../outside-hash.js#section",
     "./inside-hash.js#/../../../../outside-hash.js"),
    ("import-from-comment", "import-from", "relative",
     'import { widget } from /* boundary */ "SPEC";',
     "../../../outside-comment.js", "./inside-comment.js"),
]


def write_import_probe(tmp_path, template, token):
    web = tmp_path / "web"
    source = web / "src" / "lib" / "probe.ts"
    source.parent.mkdir(parents=True)
    if token == "ABSOLUTE_OUTSIDE":
        spec = (tmp_path / "outside.js").resolve().as_posix()
    elif token == "ABSOLUTE_INSIDE":
        spec = (web / "src" / "inside.js").resolve().as_posix()
    else:
        spec = token
    source.write_text("// boundary\n" + template.replace("SPEC", spec) + "\n", encoding="utf-8")
    return web, spec


@pytest.mark.parametrize("probe", IMPORT_PROBES, ids=[probe[0] for probe in IMPORT_PROBES])
def test_checker_reports_import_outside_web(tmp_path, probe):
    _name, form, shape, template, outside, _inside = probe
    web, spec = write_import_probe(tmp_path, template, outside)
    checked, violations = check_web_sources(web)
    assert checked == 1
    assert violations == [f"web/src/lib/probe.ts:2: {form}/{shape}: {spec}"]
    assert spec in (web / "src" / "lib" / "probe.ts").read_text(encoding="utf-8").splitlines()[1]


@pytest.mark.parametrize("probe", IMPORT_PROBES, ids=[probe[0] for probe in IMPORT_PROBES])
def test_checker_allows_import_inside_web(tmp_path, probe):
    _name, _form, _shape, template, _outside, inside = probe
    web, spec = write_import_probe(tmp_path, template, inside)
    checked, violations = check_web_sources(web)
    assert checked == 1
    assert violations == []
    assert spec in (web / "src" / "lib" / "probe.ts").read_text(encoding="utf-8").splitlines()[1]


def test_checker_ignores_non_source_import_specifiers(tmp_path):
    web = tmp_path / "web"
    source = web / "src" / "lib" / "probe.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join([
            'import "react";',
            'import "@scope/pkg";',
            'import "virtual:outside";',
            'import "https://example.com/outside.js";',
            'import "file:///etc/passwd";',
            'import { widget } from "pkg/../../../../outside.js";',
            'import "pkg/../../../../outside.js";',
            'export * from "pkg/../../../../outside.js";',
            'import("pkg/../../../../outside.js");',
            'require("pkg/../../../../outside.js");',
            '// import { widget } from "../../../outside.js";',
            '/* require("../../../outside.js"); export * from "../../../outside.js"; */',
            '''const sample = "import('../../../outside.js')";''',
            'const other = `import("../../../outside.js")`;',
            "",
        ]),
        encoding="utf-8",
    )
    checked, violations = check_web_sources(web)
    assert checked == 10
    assert violations == []
