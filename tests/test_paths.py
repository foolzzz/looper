"""Runtime-root selection leaves retired directories untouched."""

import pytest

from looper import ops_config, paths


@pytest.mark.parametrize("old_is_link", [False, True])
@pytest.mark.parametrize("explicit", [None, "custom", ".loopx"])
def test_current_root_selection_never_moves_or_links_old_state(tmp_path, monkeypatch, old_is_link, explicit):
    home = tmp_path / "home"
    home.mkdir()
    old = home / ".loopx"
    data = tmp_path / "old-data" if old_is_link else old
    data.mkdir()
    marker = data / "marker"
    marker.write_bytes(b"old state")
    if old_is_link:
        old.symlink_to(data, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOOPER_RUNTIME_ROOT", str(old))
    monkeypatch.setenv("LOOPX_RUNTIME_ROOT", str(old))
    root = home / explicit if explicit else paths.home_runtime_root()
    assert root == home / (explicit or ".looper")
    settings = ops_config.load(ops_config.Flags(runtime_root=root), require_database=False)
    assert settings.runtime_root == root
    assert not (home / ".looper").exists()
    assert old.is_symlink() is old_is_link
    assert marker.read_bytes() == b"old state"


def test_init_ops_creates_current_root_without_touching_retired_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    old = tmp_path / ".loopx"
    old.mkdir()
    (old / "marker").write_bytes(b"old state")
    written, _ = ops_config.init_file()
    assert written.runtime_root == tmp_path / ".looper"
    assert written.path.is_file()
    assert not old.is_symlink() and (old / "marker").read_bytes() == b"old state"


def test_test_guard_protects_both_home_names_and_both_ambient_roots(tmp_path, monkeypatch):
    import real_runtime_root_guard_plugin as plugin

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOOPER_RUNTIME_ROOT", "current-ambient")
    monkeypatch.setenv("LOOPX_RUNTIME_ROOT", "retired-ambient")
    roots = set(plugin.session_roots())
    assert {str(tmp_path / "home" / name) for name in (".looper", ".loopx")} <= roots
    assert {str(tmp_path / name) for name in ("current-ambient", "retired-ambient")} <= roots
