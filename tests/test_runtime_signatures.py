"""Every runtime adapter accepts the arguments its callers pass (the abstract signatures).

A runtime added before a new parameter reached the abstract method once kept the old signature, and every Grok
Worker run then failed with a TypeError that no unit test of the runtime itself could see.
"""

from __future__ import annotations

import inspect

import pytest

from looper.agents.runtimes import RuntimeAdapter, adapter

ADAPTER_TYPES = ("claude-code", "codex", "omp", "grok", "dsh")
METHODS = [name for name, member in vars(RuntimeAdapter).items()
           if getattr(member, "__isabstractmethod__", False) and name.endswith("_command")]


@pytest.mark.parametrize("runtime_type", ADAPTER_TYPES)
@pytest.mark.parametrize("method", METHODS)
def test_an_adapter_accepts_every_parameter_of_the_abstract_command(runtime_type: str, method: str) -> None:
    abstract = inspect.signature(getattr(RuntimeAdapter, method))
    concrete = inspect.signature(getattr(type(adapter(runtime_type)), method))
    missing = [name for name in abstract.parameters if name not in concrete.parameters]
    assert not missing, f"{runtime_type}.{method} lacks {missing}"
