"""The stream's dependency range must support its connection options."""

from pathlib import Path
import tomllib

from packaging.requirements import Requirement
import pytest


@pytest.mark.parametrize(("version", "supported"), [("13.1", False), ("14.2", False),
                                                  ("15.0", True), ("15.0.1", True), ("16.0", False)])
def test_websocket_range_supports_disabling_proxies(version: str, supported: bool) -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    requirement = next(Requirement(value) for value in project["project"]["dependencies"]
                       if Requirement(value).name == "websockets")
    assert (version in requirement.specifier) is supported
