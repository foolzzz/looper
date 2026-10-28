"""Release schema text markers remain eligible independently of catalog layout."""
import subprocess
from pathlib import Path

import pytest

from looper.db import bridge_upgrade
from looper.db.connection import schema_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def test_every_available_bridge_release_has_an_allowed_marker():
    tags = subprocess.check_output(
        ["git", "tag", "-l", "v2.13.*"], cwd=ROOT, text=True, timeout=10).splitlines()
    tags = [tag for tag in tags if tag.rsplit(".", 1)[-1].isdigit() and int(tag.rsplit(".", 1)[-1]) >= 165]
    if not tags:
        pytest.skip("bridge release tags are absent from this checkout")
    for tag in tags:
        sql = subprocess.check_output(
            ["git", "show", f"{tag}:looper/db/schema.sql"], cwd=ROOT, text=True, timeout=10)
        assert schema_fingerprint(sql) in bridge_upgrade.BRIDGE_ERA_FINGERPRINTS, tag
