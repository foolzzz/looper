"""Attachments are private, content-checked files, never client-provided paths."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
from typing import Any

import pytest

from looper.agents import chat_attachments as files
from looper.agents.runtimes import ImageInput


@pytest.mark.parametrize("raw,expected", [
    ("../../a.txt", "a.txt"), (r"..\a.txt", "a.txt"), (".env", "env"),
    (" \x00a\n\tb?.txt", "ab_.txt"), ("a" * 300 + ".pdf", "a" * 96 + ".pdf"),
    ("", "fallback"), ("./..", "fallback"), ("报告 ①.txt", "报告 ①.txt"),
])
def test_safe_name(raw, expected):
    assert files.safe_name(raw, "fallback") == expected


@pytest.mark.parametrize("name,content,expected", [
    ("photo.bin", b"\x89PNG\r\n\x1a\n", (files.FileClass.IMAGE, "image/png")),
    ("photo", b"\xff\xd8\xff", (files.FileClass.IMAGE, "image/jpeg")),
    ("photo", b"GIF87a", (files.FileClass.IMAGE, "image/gif")),
    ("photo", b"GIF89a", (files.FileClass.IMAGE, "image/gif")),
    ("photo", b"RIFF1234WEBP", (files.FileClass.IMAGE, "image/webp")),
    ("report", b"%PDF-1.7", (files.FileClass.PDF, "application/pdf")),
    ("report.docx", b"PK\x03\x04", (files.FileClass.OFFICE,
     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
    ("report.xlsx", b"PK\x03\x04", (files.FileClass.OFFICE,
     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
    ("report.pptx", b"PK\x03\x04", (files.FileClass.OFFICE,
     "application/vnd.openxmlformats-officedocument.presentationml.presentation")),
    ("report.zip", b"PK\x03\x04", None),
    ("notes.md", "中文".encode(), (files.FileClass.TEXT, "text/markdown")),
    ("notes.unknown", b"hello", (files.FileClass.TEXT, "text/plain")),
    ("photo.png", b"hello", (files.FileClass.TEXT, "text/plain")),
    ("cut.txt", b"a" * (65536 - 1) + "中".encode(), (files.FileClass.TEXT, "text/plain")),
    ("latin.txt", b"caf\xe9", None), ("nul.txt", b"a\x00b", None),
    ("program.exe", b"MZ", None), ("program", b"\x7fELF", None),
    ("bad.txt", b"a" * 65535 + b"\xff", None),
])
def test_classify(tmp_path, name, content, expected):
    path = tmp_path / name
    path.write_bytes(content)
    assert files.classify(path) == expected


def test_store_private_content_and_replace_redelivery(tmp_path):
    previous = os.umask(0o022)
    try:
        saved = files.store_upload(tmp_path, "web/unsafe", "message.id", [
            ("../../a.txt", b"hello"), ("drawing", b"\x89PNG\r\n\x1a\n"),
            ("report.docx", b"PK\x03\x04"),
        ])
    finally:
        os.umask(previous)
    for attachment in saved:
        assert attachment.state is files.AttachmentState.SAVED
        assert stat.S_IMODE(Path(attachment.path).stat().st_mode) == 0o600
    path = Path(saved[0].path)
    assert path.name == "1-a.txt"
    assert Path(saved[1].path).name == "2-drawing.png"
    assert saved[1].kind is files.AttachmentKind.IMAGE
    assert saved[2].mime.endswith("wordprocessingml.document")
    assert path.parent == files.root(tmp_path) / "web_unsafe" / "message_id"
    for directory in (path.parent, path.parent.parent, files.root(tmp_path)):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    directory = files.message_dir(tmp_path, "web/unsafe", "message.id")
    assert directory == path.parent and list(directory.iterdir()) == []


@pytest.mark.parametrize("content,kind,maximum,state,reason", [
    (b"hello", files.AttachmentKind.FILE, 4, files.AttachmentState.TOO_LARGE, None),
    (b"hello", files.AttachmentKind.IMAGE, 10, files.AttachmentState.NOT_ACCEPTED,
     "images must be PNG, JPEG, GIF or WEBP"),
    (b"MZ", files.AttachmentKind.FILE, 10, files.AttachmentState.NOT_ACCEPTED,
     "only images, text, PDF and Office files are accepted"),
])
def test_accept_refusal_deletes_part(tmp_path, content, kind, maximum, state, reason):
    part = tmp_path / "1.part"
    part.write_bytes(content)
    item = files.accept(part, index=1, kind=kind, name="file", source="web", max_bytes=maximum)
    assert item.state is state
    if reason:
        assert item.reason == reason
    assert not part.exists()
    assert item.path is None


@pytest.mark.parametrize("raw", [None, {}, "old", [None, {}, {"name": "old", "path": "somewhere"}],
    [{"index": 1, "kind": "video", "state": "saved", "name": "a"}],
    [{"index": True, "kind": "image", "state": "saved", "name": "a"}],
    [{"index": 1, "kind": "image", "state": "unknown", "name": "a"}],
    [{"index": 1, "kind": "file", "state": "failed", "name": 1}],
])
def test_old_or_malformed_payload_is_ignored(tmp_path, raw):
    assert files.from_payload(raw, tmp_path) == []


def test_payload_readback_confines_paths_and_survives_retention(tmp_path):
    saved = files.store_upload(tmp_path, "web", "message", [("a.txt", b"hello")])[0]
    directory = Path(saved.path).parent
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (directory / "link").symlink_to(outside)
    paths = [str(outside), str(directory / "link"), str(directory), str(directory / "missing"), None, 5]
    for path in paths:
        raw = {**saved.to_payload(), "path": path}
        item, = files.from_payload([raw], tmp_path)
        assert item.state is files.AttachmentState.FAILED
        assert item.reason == "no longer available" and item.path is None
    second = replace(saved, index=2)
    assert files.from_payload([second.to_payload(), saved.to_payload()], tmp_path) == [saved, second]
    Path(saved.path).unlink()
    item, = files.from_payload([saved.to_payload()], tmp_path)
    assert item.state is files.AttachmentState.FAILED


@pytest.mark.parametrize("image_input,last", [
    (ImageInput.READ_TOOL, "Open one with your file-reading tool when the message needs it."),
    (ImageInput.NONE, "Open a file with your file-reading tool when the message needs it; your runtime cannot "
     "view images, so if the user asks what an image shows, say you cannot see it."),
])
def test_section(image_input, last):
    items = [files.Attachment(1, files.AttachmentKind.IMAGE, files.AttachmentState.SAVED, 'screen"shot.png',
                             "web", path="/attachment.png", mime="image/png", size=251187),
             files.Attachment(2, files.AttachmentKind.FILE, files.AttachmentState.SAVED, "notes.txt", "web",
                              path="/notes.txt", mime="text/plain", size=1229),
             files.Attachment(3, files.AttachmentKind.IMAGE, files.AttachmentState.TOO_LARGE, "photo", "web", told=True)]
    assert files.section(items, image_input) == [
        "Attachments sent with this message (kept on this machine for 7 days; never put their paths in a reply):",
        '  1. image "screen\'shot.png" (image/png, 245.3 KB): /attachment.png',
        '  2. file "notes.txt" (text/plain, 1.2 KB): /notes.txt',
        '  3. image "photo": not received (larger than 20 MB); the user was told', last]
    assert files.section([], image_input) == []


@pytest.mark.parametrize("state,reason,expected", [
    (files.AttachmentState.FAILED, None, "the download failed"),
    (files.AttachmentState.FAILED, "no longer available", "no longer available"),
    (files.AttachmentState.NOT_ACCEPTED, "unsupported file", "unsupported file"),
    (files.AttachmentState.OVER_LIMIT, None, "more than 10 attachments"),
])
def test_section_failures(state, reason, expected):
    item = files.Attachment(1, files.AttachmentKind.FILE, state, "file", "web", reason=reason)
    assert files.section([item], ImageInput.NONE)[1] == f'  1. file "file": not received ({expected})'


@pytest.mark.parametrize("size,label", [(12, "12 B"), (1024, "1.0 KB"), (1572864, "1.5 MB")])
def test_section_sizes(size, label):
    item = files.Attachment(1, files.AttachmentKind.FILE, files.AttachmentState.SAVED, "file", "web",
                            path="/file", mime="text/plain", size=size)
    assert f"text/plain, {label}" in files.section([item], ImageInput.READ_TOOL)[1]


def test_prune_directory_age_and_never_follow_symlinks(tmp_path):
    now = datetime.now(timezone.utc)
    assert files.prune(tmp_path, now) == 0
    old = files.message_dir(tmp_path, "web", "old")
    fresh = files.message_dir(tmp_path, "web", "fresh")
    (fresh / "old-file").write_text("keep")
    earlier = (now - timedelta(days=8)).timestamp()
    os.utime(fresh / "old-file", (earlier, earlier))
    os.utime(old, (earlier, earlier))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (files.root(tmp_path) / "source-link").symlink_to(outside, target_is_directory=True)
    (fresh.parent / "message-link").symlink_to(outside, target_is_directory=True)
    assert files.prune(tmp_path, now) == 1
    assert not old.exists() and fresh.exists() and (outside / "keep").exists()
    assert not (files.root(tmp_path) / "source-link").is_symlink()
    assert not (fresh.parent / "message-link").is_symlink()


def test_prune_continues_after_directory_error(tmp_path, monkeypatch, caplog):
    now = datetime.now(timezone.utc)
    first = files.message_dir(tmp_path, "web", "first")
    second = files.message_dir(tmp_path, "web", "second")
    earlier = (now - timedelta(days=8)).timestamp()
    for directory in (first, second):
        os.utime(directory, (earlier, earlier))
    original = files.shutil.rmtree
    def remove(path):
        if path == first:
            raise PermissionError("denied")
        original(path)
    monkeypatch.setattr(files.shutil, "rmtree", remove)
    assert files.prune(tmp_path, now) == 1
    assert first.exists() and not second.exists()
    assert "denied" in caplog.text


def test_redact(tmp_path):
    prefix = str(files.root(tmp_path))
    text = f'{prefix}/web/a/1.png) [{prefix}/web/a/2.txt] `{prefix}/web/b/3.pdf` "{prefix}/web/c/4.png" end'
    assert files.redact(text, tmp_path) == '[attachment]) [[attachment]] `[attachment]` "[attachment]" end'
    assert files.redact("unrelated", tmp_path) == "unrelated"


def test_only_claude_code_claims_image_read_tool_support():
    from looper.agents.runtimes import ADAPTERS, ImageInput
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime

    assert ClaudeCodeRuntime.image_input is ImageInput.READ_TOOL
    assert {name: runtime.image_input for name, runtime in ADAPTERS.items()} == {
        name: ImageInput.READ_TOOL if name == "claude-code" else ImageInput.NONE for name in ADAPTERS}


@pytest.mark.parametrize("mode", ["real", "fake"])
def test_attachment_dialog_sends_inline_files_and_checks_the_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    import base64
    import struct
    import zlib

    from evals.prompt_regression import dialogs

    sent: list[dict[str, Any]] = []
    reply = "已收到" if mode == "fake" else "口令是青松-8213，图片数字是4937。"

    class Api:
        def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            if path == "/api/v1/tasks/page":
                return {"result": {"tasks": []}}
            if path == "/api/v1/operator_chat/send":
                sent.append(body)
                return {}
            assert path == "/api/v1/operator_chat/messages"
            return {"result": {"messages": [{"message_id": sent[-1]["message_id"] + ":reply", "content": reply}]}}

    monkeypatch.setattr(dialogs, "_newest_run", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_check_manager_run", lambda *_, **__: None)
    monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *_: [])
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda *_: None)
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text("{}")
    (dialog,) = dialogs.select(["attachment_read"])
    row = dialogs.run_dialog(Api(), dialog, database_url="disposable", project_id="project-test",
                             scenarios=scenarios, mode=mode, timeout=1)
    assert row["outcome"] == "pass", row
    assert len(sent) == 1
    text, image = sent[0]["attachments"]
    assert text["filename"] == "notes.txt"
    assert base64.b64decode(text["content_base64"], validate=True).decode() == "口令：青松-8213\n"
    assert image["filename"] == "number.png"
    png = base64.b64decode(image["content_base64"], validate=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", png[16:29])
    assert width >= 250 and height >= 100
    assert (depth, color, compression, filtering, interlace) == (8, 0, 0, 0, 0)
    pixels = bytearray()
    offset = 8
    while offset < len(png):
        length = struct.unpack(">I", png[offset:offset + 4])[0]
        kind, data = png[offset + 4:offset + 8], png[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", png[offset + 8 + length:offset + 12 + length])[0]
        assert crc == zlib.crc32(kind + data)
        if kind == b"IDAT":
            pixels.extend(zlib.decompress(data))
        offset += length + 12
    assert len(pixels) == height * (width + 1)
    assert set(pixels) == {0, 255}


@pytest.mark.parametrize(("fake", "reply", "passes"), [
    (False, "口令青松-8213，数字4937", True),
    (False, "口令青松-8213", False),
    (False, "数字4937", False),
    (False, "", False),
    (True, "已处理", True),
    (True, "", False),
])
def test_attachment_dialog_requires_both_contents_in_real_mode(fake: bool, reply: str, passes: bool) -> None:
    from evals.prompt_regression import dialogs

    look = dialogs.Look(None, None, reply, "disposable", 0, frozenset(), fake)
    assert (dialogs._attachments_read(look) is None) is passes


@pytest.mark.parametrize("name,content,stored", [
    ("x.png", b"hello", "x.png.txt"), ("x.txt", b"\x89PNG\r\n\x1a\n", "x.txt.png"),
    ("x.jpg", b"\x89PNG\r\n\x1a\n", "x.jpg.png"), ("x.txt", b"hello", "x.txt"),
])
def test_stored_extension_follows_content(tmp_path, name, content, stored):
    item, = files.store_upload(tmp_path, "web", "message", [(name, content)])
    assert Path(item.path).name == "1-" + stored
