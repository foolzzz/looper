"""The disabled switch preserves the recorded pre-lanes contract."""
import hashlib
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
    assert baseline["commit"] == "ecc2eedbf2ac4a1b44ff9078e634002c228458c1"

    async def run(pool):
        actual, _ = await capture(pool, tmp_path, monkeypatch, routing=routing)
        expected = baseline["routing_on" if routing else "routing_off"]
        # The attempts migration adds a nullable link; these Manager-only runs have no attempt.
        for record in expected["records"]:
            record["attempt_id"] = hashlib.sha256(b"null").hexdigest()
        assert actual == expected

    run_db(run, monkeypatch, timeout=60)
