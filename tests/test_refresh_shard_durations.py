"""Refresh timing weights from all eight shard reports without contacting GitHub."""

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
        for shard in range(1, 9):
            artifact = directory / f"agent-shard-junit-{shard}-of-8"
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
    assert json.loads(output.read_text()) == {f"tests/test_{i}.py": 3.6 for i in range(1, 9)}
    assert output.read_text() == json.dumps(json.loads(output.read_text()), indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize("problem", ["missing-shard", "invalid-xml", "missing-file", "negative-time", "empty"])
def test_bad_reports_do_not_replace_table(tmp_path, monkeypatch, problem) -> None:
    monkeypatch.setattr(refresh, "REPO_ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    output = tmp_path / "tests/shard_durations.json"
    output.write_text('{"tests/test_old.py": 5.0}\n')

    def download(command, **kwargs):
        directory = Path(command[command.index("--dir") + 1])
        for shard in range(1, 9):
            if problem == "missing-shard" and shard == 8:
                continue
            artifact = directory / f"agent-shard-junit-{shard}-of-8"
            artifact.mkdir()
            xml = f'<testsuite><testcase file="tests/test_{shard}.py" time="1"/></testsuite>'
            if shard == 1:
                xml = {"invalid-xml": "<", "missing-file": '<testsuite><testcase time="1"/></testsuite>',
                       "negative-time": '<testsuite><testcase file="tests/test_1.py" time="-1"/></testsuite>',
                       "empty": '<testsuite/>'}.get(problem, xml)
            (artifact / "agent-shard-junit.xml").write_text(xml)

    monkeypatch.setattr(refresh.subprocess, "run", download)
    sibling = tmp_path / "tests/shard_test_durations.json"
    sibling.write_text('{"tests/test_old.py": {"tests/test_old.py::test_keep": 5.0}}\n')
    with pytest.raises(SystemExit):
        refresh.main(["12345"])
    assert output.read_text() == '{"tests/test_old.py": 5.0}\n'
    assert sibling.read_text() == '{"tests/test_old.py": {"tests/test_old.py::test_keep": 5.0}}\n'


def _download(cases_for_shard):
    def download(command, **kwargs):
        directory = Path(command[command.index("--dir") + 1])
        for shard in range(1, 9):
            artifact = directory / f"agent-shard-junit-{shard}-of-8"
            artifact.mkdir()
            body = "".join(cases_for_shard(shard))
            (artifact / "agent-shard-junit.xml").write_text(f"<testsuite>{body}</testsuite>")

    return download


def test_refresh_records_per_test_seconds_only_above_the_split_threshold(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(refresh, "REPO_ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    assert refresh.HEAVY_FILE_SPLIT_SECONDS == 300.0

    def cases_for_shard(shard: int) -> list[str]:
        rows = ['<testcase classname="tests.test_light" name="test_one" file="tests/test_light.py" time="1"/>']
        if shard == 1:
            rows.append(
                '<testcase classname="tests.test_heavy.TestGroup" name="test_slow[a.b]" '
                'file="tests/test_heavy.py" time="200.04"/>'
                '<testcase classname="tests.test_exact" name="test_at" file="tests/test_exact.py" time="300"/>'
            )
        if shard == 2:
            rows.append(
                '<testcase classname="tests.test_heavy" name="test_fast" file="tests/test_heavy.py" time="100.02"/>'
            )
        return rows

    monkeypatch.setattr(refresh.subprocess, "run", _download(cases_for_shard))
    refresh.main(["12345"])
    files = json.loads((tmp_path / "tests/shard_durations.json").read_text())
    assert files == {
        "tests/test_exact.py": 300.0,
        "tests/test_heavy.py": 300.1,
        "tests/test_light.py": 8.0,
    }
    per_test_path = tmp_path / "tests/shard_test_durations.json"
    per_test = json.loads(per_test_path.read_text())
    assert per_test == {
        "tests/test_heavy.py": {
            "tests/test_heavy.py::TestGroup::test_slow[a.b]": 200.0,
            "tests/test_heavy.py::test_fast": 100.0,
        },
    }
    assert per_test_path.read_text() == json.dumps(per_test, indent=2, sort_keys=True) + "\n"


def test_refresh_rejects_a_classname_that_does_not_match_the_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(refresh, "REPO_ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    output = tmp_path / "tests/shard_durations.json"
    output.write_text('{"tests/test_old.py": 5.0}\n')
    sibling = tmp_path / "tests/shard_test_durations.json"
    sibling.write_text("{}\n")

    def cases_for_shard(shard: int) -> list[str]:
        # 40s * 8 shards is over the split threshold, so the classname must invert.
        return ['<testcase classname="not.the.module" name="test_one" file="tests/test_new.py" time="40"/>']

    monkeypatch.setattr(refresh.subprocess, "run", _download(cases_for_shard))
    with pytest.raises(SystemExit):
        refresh.main(["12345"])
    assert output.read_text() == '{"tests/test_old.py": 5.0}\n'
    assert sibling.read_text() == "{}\n"
