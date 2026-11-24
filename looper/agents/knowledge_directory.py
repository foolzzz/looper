"""Operator-selected project directory inside the configured knowledge vault."""

from pathlib import PurePosixPath

from . import vault


def validate(config: vault.VaultConfig, value: str | None) -> str | None:
    if value is None:
        return None
    if problem := vault._path_problem(value):
        raise ValueError(f"knowledge_dir {problem}")
    parts = PurePosixPath(value).parts
    if any(config.path.joinpath(*parts[:depth]).is_symlink() for depth in range(1, len(parts) + 1)):
        raise ValueError("knowledge_dir must not pass through a symbolic link")
    path = config.path / value
    if not path.resolve().is_relative_to(config.path):
        raise ValueError("knowledge_dir must resolve inside the knowledge vault")
    if any((part := config.path.joinpath(*parts[:depth])).exists() and not part.is_dir()
           for depth in range(1, len(parts) + 1)):
        raise ValueError("knowledge_dir must contain only directories")
    return value
