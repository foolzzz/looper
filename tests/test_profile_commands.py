"""The local hook command is bounded and silent, including a PostgreSQL handshake that never answers."""

import socket
import subprocess
import sys
import time

import pytest

from looper import ops_config
from looper.agents import profile_commands


def _show(root):
    return subprocess.run([sys.executable, "-m", "looper.cli", "profile", "show", "--runtime-root", str(root)],
                          capture_output=True, timeout=2)


def test_unavailable_settings_are_silent(tmp_path):
    # A malformed private ops file cannot fall through to any real installation.
    root = tmp_path / "runtime"
    ops_config.write_private(root / ops_config.FILE, "invalid = [")
    result = _show(root)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_database_handshake_has_a_deadline(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        root = tmp_path / "runtime"
        ops = ops_config.OpsConfig(runtime_root=root, path=root / ops_config.FILE,
            database_url=f"postgresql://synthetic@127.0.0.1:{listener.getsockname()[1]}/unavailable")
        ops_config.write_private(ops.path, ops_config.render(ops))
        before = time.monotonic()
        result = _show(root)
        elapsed = time.monotonic() - before
        assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
        assert elapsed < 2
        # Positive control: the client really connected, rather than failing before the blocking handshake.
        listener.settimeout(0.2)
        connection, _ = listener.accept()
        connection.close()


def test_synchronous_worker_hang_is_killed(tmp_path, monkeypatch, capsys):
    worker = tmp_path / "blocked-python"
    worker.write_text("#!/bin/sh\nexec sleep 10\n")
    worker.chmod(0o755)
    monkeypatch.setattr(profile_commands.sys, "executable", str(worker))
    before = time.monotonic()
    assert profile_commands.show(object()) == 0
    assert time.monotonic() - before < 2
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("output", [b"x" * (profile_commands.MAX_OUTPUT_BYTES + 1), b"\xff"])
def test_invalid_or_over_cap_worker_output_is_silent(output, monkeypatch, capsys):
    monkeypatch.setattr(profile_commands.subprocess, "run",
                        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, output))
    assert profile_commands.show(object()) == 0
    assert capsys.readouterr() == ("", "")


def test_database_without_settings_table_is_silent(tmp_path):
    from tests.db_support import DSN
    from evals.prompt_regression.runner import fresh_database

    if not DSN:
        pytest.skip("LOOPER_TEST_DATABASE_URL is not set")
    with fresh_database(DSN) as (database_url, _):
        root = tmp_path / "runtime"
        ops = ops_config.OpsConfig(runtime_root=root, path=root / ops_config.FILE, database_url=database_url)
        ops_config.write_private(ops.path, ops_config.render(ops))
        result = _show(root)
        assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
