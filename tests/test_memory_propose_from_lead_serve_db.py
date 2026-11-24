"""Synthetic lead dialogs through a real isolated CLI server and MCP bridge."""

from contextlib import contextmanager
from dataclasses import replace
import tomllib
from urllib.parse import urlsplit

import pytest

from evals.prompt_regression import dialogs, runner
from tests.db_support import DSN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("case", ["memory_from_lead", "memory_from_lead_request"])
def test_lead_dialog_through_isolated_serve(monkeypatch, tmp_path, case):
    monkeypatch.setattr(runner, "_SERVE", "import sys\nfrom looper.cli import main\n"
                        'sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], '
                        '"--runtime-root", sys.argv[2]]))\n')
    original, render, post = dialogs.running_server, runner.ops_config.render, runner.Api.post
    roots = {}

    def checked_post(api, path, body):
        port = urlsplit(api.base_url).port
        assert port != 8767
        config = tomllib.loads((roots[port] / runner.ops_config.FILE).read_text())
        assert config["server"]["port"] == port
        return post(api, path, body)

    @contextmanager
    def isolated(**kwargs):
        port = runner.free_port()
        assert port != 8767
        roots[port] = kwargs["runtime_root"]
        with monkeypatch.context() as patch:
            patch.setattr(runner.ops_config, "render", lambda config: render(
                replace(config, host="127.0.0.1", port=port)))
            with original(**kwargs, port=port) as api:
                yield api

    monkeypatch.setattr(dialogs, "running_server", isolated)
    monkeypatch.setattr(runner.Api, "post", checked_post)
    result, _ = dialogs.run(mode="fake", dialogs=dialogs.select([case]), admin_url=DSN,
                            out_dir=tmp_path, timeout=45)
    assert result["aborted"] is None, result
    assert [row["outcome"] for row in result["dialogs"]] == ["pass"], result["dialogs"]
