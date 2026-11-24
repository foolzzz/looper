"""A fresh serve exposes the preview protocol without any model or external writes."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from evals.prompt_regression import runner
from tests.db_support import DSN, fresh_database_without_recording
from tests.fake_claude import fake_claude

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_rule_management_round_trip_on_fresh_serve(tmp_path):
    runtime, bins = tmp_path / "runtime", tmp_path / "bin"
    bins.mkdir()
    fake_claude(bins / "claude", "import sys\nprint('2.1.0')\nsys.exit(0)\n")
    with fresh_database_without_recording(DSN) as (database_url, _):
        with runner.running_server(database_url=database_url, runtime_root=runtime, bin_dir=bins,
                                   log_path=tmp_path / "serve.log", home=tmp_path / "home") as api:
            def call(action, body=None):
                return api.post("/api/v1/intent_rules/" + action, body or {})["result"]
            initial = call("list")
            assert initial["generation"] == 0 and initial["rules"] == []
            assert initial["runtime_ready"] is False
            command = {"kind": "rule_create", "rule": {"enabled": True, "position": 1,
                "intent_type": "any", "scope": {"kind": "all"}, "conditions": [],
                "action": "push_for_confirmation", "project_id": None, "template_key": None}}
            preview = call("preview", {"expected_generation": 0, "change": command})
            assert call("list")["rules"] == []
            confirmation = {"preview_id": preview["preview_id"], "acceptance": preview["required_acceptance"]}
            result = call("confirm", confirmation)
            assert result["generation"] == 1 and call("confirm", confirmation) == result
            rid = result["rules"][0]["id"]
            assert call("history", {"rule_id": rid})["versions"][0]["version"] == 1
            tokens = [call("preview", {"expected_generation": 1, "change": {"kind": "rule_disable",
                "rule_id": rid, "expected_version": 1}})["preview_id"] for _ in range(2)]
            def confirm(token):
                try:
                    return call("confirm", {"preview_id": token, "acceptance": []})
                except runner.EvalError as exc:
                    return str(exc)
            with ThreadPoolExecutor(max_workers=2) as workers:
                answers = list(workers.map(confirm, tokens))
            assert sum(isinstance(answer, dict) for answer in answers) == 1
            assert any("409" in str(answer) and "conflict" in str(answer) for answer in answers)
            with pytest.raises(runner.EvalError, match="409"):
                call("preview", {"expected_generation": 2, "change": {"kind": "settings", "enabled": True}})
            assert call("list")["generation"] == 2
