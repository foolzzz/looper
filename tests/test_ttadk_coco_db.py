"""Native Coco and its TTADK launcher remain distinct registry choices."""
import json
from pathlib import Path

import pytest

from fake_claude import fake_claude
from looper.agents import api_v1, profile_registry as registry
from looper.agents.ops import OpError
from looper.agents.v1_domains import workflows as workflow_api
from looper.agents.runtimes import coco_login, ttadk
from looper.db import profile_registry as db
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture
def coco_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("coco", "traex"):
        fake_claude(bin_dir / name, '''import sys
if sys.argv[1:] == ['--version']: print('traecli 0.214.1')
elif sys.argv[1:] == ['models', '--json']: print('[{"name":"GPT-5.5"}]')
else: raise AssertionError(sys.argv)
''')
    fake_claude(bin_dir / "ttadk", "import sys\nassert sys.argv[1:] == ['--version']\nprint('1.0.37')\n")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(registry, "_login_modules", lambda: {})
    coco_login.forget_models()
    ttadk.forget_probe()
    yield
    coco_login.forget_models()
    ttadk.forget_probe()


def test_detection_registers_both_coco_launchers(coco_on_path, monkeypatch):
    async def test(pool):
        found = await registry.register_found_runtimes()
        assert {"coco", "ttadk-coco"} <= set(found)
        native = await db.get_runtime("rt-coco")
        wrapped = await db.get_runtime("rt-ttadk-coco")
        assert native["launcher"] == "native" and native["version"] == "traecli 0.214.1"
        assert (wrapped["name"], wrapped["type"], wrapped["launcher"]) == ("ttadk-coco", "coco", "ttadk")
        assert wrapped["version"] == "ttadk 1.0.37 / traecli 0.214.1" and wrapped["probe_error"] is None
        assert await registry.register_found_runtimes() == []

    run_db(test, monkeypatch, timeout=30)


def test_panel_saves_with_native_work_and_ttadk_review(coco_on_path, tmp_path, monkeypatch):
    async def test(pool):
        await registry.register_found_runtimes()
        model = (await registry.create_model({"provider_id": "prov-coco-login", "name": "GPT-5.5",
                                               "protocol": "openai-responses"}))["model"]["model_id"]
        profiles = []
        for runtime_id in ("rt-coco", "rt-ttadk-coco"):
            result = await registry.create_profile({"name": runtime_id, "runtime_id": runtime_id, "model_id": model})
            profiles.append(result["profile"]["profile_id"])
        preset = {"key": "coco-review", "name": "Coco review", "work_role": "dev",
                  "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement the change.",
                             "profile_id": profiles[0]},
                            {"key": "review_a", "kind": "review", "role_prompt": "Check the change.",
                             "profile_id": profiles[1]}],
                  "review_panel": {"members": ["review_a"], "accept_when": "review_a"},
                  "distinct_reviewer_runtime": True}
        async def call(action, body):
            return await workflow_api.run(None, api_v1.find("POST", "/api/v1/workflows/" + action), body, None, {})

        await call("put", preset)
        answer = await call("get", {"key": preset["key"]})
        assert [role["profile_id"] for role in answer["workflow"]["roles"]] == profiles
        # The same runtime is rejected on this same path, proving the distinct-runtime rule ran.
        preset["roles"][1]["profile_id"] = profiles[0]
        with pytest.raises(OpError, match="runtime"):
            await call("put", preset)
        saved = json.loads(await pool.fetchval("SELECT definition FROM workflows WHERE key = 'coco-review'"))
        assert saved["roles"][1]["profile_id"] == profiles[1]

    run_db(test, monkeypatch, timeout=30)
