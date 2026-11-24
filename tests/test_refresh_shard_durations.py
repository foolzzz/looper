"""Refresh timing weights from all six shard reports without contacting GitHub."""

import json
from pathlib import Path

import pytest

from scripts import refresh_shard_durations as refresh


def test_refresh_downloads_all_shards_and_sums_total_time(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(refresh, "REPO_ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    commands = []

    def download(command, **kwargs):
        commands.append(command)
        directory = Path(command[command.index("--dir") + 1])
        for shard in range(1, 7):
            artifact = directory / f"agent-shard-junit-{shard}-of-6"
            artifact.mkdir()
            (artifact / "agent-shard-junit.xml").write_text(
                '<testsuites><testsuite>'
                f'<testcase file="tests/test_{shard}.py" time="1.24"/>'
                f'<testcase file="tests/test_{shard}.py" time="2.34"><failure/></testcase>'
                '</testsuite></testsuites>')

    monkeypatch.setattr(refresh.subprocess, "run", download)
    refresh.main(["12345"])
    assert commands[0][:4] == ["gh", "run", "download", "12345"]
    assert "foolzzz/looper" in commands[0]
    output = tmp_path / "tests/shard_durations.json"
    assert json.loads(output.read_text()) == {f"tests/test_{i}.py": 3.6 for i in range(1, 7)}
    assert output.read_text() == json.dumps(json.loads(output.read_text()), indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize("problem", ["missing-shard", "invalid-xml", "missing-file", "negative-time", "empty"])
def test_bad_reports_do_not_replace_table(tmp_path, monkeypatch, problem) -> None:
    monkeypatch.setattr(refresh, "REPO_ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    output = tmp_path / "tests/shard_durations.json"
    output.write_text('{"tests/test_old.py": 5.0}\n')

    def download(command, **kwargs):
        directory = Path(command[command.index("--dir") + 1])
        for shard in range(1, 7):
            if problem == "missing-shard" and shard == 6:
                continue
            artifact = directory / f"agent-shard-junit-{shard}-of-6"
            artifact.mkdir()
            xml = f'<testsuite><testcase file="tests/test_{shard}.py" time="1"/></testsuite>'
            if shard == 1:
                xml = {"invalid-xml": "<", "missing-file": '<testsuite><testcase time="1"/></testsuite>',
                       "negative-time": '<testsuite><testcase file="tests/test_1.py" time="-1"/></testsuite>',
                       "empty": '<testsuite/>'}.get(problem, xml)
            (artifact / "agent-shard-junit.xml").write_text(xml)

    monkeypatch.setattr(refresh.subprocess, "run", download)
    with pytest.raises(SystemExit):
        refresh.main(["12345"])
    assert output.read_text() == '{"tests/test_old.py": 5.0}\n'
