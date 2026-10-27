"""Deployment settings are private, explicit and read without changing the environment."""

from __future__ import annotations

import asyncio
import json
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
from looper.agents.runtimes import VendorType

SECRET = "Sekr1t-pw"
DATABASE_URL = f"postgresql://u:{SECRET}@h/db"
MALFORMED_URLS = [
    "looper.example.com", "ftp://looper.example.com", "http://", "http://x:99999", "http://user:secret@x",
    "http://x/?a=1", "http://x/#tasks", "http://x:port",
]


def test_manager_provider_list_includes_vendors_and_each_model_suggestion(monkeypatch):
    monkeypatch.setattr(manager_config.profile_registry, "list_providers", AsyncMock(return_value={"providers": []}))
    answer = _provider_call({"action": "list"})
    assert answer["providers"] == []
    assert answer["vendors"] == manager_config.profile_registry.vendor_types_view()
    by_id = {v["vendor_type"]: v for v in answer["vendors"]}
    assert {"opencode", "sub2api"} <= by_id.keys()
    models = by_id["opencode"]["suggested_models"]
    assert [[m["name"] for m in models if m["protocol"] == protocol] for protocol in
            ("openai-responses", "openai-chat", "anthropic-messages")] == [
        ["gpt-6.1-sol", "gpt-5.6-sol"], ["deepseek-v4-pro", "glm-5.3", "kimi-k3"],
        ["claude-opus-5-5", "claude-sonnet-5-5"]]


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
    monkeypatch.chdir(tmp_path)
    return tmp_path / ".env"


def _load(root: Path, dotenv: Path, **kwargs: object) -> ops.OpsConfig:
    return ops.load(Flags(runtime_root=root), require_database=False, **kwargs)


@pytest.mark.parametrize("in_file", [False, True])
def test_retired_environment_and_dotenv_are_ignored(tmp_path: Path, dotenv: Path, monkeypatch, in_file):
    for name in ops.VARIABLES | {"LOOPER_RUNTIME_ROOT", "LOOPX_RUNTIME_ROOT"}:
        monkeypatch.setenv(name, SECRET)
    dotenv.write_text("\n".join(f"{name}={SECRET}" for name in ops.VARIABLES))
    if in_file:
        _file(tmp_path, f'[database]\nurl = "{DATABASE_URL}"\n')
    config = _load(tmp_path, dotenv)
    assert config.database_url == (DATABASE_URL if in_file else "")
    assert config.warnings == ()
    assert config.runtime_root == tmp_path
    assert config.pg_dump == config.public_url == config.operator_notes_root == ""
    assert config.backup_command == config.backup_verify_command == ()
    if not in_file:
        with pytest.raises(OpsConfigError, match="no database configured"):
            ops.load(Flags(runtime_root=tmp_path))


@pytest.mark.parametrize(("section", "key", "value", "field"), [
    ("database", "url", DATABASE_URL, "database_url"),
    ("server", "public_url", "https://example.com", "public_url"),
    ("backup", "pg_dump", "/bin/pg_dump", "pg_dump"),
    ("backup", "command", ["dump", "{dbname}"], "backup_command"),
    ("backup", "verify_command", ["restore", "--list"], "backup_verify_command"),
    ("operator_notes", "root", "/notes", "operator_notes_root"),
])
def test_file_settings_have_file_sources(tmp_path, dotenv, section, key, value, field):
    _file(tmp_path, f"[{section}]\n{key} = {json.dumps(value)}\n")
    config = _load(tmp_path, dotenv)
    assert getattr(config, field) == (tuple(value) if isinstance(value, list) else value)
    assert config.sources[field].origin is Origin.FILE
    assert not config.warnings


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
    config = ops.load(flags, require_database=False)
    assert getattr(config, name) == {"flag": flag_value, "file": file_value, "default": default}[winner]
    if winner == "default":
        assert name not in config.sources
    else:
        assert config.sources[name].origin is (Origin.FLAG if winner == "flag" else Origin.FILE)
    assert not config.warnings


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
        ops.load(Flags(runtime_root=tmp_path, config=tmp_path / "missing.toml"),
                 require_database=False)


@pytest.mark.parametrize("flag", [None, "relative-root", ".loopx"])
def test_runtime_root_uses_flag_or_home(tmp_path, dotenv, monkeypatch, flag):
    monkeypatch.setenv("LOOPER_RUNTIME_ROOT", str(tmp_path / "retired-new"))
    monkeypatch.setenv("LOOPX_RUNTIME_ROOT", str(tmp_path / "retired-old"))
    root, warning = ops.runtime_root(flag)
    assert root == (tmp_path / flag if flag else tmp_path / "home" / ".looper")
    assert warning == ""
    config = ops.load(Flags(runtime_root=flag), require_database=False)
    assert config.runtime_root == root and config.warnings == ()
    assert ops.config_path(None, root) == root / "ops" / "server.toml"
    assert ops.config_path("relative.toml", root) == tmp_path / "relative.toml"


def test_public_url_is_trimmed_and_trailing_slashes_removed(tmp_path, dotenv):
    _file(tmp_path, f'[server]\npublic_url = " https://example.com/{SECRET}/// "')
    assert _load(tmp_path, dotenv).public_url == f"https://example.com/{SECRET}"


@pytest.mark.parametrize("configured", [True, False], ids=["flags", "sample-dsn"])
def test_init_file_is_private_round_trips_and_explains_sources(tmp_path, dotenv, configured):
    flags = Flags(runtime_root=tmp_path / "runtime")
    if configured:
        flags = replace(flags, host=f"{SECRET}-host", port=9002, log_level="debug")
    before = ops.load(flags, require_database=False)
    written, notes = ops.init_file(flags)
    assert stat.S_IMODE(written.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.path.parent.stat().st_mode) == 0o700
    table = tomllib.loads(written.path.read_text())
    assert table["database"]["url"] == ops.SAMPLE_DATABASE_URL
    assert table["server"] == ({"host": before.host, "port": before.port, "log_level": before.log_level}
                                if configured else {})
    assert any(ops.SAMPLE_DATABASE_URL in note and "sample DSN" in note for note in notes)
    assert SECRET not in "\n".join(notes)
    later = ops.load(Flags(runtime_root=flags.runtime_root))
    assert later == written
    assert all(source.origin is Origin.FILE for source in later.sources.values()) and not later.warnings
    for name in before.sources:
        assert getattr(later, name) == getattr(before, name)
    with pytest.raises(OpsConfigError, match="exists already"):
        ops.init_file(flags)


def test_existing_database_url_is_preserved(tmp_path, dotenv):
    path = _file(tmp_path, f'[database]\nurl = "{DATABASE_URL}"\n')
    config = ops.load(Flags(runtime_root=tmp_path))
    assert tomllib.loads(ops.render(config))["database"]["url"] == DATABASE_URL
    with pytest.raises(OpsConfigError, match="exists already"):
        ops.init_file(Flags(runtime_root=tmp_path))
    assert ops.read_file(path)["database_url"] == DATABASE_URL


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
                     host=value, pg_dump=value, backup_command=(value,), backup_verify_command=(value,), operator_notes_root=value)
    table = tomllib.loads(ops.render(config))
    assert table["server"]["host"] == table["backup"]["pg_dump"] == table["operator_notes"]["root"] == value
    assert table["backup"]["command"] == table["backup"]["verify_command"] == [value]


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
    written, notes = ops.init_file(Flags(runtime_root=tmp_path / "runtime", config=path))
    assert written.path == path and written.database_url == ops.SAMPLE_DATABASE_URL
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "runtime" / ops.FILE).exists()
    assert all(SECRET not in note for note in notes)


def _provider_call(arguments: dict[str, object]) -> dict[str, object]:
    tool = next(tool for tool in manager_config.TOOLS if tool.name == "looper_providers")
    call = Call(check_arguments(tool.schema, arguments), Turn(channel=None, chat=None), None)
    return asyncio.run(tool.run(call))


@pytest.mark.parametrize("vendor", [v for v in VendorType if v.auth_kind.value == "api_key"])
def test_manager_provider_vendor_defaults(monkeypatch, vendor):
    from looper.agents.provider_presets import vendor_defaults

    defaults = vendor_defaults(vendor, provider_id="new")
    provider = {"provider_id": "new", "name": vendor.value, "vendor_type": vendor.value, "auth": "api_key",
                "endpoints": [{"protocol": e.protocol.value, "base_url": e.base_url,
                               "use_runtime_default": e.use_runtime_default} for e in defaults.endpoints],
                "model_count": 0, "profile_count": 0, "builtin": False, "login": None, "api_key": None}
    create = AsyncMock(return_value={"provider": provider})
    monkeypatch.setattr(manager_config.profile_registry, "create_provider", create)
    fields = {"name": vendor.value, "vendor_type": vendor.value}
    if defaults.requires_url_confirmation:
        fields["endpoints"] = [{"protocol": "openai-chat", "base_url": "https://example.com"}]
    answer = _provider_call({"action": "register", **fields})
    assert create.await_args.args[0] == fields
    assert answer["provider"]["endpoints"] == provider["endpoints"]
    assert answer["provider"]["model_count"] == 0
    assert "Settings → LLM Providers" in answer["note"]
    suggestions = next(v for v in manager_config.profile_registry.vendor_types_view()
                       if v["vendor_type"] == vendor.value)
    assert [(m["name"], m["protocol"], m["context_window"], m["background_model"])
            for m in suggestions["suggested_models"]] == [
        (m.name, m.protocol.value, m.context_window, m.background_model) for m in defaults.models]
    assert suggestions["env"] == [{"key": k, "value": v} for k, v in defaults.env]


@pytest.mark.parametrize("empty", [False, True])
def test_manager_provider_explicit_overrides(monkeypatch, empty):
    provider = {"provider_id": "new", "name": "custom", "vendor_type": "deepseek", "auth": "api_key",
                "endpoints": [{"protocol": "openai-chat", "base_url": "https://example.com"}],
                "model_count": 0, "profile_count": 0, "builtin": False, "login": None, "api_key": None}
    create = AsyncMock(return_value={"provider": provider})
    monkeypatch.setattr(manager_config.profile_registry, "create_provider", create)
    fields = {"name": "custom", "vendor_type": "deepseek", "endpoints": provider["endpoints"],
              "env": [] if empty else [{"key": "API_TIMEOUT_MS", "value": "12"}]}
    assert _provider_call({"action": "register", **fields})["provider"]["name"] == "custom"
    assert create.await_args.args[0] == fields


@pytest.mark.parametrize(("arguments", "message"), [
    ({"name": "x", "vendor_type": "unknown"}, "vendor_type must be one of"),
    ({"name": "x"}, "vendor_type is required"),
    ({"name": "x", "vendor_type": "claude-subscription"}, "builtin login account"),
    ({"preset": "deepseek"}, "unknown argument 'preset'"),
])
def test_manager_provider_errors(monkeypatch, arguments, message):
    create = AsyncMock()
    monkeypatch.setattr(manager_config.profile_registry, "create_provider", create)
    with pytest.raises(OpError, match=message) as error:
        _provider_call({"action": "register", **arguments})
    assert error.value.code.value == "invalid"
    create.assert_not_awaited()


@pytest.mark.parametrize("action", ["create", "update"])
def test_manager_model_update_keeps_catalog_metadata(monkeypatch, action):
    model = {"model_id": "model-test", "name": "second", "protocol": "openai-chat", "context_window": 200_000,
             "background_model": "background", "efforts": ["low", "high"], "input_usd_per_mtok": 1.0,
             "output_usd_per_mtok": 2.0, "cache_read_usd_per_mtok": 0.0, "cache_write_usd_per_mtok": 0.5}
    monkeypatch.setattr(manager_config, "_model_provider", AsyncMock(return_value="prov-test"))
    monkeypatch.setattr(manager_config, "_model_reference", AsyncMock(return_value=model))
    create = AsyncMock(return_value={"model": model, "warnings": []})
    monkeypatch.setattr(manager_config.profile_registry, action + "_model", create)
    tool = next(t for t in manager_config.TOOLS if t.name == "looper_models")
    fields = {k: model[k] for k in manager_config._MODEL_SETTINGS if k in model}
    arguments = {"action": action, **fields, **({"provider": "account", "name": "second", "protocol": "openai-chat"}
                                            if action == "create" else {"model": "model-test"})}
    answer = asyncio.run(tool.run(Call(check_arguments(tool.schema, arguments), Turn(None, None), None)))
    assert answer == {"model": model, "warnings": []}
    if action == "create":
        create.assert_awaited_once_with({**fields, "provider_id": "prov-test", "name": "second", "protocol": "openai-chat"})
    else:
        create.assert_awaited_once_with("model-test", fields)


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize(("name", "protocol", "unpriced", "suffix"), [
    ("deepseek-typo", "anthropic-messages", True, False),
    ("deepseek-flash[1m]", "anthropic-messages", False, False),
    ("gpt-6.1-sol", "openai-chat", False, False),
    ("glm-5.3-flashx[1m]", "openai-chat", False, True),
    ("custom-model", "openai-responses", True, False),
])
def test_manager_model_warnings_and_price_status(monkeypatch, action, name, protocol, unpriced, suffix):
    from tests.test_llm_models_db import _row

    view = manager_config.profile_registry.model_view(_row(name=name, protocol=protocol))
    monkeypatch.setattr(manager_config, "_model_provider", AsyncMock(return_value="prov-test"))
    monkeypatch.setattr(manager_config, "_model_reference", AsyncMock(return_value=view))
    monkeypatch.setattr(manager_config.profile_registry, action + "_model",
                        AsyncMock(return_value={"model": view, "warnings": view["warnings"]}))
    arguments = {"action": action, **({"provider": "account", "name": name, "protocol": protocol}
                                     if action == "create" else {"model": view["model_id"], "context_window": 123})}
    answer = asyncio.run(manager_config._models(Call(arguments, Turn(None, None), None)))
    assert answer["model"]["name"] == name and answer["model"]["protocol"] == protocol
    assert answer["warnings"] == view["warnings"]
    assert any("no full price" in w and "spend caps" in w for w in answer["warnings"]) == unpriced
    assert any("context suffix" in w and name in w for w in answer["warnings"]) == suffix
    assert any(p["effective"] is None for p in answer["model"]["prices"].values()) == unpriced


def test_manager_provider_refuses_key_arguments() -> None:
    # Which names count as a secret's is ``check_arguments``' (test_manager_ops); here a tool refuses one.
    with pytest.raises(OpError, match="Settings → LLM Providers") as error:
        _provider_call({"action": "register", "api_key": SECRET})
    assert error.value.code.value == "forbidden"
    assert SECRET not in str(error.value)
