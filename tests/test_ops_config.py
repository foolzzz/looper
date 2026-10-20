"""Deployment settings are private, explicit and read without changing the environment."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tomllib
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper import ops_config as ops
from looper.ops_config import Flags, OpsConfigError, Origin
from looper.agents.ops import Call, OpError, Turn, check_arguments
from looper.agents.ops import config as manager_config
from looper.agents.provider_presets import PRESETS, presets_view

SECRET = "Sekr1t-pw"
DATABASE_URL = f"postgresql://u:{SECRET}@h/db"
MALFORMED_URLS = [
    "looper.example.com", "ftp://looper.example.com", "http://", "http://x:99999", "http://user:secret@x",
    "http://x/?a=1", "http://x/#tasks", "http://x:port",
]


def _file(root: Path, text: str | bytes) -> Path:
    directory = root / "ops"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / "server.toml"
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    path.chmod(0o600)
    return path


@pytest.fixture
def dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / ".env"


def _load(root: Path, dotenv: Path, environ: dict[str, str] | None = None, **kwargs: object) -> ops.OpsConfig:
    return ops.load(Flags(runtime_root=root), environ={} if environ is None else environ, dotenv=dotenv,
                    require_database=False, **kwargs)


def _value(name: str, tag: str) -> str | list[str]:
    return {
        "database_url": f"postgresql://u:{SECRET}-{tag}@h/db",
        "public_url": f"https://example.com/{SECRET}-{tag}",
        "pg_dump": f"/bin/{SECRET}-{tag}/pg_dump",
        "backup_command": ["docker", "exec", f"{SECRET}-{tag} container", "pg_dump"],
        "operator_notes_root": f"/notes/{SECRET}-{tag}",
    }[name]


LEGACY_SETTINGS = [
    ("database_url", "database", "url", "DATABASE_URL"),
    ("public_url", "server", "public_url", "PUBLIC_URL"),
    ("pg_dump", "backup", "pg_dump", "PG_DUMP"),
    ("backup_command", "backup", "command", "BACKUP_CMD"),
    ("operator_notes_root", "operator_notes", "root", "OPERATOR_NOTES_ROOT"),
]


@pytest.mark.parametrize(("name", "section", "key", "suffix", "winner"), [
    (*setting, winner) for setting in LEGACY_SETTINGS
    for winner in ("file", "environment-new", "environment-old", "dotenv-new", "dotenv-old")
    if setting[0] != "operator_notes_root" or not winner.endswith("old")
])
def test_setting_precedence_and_sources(
    tmp_path: Path, dotenv: Path, name: str, section: str, key: str, suffix: str, winner: str,
) -> None:
    candidates = ["file", "environment-new", "environment-old", "dotenv-new", "dotenv-old"]
    if name == "operator_notes_root":
        candidates = [candidate for candidate in candidates if not candidate.endswith("old")]
    present = candidates[candidates.index(winner):]
    environ: dict[str, str] = {}
    lines = []
    for candidate in present:
        value = _value(name, candidate)
        if candidate == "file":
            _file(tmp_path, f"[{section}]\n{key} = {json.dumps(value)}\n")
            continue
        prefix = "LOOPER_" if candidate.endswith("new") else "LOOPX_"
        variable = prefix + suffix
        text = f'docker exec "{SECRET}-{candidate} container" pg_dump' if isinstance(value, list) else value
        if candidate.startswith("environment"):
            environ[variable] = text
        else:
            lines.append(f"{variable}={text}")
    if winner == "environment-old":
        environ["LOOPER_" + suffix] = " \t"
    elif winner == "dotenv-old":
        lines.append("LOOPER_" + suffix + "= ")
    dotenv.write_text("\n".join(lines))
    config = _load(tmp_path, dotenv, environ)
    value = _value(name, winner)
    assert getattr(config, name) == (tuple(value) if isinstance(value, list) else value)
    assert config.sources[name].origin is {
        "file": Origin.FILE, "environment": Origin.ENVIRONMENT, "dotenv": Origin.DOTENV,
    }[winner.split("-")[0]]
    assert len(config.warnings) == 1
    if winner != "file":
        prefix = "LOOPER_" if winner.endswith("new") else "LOOPX_"
        assert prefix + suffix in config.warnings[0] and "is deprecated" in config.warnings[0]
    assert SECRET not in "\n".join(config.warnings)


@pytest.mark.parametrize(("name", "section", "key", "suffix"), LEGACY_SETTINGS)
def test_missing_and_blank_variables_are_unset(
    tmp_path: Path, dotenv: Path, name: str, section: str, key: str, suffix: str,
) -> None:
    dotenv.write_text(f"LOOPER_{suffix}= \nLOOPX_{suffix}=\n")
    config = _load(tmp_path, dotenv, {f"LOOPER_{suffix}": " \t", f"LOOPX_{suffix}": ""})
    assert getattr(config, name) == (() if name == "backup_command" else "")
    assert name not in config.sources
    assert not any("is deprecated" in warning for warning in config.warnings)
    if name == "database_url":
        with pytest.raises(OpsConfigError, match="looper config init-ops") as error:
            ops.load(Flags(runtime_root=tmp_path), environ={}, dotenv=dotenv)
        assert SECRET not in str(error.value)


@pytest.mark.parametrize("origin", ["environment", "dotenv"])
def test_loopx_operator_notes_root_is_not_read(tmp_path: Path, dotenv: Path, origin: str) -> None:
    value = f"/notes/{SECRET}"
    dotenv.write_text(f"LOOPX_OPERATOR_NOTES_ROOT={value}" if origin == "dotenv" else "")
    config = _load(tmp_path, dotenv, {"LOOPX_OPERATOR_NOTES_ROOT": value} if origin == "environment" else {})
    assert config.operator_notes_root == "" and "operator_notes_root" not in config.sources
    assert SECRET not in "\n".join(config.warnings)
    if origin == "dotenv":
        assert len(config.warnings) == 1 and "not read: LOOPX_OPERATOR_NOTES_ROOT" in config.warnings[0]
    else:
        assert config.warnings == ()


@pytest.mark.parametrize(("name", "file_value", "flag_value", "default"), [
    ("host", f"{SECRET}-file", f"{SECRET}-flag", "127.0.0.1"),
    ("port", 9001, 9002, 8767),
    ("log_level", "warning", "debug", "info"),
])
@pytest.mark.parametrize("winner", ["flag", "file", "default"])
def test_server_flags_file_and_defaults(
    tmp_path: Path, dotenv: Path, name: str, file_value: object, flag_value: object, default: object, winner: str,
) -> None:
    if winner != "default":
        _file(tmp_path, f"[server]\n{name} = {json.dumps(file_value)}\n")
    flags = Flags(runtime_root=tmp_path, **({name: flag_value} if winner == "flag" else {}))
    config = ops.load(flags, environ={f"LOOPER_{name.upper()}": SECRET}, dotenv=dotenv, require_database=False)
    assert getattr(config, name) == {"flag": flag_value, "file": file_value, "default": default}[winner]
    if winner == "default":
        assert name not in config.sources
    else:
        assert config.sources[name].origin is (Origin.FLAG if winner == "flag" else Origin.FILE)
    assert not config.warnings


def test_legacy_warnings_name_each_used_variable_once(tmp_path: Path, dotenv: Path) -> None:
    environ = {"LOOPER_" + suffix: (
        f'docker exec "{SECRET} container" pg_dump' if name == "backup_command" else _value(name, "env")
    ) for name, _, _, suffix in LEGACY_SETTINGS}
    config = _load(tmp_path, dotenv, environ)
    assert len(config.warnings) == len(environ)
    for name in environ:
        assert sum(name in warning for warning in config.warnings) == 1
    assert all("is deprecated" in warning and SECRET not in warning for warning in config.warnings)


@pytest.mark.parametrize("prefix", ["LOOPER_", "LOOPX_"])
def test_shadowed_environment_is_warned_without_reading_its_value(tmp_path: Path, dotenv: Path, prefix: str) -> None:
    _file(tmp_path, f'[database]\nurl = "{DATABASE_URL}"\n')
    config = _load(tmp_path, dotenv, {prefix + "DATABASE_URL": SECRET})
    assert config.database_url == DATABASE_URL
    assert len(config.warnings) == 1
    assert prefix + "DATABASE_URL is set but not used" in config.warnings[0]
    assert SECRET not in config.warnings[0]


def test_dotenv_unknown_keys_are_names_only_and_never_exported(
    tmp_path: Path, dotenv: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("FOO", "LOOPER_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    dotenv.write_text(f"FOO={SECRET}\nLOOPER_DATABASE_URL={DATABASE_URL}\n")
    config = _load(tmp_path, dotenv)
    assert config.database_url == DATABASE_URL
    assert any("not read: FOO" in warning for warning in config.warnings)
    assert SECRET not in "\n".join(config.warnings)
    assert "FOO" not in os.environ and "LOOPER_DATABASE_URL" not in os.environ


BAD_FILE_VALUES = [
    ('[unknown]\nkey = "Sekr1t-pw"', "unknown"),
    ('[database]\nunknown = "Sekr1t-pw"', "database.unknown"),
    ('key = "Sekr1t-pw"', "key"),
    ('database = "Sekr1t-pw"', "database"),
    ('[database]\nurl = 5', "database.url"),
    ('[database]\nurl = ""', "database.url"),
    ('[database]\nurl = "mysql://u:Sekr1t-pw@h/db"', "database.url"),
    *[(f"[server]\nport = {value}", "server.port") for value in ('"8767"', 'true', '0', '70000')],
    *[(f"[backup]\ncommand = {value}", "backup.command") for value in ('"docker exec"', '[]', '[""]')],
    ('[server]\nlog_level = "verbose"', "server.log_level"),
    *[(f"[server]\npublic_url = {json.dumps(value)}", "server.public_url") for value in MALFORMED_URLS],
    ('[operator_notes]\nroot = "Sekr1t-pw/relative"', "operator_notes.root"),
    ('[database\nurl = "Sekr1t-pw"', "server.toml"),
    (b'\xffSekr1t-pw', "server.toml"),
]


@pytest.mark.parametrize(("text", "key"), BAD_FILE_VALUES)
def test_invalid_file_names_the_key_or_file_without_values(
    tmp_path: Path, dotenv: Path, text: str | bytes, key: str,
) -> None:
    _file(tmp_path, text)
    with pytest.raises(OpsConfigError) as error:
        _load(tmp_path, dotenv)
    assert key in str(error.value)
    assert SECRET not in str(error.value)
    assert "user:secret" not in str(error.value)


@pytest.mark.parametrize(("variable", "value"), [
    ("LOOPER_PUBLIC_URL", f"{SECRET}.example.com"),
    ("LOOPER_BACKUP_CMD", f'{SECRET} unterminated "quote'),
    ("LOOPER_DATABASE_URL", f"mysql://u:{SECRET}@h/db"),
])
def test_invalid_environment_names_variable_without_value(
    tmp_path: Path, dotenv: Path, variable: str, value: str,
) -> None:
    with pytest.raises(OpsConfigError) as error:
        _load(tmp_path, dotenv, {variable: value})
    assert variable in str(error.value) and SECRET not in str(error.value)


@pytest.mark.parametrize(("problem", "message"), [
    ("file-mode", "chmod 600"), ("directory-mode", "chmod 700"), ("file-link", "symbolic link"),
    ("directory-link", "symbolic link"), ("not-file", "regular file"), ("owner", "uid"),
])
def test_file_safety(tmp_path: Path, dotenv: Path, monkeypatch: pytest.MonkeyPatch, problem: str, message: str) -> None:
    path = _file(tmp_path, f'[database]\nurl = "{DATABASE_URL}"\n')
    if problem == "file-mode":
        path.chmod(0o644)
    elif problem == "directory-mode":
        path.parent.chmod(0o755)
    elif problem == "file-link":
        target = path.with_name("private.toml")
        path.rename(target)
        path.symlink_to(target)
    elif problem == "directory-link":
        target = tmp_path / "private-ops"
        path.parent.rename(target)
        path.parent.symlink_to(target, target_is_directory=True)
    elif problem == "not-file":
        path.unlink()
        path.mkdir(mode=0o700)
    else:
        monkeypatch.setattr(ops.os, "geteuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(OpsConfigError) as error:
        _load(tmp_path, dotenv)
    assert message in str(error.value) and SECRET not in str(error.value)


def test_missing_explicit_file_refused_but_missing_default_is_fine(tmp_path: Path, dotenv: Path) -> None:
    config = _load(tmp_path, dotenv)
    assert config.sources == {} and config.warnings == ()
    with pytest.raises(OpsConfigError, match="no such file"):
        ops.load(Flags(runtime_root=tmp_path, config=tmp_path / "missing.toml"), environ={}, dotenv=dotenv,
                 require_database=False)


@pytest.mark.parametrize("winner", ["flag", "LOOPER_RUNTIME_ROOT", "LOOPX_RUNTIME_ROOT", "home", "blank"])
def test_runtime_root_precedence_and_warning(
    tmp_path: Path, dotenv: Path, monkeypatch: pytest.MonkeyPatch, winner: str,
) -> None:
    environ = {"LOOPER_RUNTIME_ROOT": str(tmp_path / f"{SECRET}-new"),
               "LOOPX_RUNTIME_ROOT": str(tmp_path / f"{SECRET}-old")}
    if winner == "LOOPX_RUNTIME_ROOT":
        environ["LOOPER_RUNTIME_ROOT"] = " \t"
    if winner in ("home", "blank"):
        environ = {} if winner == "home" else dict.fromkeys(environ, " \t")
    monkeypatch.chdir(tmp_path)
    flag = "relative-root" if winner == "flag" else None
    root, warning = ops.runtime_root(flag, environ)
    expected = tmp_path / "relative-root" if flag else (
        tmp_path / "home" / ".looper" if winner in ("home", "blank") else Path(environ[winner]))
    assert root == expected and root.is_absolute()
    if winner.startswith("LOOP"):
        assert winner in warning and "is deprecated" in warning
    else:
        assert warning == ""
    assert SECRET not in warning
    config = ops.load(Flags(runtime_root=flag), environ=environ, dotenv=dotenv, require_database=False)
    assert config.runtime_root == root
    assert config.warnings == ((warning,) if warning else ())
    assert ops.config_path(None, root) == root / "ops" / "server.toml"
    assert ops.config_path("relative.toml", root) == tmp_path / "relative.toml"


def test_public_url_is_trimmed_and_trailing_slashes_removed(tmp_path: Path, dotenv: Path) -> None:
    config = _load(tmp_path, dotenv, {"LOOPER_PUBLIC_URL": f" https://example.com/{SECRET}/// "})
    assert config.public_url == f"https://example.com/{SECRET}"


@pytest.mark.parametrize("configured", [True, False], ids=["effective-settings", "legacy-default"])
def test_init_file_is_private_round_trips_and_explains_sources(
    tmp_path: Path, dotenv: Path, configured: bool,
) -> None:
    environ = {"LOOPER_" + suffix: (
        f'docker exec "{SECRET} container" pg_dump' if name == "backup_command" else _value(name, "env")
    ) for name, _, _, suffix in LEGACY_SETTINGS} if configured else {}
    flags = Flags(runtime_root=tmp_path / "runtime")
    if configured:
        flags = replace(flags, host=f"{SECRET}-host", port=9002, log_level="debug")
    before = ops.load(flags, environ=environ, dotenv=dotenv, require_database=False)
    written, notes = ops.init_file(flags, environ=environ, dotenv=dotenv)
    assert stat.S_IMODE(written.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.path.parent.stat().st_mode) == 0o700
    table = tomllib.loads(written.path.read_text())
    assert table["database"]["url"] == (before.database_url or ops.LEGACY_DEFAULT_DATABASE_URL)
    assert table["server"] == ({"host": before.host, "port": before.port, "log_level": before.log_level,
                                "public_url": before.public_url} if configured else {})
    if configured:
        assert table["backup"] == {"pg_dump": before.pg_dump, "command": list(before.backup_command)}
        assert table["operator_notes"] == {"root": before.operator_notes_root}
    for name, source in before.sources.items():
        setting = next(setting for setting in ops.SETTINGS if setting.name == name)
        assert f"{setting.dotted} from {source}" in notes
    assert SECRET not in "\n".join(notes)
    if not configured:
        assert any(ops.LEGACY_DEFAULT_DATABASE_URL in note and "old built-in default" in note for note in notes)
    later = ops.load(Flags(runtime_root=flags.runtime_root), environ={}, dotenv=dotenv)
    assert later == written
    assert all(source.origin is Origin.FILE for source in later.sources.values()) and not later.warnings
    for name in before.sources:
        assert getattr(later, name) == getattr(before, name)
    assert set(later.sources) == set(before.sources) | {"database_url"}
    with pytest.raises(OpsConfigError, match="exists already"):
        ops.init_file(flags, environ=environ, dotenv=dotenv)


@pytest.mark.parametrize("fails", [False, True], ids=["replace", "failed-replace"])
def test_write_private_replaces_and_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool,
) -> None:
    path = _file(tmp_path, "old")
    if fails:
        def fail_replace(*args: object) -> None:
            raise OSError("write failed")
        monkeypatch.setattr(ops.os, "replace", fail_replace)
        with pytest.raises(OSError, match="write failed"):
            ops.write_private(path, SECRET)
    else:
        ops.write_private(path, SECRET)
    assert path.read_text() == ("old" if fails else SECRET)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(".server.toml.*")) == []


def test_render_escapes_strings_for_toml(tmp_path: Path) -> None:
    value = 'Sekr1t-pw "quoted" \\path\n雪'
    config = replace(ops.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops.FILE), database_url=DATABASE_URL,
                     host=value, pg_dump=value, backup_command=(value,), operator_notes_root=value)
    table = tomllib.loads(ops.render(config))
    assert table["server"]["host"] == table["backup"]["pg_dump"] == table["operator_notes"]["root"] == value
    assert table["backup"]["command"] == [value]


def test_render_writes_characters_outside_the_bmp_that_load_reads_back(tmp_path: Path, dotenv: Path) -> None:
    """A JSON escape of such a character is a surrogate pair, which TOML refuses: the file would not load."""
    url = "postgresql://u:pw-\U0001F512-\x7f@h/db"
    path = tmp_path / "runtime" / ops.FILE
    ops.write_private(path, ops.render(ops.OpsConfig(runtime_root=tmp_path / "runtime", path=path, database_url=url,
                                                     public_url="https://looper.example.com/é")))
    loaded = _load(tmp_path / "runtime", dotenv)
    assert loaded.database_url == url and loaded.public_url == "https://looper.example.com/é"


def test_render_refuses_text_that_is_not_unicode(tmp_path: Path) -> None:
    config = ops.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops.FILE, database_url="postgresql://u@h/\udcff")
    with pytest.raises(OpsConfigError, match="not text"):
        ops.render(config)


def test_init_file_writes_a_new_config_path(tmp_path: Path, dotenv: Path) -> None:
    """``init-ops --config <path that does not exist yet>`` writes that file."""
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "looper.toml"
    written, notes = ops.init_file(Flags(runtime_root=tmp_path / "runtime", config=path),
                                   environ={"LOOPER_DATABASE_URL": DATABASE_URL}, dotenv=dotenv)
    assert written.path == path and written.database_url == DATABASE_URL
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "runtime" / ops.FILE).exists()
    assert all(SECRET not in note for note in notes)


def _provider_call(arguments: dict[str, object]) -> dict[str, object]:
    tool = next(tool for tool in manager_config.TOOLS if tool.name == "looper_providers")
    call = Call(check_arguments(tool.schema, arguments), Turn(channel=None, chat=None), None)
    return asyncio.run(tool.run(call))


@pytest.mark.parametrize(("api", "protocols", "base_url", "suffix"), [
    ("anthropic-messages", ["anthropic-messages"], "https://api.deepseek.com/anthropic", "[1m]"),
    ("openai-chat", ["openai-responses", "openai-chat"], "https://api.deepseek.com", ""),
    ("openai-responses", ["openai-responses", "openai-chat"], "https://api.deepseek.com", ""),
])
def test_manager_provider_preset_defaults(
    monkeypatch: pytest.MonkeyPatch, api: str, protocols: list[str], base_url: str, suffix: str,
) -> None:
    create = AsyncMock(return_value={"provider": {
        "provider_id": "new", "name": "deepseek", "auth": "api_key", "protocols": protocols,
        "base_url": base_url, "models": [], "profile_count": 0, "builtin": False, "api_key": None}})
    monkeypatch.setattr(manager_config.profile_registry, "create_provider", create)
    _provider_call({"action": "register", "preset": "deepseek", "api": api})
    assert create.await_args.args[0] == {
        "name": "deepseek", "auth": "api_key", "protocols": protocols, "base_url": base_url,
        "models": [{"name": name + suffix, "context_window": 1_000_000, "background_model": "deepseek-flash"}
                   for name in ("deepseek-flash", "deepseek-v4-pro")],
        "env": [{"key": "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "value": "786432"}]}


@pytest.mark.parametrize("empty", [False, True])
def test_manager_provider_preset_explicit_overrides(monkeypatch: pytest.MonkeyPatch, empty: bool) -> None:
    create = AsyncMock(return_value={"provider": {
        "provider_id": "new", "name": "custom", "auth": "api_key", "protocols": ["openai-chat"],
        "base_url": "https://example.com", "models": [], "profile_count": 0, "builtin": False, "api_key": None}})
    monkeypatch.setattr(manager_config.profile_registry, "create_provider", create)
    _provider_call({"action": "register", "preset": "glm-bigmodel", "api": "anthropic-messages",
                    "name": "custom", "protocols": ["openai-chat"], "base_url": "https://example.com",
                    "models": [] if empty else ["custom-model"], "env": {} if empty else {"API_TIMEOUT_MS": "12"}})
    assert create.await_args.args[0] == {
        "name": "custom", "auth": "api_key", "protocols": ["openai-chat"], "base_url": "https://example.com",
        "models": [] if empty else [{"name": "custom-model"}],
        "env": [] if empty else [{"key": "API_TIMEOUT_MS", "value": "12"}]}


@pytest.mark.parametrize(("arguments", "message"), [
    ({"preset": "unknown"}, "unknown preset 'unknown'"),
    ({"preset": "deepseek"}, "api is required"),
    ({"preset": "deepseek", "api": "unknown"}, "unknown API"),
])
def test_manager_provider_preset_errors(monkeypatch: pytest.MonkeyPatch, arguments: dict, message: str) -> None:
    create = AsyncMock()
    monkeypatch.setattr(manager_config.profile_registry, "create_provider", create)
    with pytest.raises(OpError, match=message) as error:
        _provider_call({"action": "register", **arguments})
    assert error.value.code.value == "invalid"
    if arguments["preset"] == "unknown":
        assert all(preset.id in str(error.value) for preset in PRESETS)
    create.assert_not_awaited()


def test_manager_provider_list_includes_presets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_config, "_registry", AsyncMock(return_value={"providers": []}))
    assert _provider_call({"action": "list"}) == {"providers": [], "presets": presets_view()}
