"""Byte-preserving replay and the write-time target boundary, using synthetic notes only."""

import hashlib

import pytest

from looper.agents import memory_model as model, memory_write as writer, vault


@pytest.mark.parametrize(("change", "before", "after"), [
    ({"op": "append_line", "under": "## Index", "line": "new"},
     b"# Notes\r\n## Index\r\nold\r\n\r\n## Other\r\nkeep\r\n",
     b"# Notes\r\n## Index\r\nold\r\nnew\r\n\r\n## Other\r\nkeep\r\n"),
    ({"op": "append_line", "under": None, "line": "new"}, b"old", b"old\nnew\n"),
    ({"op": "append_line", "under": "## Index", "line": "old"}, b"## Index\nold\n", b"## Index\nold\n"),
    ({"op": "replace_line", "old": "old", "new": "new"}, b"# Notes\r\nold", b"# Notes\r\nnew"),
])
def test_line_operations_preserve_every_other_byte(tmp_path, change, before, after):
    (tmp_path / "note.md").write_bytes(before)
    result, original = writer.replay(tmp_path, [{**change, "path": "note.md"}])
    assert result == {"note.md": after} and original == {"note.md": before}
    assert (tmp_path / "note.md").read_bytes() == before  # previews never write


@pytest.mark.parametrize("change,body,reason", [
    ({"op": "create_note", "content": "new"}, "old", "base_changed"),
    ({"op": "replace_note", "content": "new", "base_sha256": "0" * 64}, "old", "base_changed"),
    ({"op": "append_line", "under": "## Missing", "line": "new"}, "old", "anchor_missing"),
    ({"op": "replace_line", "old": "old", "new": "new"}, "old\nold\n", "anchor_missing"),
])
def test_replay_conflicts_never_write(tmp_path, change, body, reason):
    path = tmp_path / "note.md"
    path.write_text(body)
    with pytest.raises(writer.WriteFailure) as failure:
        writer.replay(tmp_path, [{**change, "path": "note.md"}])
    assert (failure.value.state, failure.value.reason) == ("conflict", reason)
    assert path.read_text() == body


def test_replace_note_checks_the_whole_base(tmp_path):
    body = b"old\r\n"
    (tmp_path / "note.md").write_bytes(body)
    result, _ = writer.replay(tmp_path, [{"op": "replace_note", "path": "note.md", "content": "new\n",
                                       "base_sha256": hashlib.sha256(body).hexdigest()}])
    assert result["note.md"] == b"new\n"


@pytest.mark.parametrize("path", ["../outside.md", "mine/note.md", "mem/AGENTS.md", "mem/prefs/AGENTS.md",
                                  "mem/prefs/link.md", "mem/prefs/a b.md"])
def test_write_boundary_rejects_escape_rules_links_and_unconfigured_targets(tmp_path, path):
    (tmp_path / "mem/prefs").mkdir(parents=True)
    (tmp_path / "mine").mkdir()
    (tmp_path / "mem/AGENTS.md").write_text("rules")
    (tmp_path / "mem/prefs/_index.md").write_text("## Index\n")
    (tmp_path / "mem/prefs/link.md").symlink_to(tmp_path / "mem/AGENTS.md")
    config = vault.VaultConfig(tmp_path, (), "mem/AGENTS.md")
    settings = model.check_settings({"root": "mem", "rules_files": ["mem/AGENTS.md"], "targets": {
        "preference": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"}}}, config)
    with pytest.raises(ValueError):
        writer.validate_changes(config, settings, "preference", [
            {"op": "create_note", "path": path, "content": "---\nkind: memory\n---\ntext\n"},
            {"op": "append_line", "path": "mem/prefs/_index.md", "under": "## Index", "line": "- note"}])
