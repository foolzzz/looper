"""The session-budget dialog exercises rotation and the next turn's task-result brief on a disposable serve."""

import os
from pathlib import Path

import pytest

from evals.prompt_regression import dialogs


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"), reason="disposable PostgreSQL unavailable")
def test_the_fake_session_bound_rotation_remembers_an_ended_task(tmp_path: Path) -> None:
    result, _ = dialogs.run(mode="fake", dialogs=dialogs.select(["session_bound_rotate"]),
                            admin_url=os.environ["LOOPER_TEST_DATABASE_URL"], out_dir=tmp_path, timeout=30)
    assert result["aborted"] is None, result
    assert [row["outcome"] for row in result["dialogs"]] == ["pass"], result["dialogs"]
