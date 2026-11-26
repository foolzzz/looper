"""Local sessions read only marked profile entries; the Manager's resident text keeps everything."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from looper.agents import operator_profile, profile_commands, vault

NOTE = """## Name
Ada Example

- Builds the Beacon service at Example Corp [kind:: fact]
- Lead with the conclusion [kind:: preference] [share:: hook]
"""


def _vault(tmp_path: Path, monkeypatch, text: str) -> None:
    (tmp_path / "记忆").mkdir()
    (tmp_path / "记忆" / "画像.md").write_text(text, encoding="utf-8")

    async def load():
        return SimpleNamespace(path=tmp_path, profile_source="记忆/画像.md"), None

    monkeypatch.setattr(vault, "load", load)


def test_session_text_is_the_marked_subset_and_resident_text_is_unchanged(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch, NOTE)
    assert asyncio.run(operator_profile.resident_text()) == (
        "Name: Ada Example\nCommunication:\n- Lead with the conclusion\n"
        "Standing rules:\n- Builds the Beacon service at Example Corp")
    assert asyncio.run(operator_profile.session_text()) == "Communication:\n- Lead with the conclusion"


def test_session_text_is_empty_when_the_note_cannot_compile(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch, "## Name\nAda\n\n## 名字\nBee\n\n" + NOTE.split("\n", 3)[3])
    assert asyncio.run(operator_profile.session_text()) == ""
    assert asyncio.run(operator_profile.resident_text()) == ""


def test_the_local_reader_asks_for_the_session_text(monkeypatch):
    from looper import ops_config
    from looper.db import connection

    class Pool:
        def terminate(self):
            pass

    async def init_pool(*_args, **_kwargs):
        return Pool()

    async def session_text():
        return "SESSION"

    async def resident_text():
        return "RESIDENT"

    monkeypatch.setattr(ops_config, "load", lambda _flags: SimpleNamespace(database_url="postgresql://unused"))
    monkeypatch.setattr(connection, "init_pool", init_pool)
    monkeypatch.setattr(operator_profile, "session_text", session_text)
    monkeypatch.setattr(operator_profile, "resident_text", resident_text)
    args = SimpleNamespace(runtime_root=None, config=None)
    assert asyncio.run(profile_commands._read(args)) == "SESSION"
