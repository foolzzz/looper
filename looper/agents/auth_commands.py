"""Manage the password used to sign in from another browser."""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from . import operator_auth


def set_password(runtime_root: Path, user: str = "admin", *,
                 read_password: Callable[[str], str] = getpass.getpass) -> int:
    try:
        password = read_password("New password: ")
        if read_password("Repeat it: ") != password:
            print("error: the passwords differ; nothing changed", file=sys.stderr)
            return 1
        operator_auth.set_password(runtime_root, user, password)
    except (ValueError, operator_auth.OperatorTokenError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"password set for {user} in {runtime_root / operator_auth.PASSWORD_FILE}; "
          "browsers that are not on this machine sign in with it at /login")
    return 0


def clear_password(runtime_root: Path) -> int:
    try:
        removed = operator_auth.clear_password(runtime_root)
    except (operator_auth.OperatorTokenError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path = runtime_root / operator_auth.PASSWORD_FILE
    print(f"password removed from {path}" if removed else f"no password was set in {path}")
    return 0
