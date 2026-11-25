"""Project-document migration previews are deterministic and preserve their source."""

import hashlib

import yaml

from looper import cli
from looper.agents import knowledge_migrate


def _document(**fields):
    return {"id": "doc-example", "title": "Guide: original", "summary": "A quoted \"summary\"",
            "source_kind": "text", "filename": "guide.md", "content": "Original.\n", "path": None,
            "sha256": hashlib.sha256(b"Original.\n").hexdigest(), **fields}


def test_preview_includes_exact_frontmatter_and_large_document_flag():
    row = _document(content="x" * 8193, filename="../../guide.md",
                    title="Guide: original\n第二行\u2028😀", summary="Literal\u2029false")
    files, report = knowledge_migrate.prepare([row], "projects/example")
    item = report[0]
    assert item["target"] == "projects/example/doc-example-guide.md"
    data = files[item["target"]]
    frontmatter = yaml.safe_load(data.decode().split("---\n", 2)[1])
    assert frontmatter == item["frontmatter"] == {
        "title": row["title"], "summary": row["summary"], "source_document_id": row["id"],
        "original_sha256": row["sha256"], "source_kind": "text"}
    assert data.endswith(row["content"].encode())
    assert item["size_bytes"] == len(data) and item["over_8kb"] is True
    assert item["sha256"] == hashlib.sha256(data).hexdigest()


def test_path_documents_become_references_without_reading_the_original():
    row = _document(source_kind="path", filename="", content=None, path="docs/unavailable.md")
    files, report = knowledge_migrate.prepare([row], "projects/example")
    item = report[0]
    assert item["reference"] is True and item["original_path"] == row["path"]
    assert item["frontmatter"]["original_path"] == row["path"]
    assert row["path"] in files[item["target"]].decode()


def test_filenames_are_unique_even_for_duplicate_titles_and_case():
    rows = [_document(id="doc-first"), _document(id="doc-second", title="GUIDE")]
    files, report = knowledge_migrate.prepare(rows, "projects/example")
    assert len(files) == 2 and len({item["target"].casefold() for item in report}) == 2


def test_cli_defaults_to_dry_run_and_is_operator_only(monkeypatch, tmp_path, capsys):
    parsed = cli.build_parser().parse_args(["knowledge", "migrate", "--project", "Example"])
    assert parsed.execute is False
    monkeypatch.setenv("LOOPER_AGENT_RUN", "1")
    assert cli.main(["knowledge", "migrate", "--project", "Example", "--runtime-root", str(tmp_path)]) == 4
    assert "操作者" in capsys.readouterr().err
