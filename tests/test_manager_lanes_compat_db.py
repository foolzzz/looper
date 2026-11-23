"""The disabled switch preserves the recorded main contract with lanes disabled."""
import json
from pathlib import Path

import pytest

from tests.db_support import DSN, run_db
from tests.manager_lanes_compat_support import capture

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
BASELINE = Path(__file__).parent / "fixtures" / "manager_lanes_off.json"


@pytest.mark.parametrize("routing", [False, True])
def test_disabled_lanes_match_recorded_inputs_records_replies_and_settles(monkeypatch, tmp_path, routing):
    baseline = json.loads(BASELINE.read_text())
    # Re-recorded from plain main after #1574 added nullable llm_records.attempt_id.
    assert baseline["commit"] == "335689f374f2200094c7063ef3a732ee56660182"

    async def run(pool):
        actual, _ = await capture(pool, tmp_path, monkeypatch, routing=routing)
        assert actual == baseline["routing_on" if routing else "routing_off"]

    run_db(run, monkeypatch, timeout=60)
