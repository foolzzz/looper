"""The one addition to the frozen Manager process signature that scoped routing makes.

``_prepare_turn`` appends ``(scope.fingerprint, scope.mode.value)`` to the resident process signature inside
``if scope is not None``. Default-off freeze tests that compare the signature assignments with main's call
``without_scope_signature`` to set exactly that node aside; test_chat_scope_signature_off_path.py shows it is
guarded and that an unscoped turn's signature is unchanged. Any other new signature assignment still fails them.
"""

from __future__ import annotations

import ast

SCOPE_SIGNATURE_NODE = ast.dump(
    ast.parse("signature += (scope.fingerprint, scope.mode.value)").body[0], include_attributes=False)


def without_scope_signature(dumps: list[str]) -> list[str]:
    assert dumps.count(SCOPE_SIGNATURE_NODE) == 1, "the scoped signature addition must appear exactly once"
    return [dump for dump in dumps if dump != SCOPE_SIGNATURE_NODE]
