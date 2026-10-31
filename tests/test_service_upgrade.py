"""Upgrade previews use the running manifest and never mutate a service."""

from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper import cli, components
from looper.agents import operator_auth, service
from looper.components import Manifest


def _manifest(changed: str | None = None) -> Manifest:
    return Manifest(
        version="1.0.0", loaded_at="2026-10-30T00:00:00+00:00",
        components={name: ("b" if name == changed else "a") * 64
                    for name in ("web", "control_plane", "keeper", "runners")},
        adoption={"run_manifest_version": 0, "work_contract": "c" * 64,
                  "review_contract": "d" * 64,
                  "seat_tools": {"worker": "e" * 64, "reviewer": "f" * 64},
                  "keeper_protocols": []},
    )


@pytest.fixture
def upgrade_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "runtime"
    token = operator_auth.ensure_token(root)
    unit = tmp_path / "looper.service"
    unit.write_text("test service unit\n")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    requests = []

    def forbidden(*args, **kwargs):
        pytest.fail("an upgrade preview must not install, restart, sleep or run the service manager")

    def get_components(port, credential):
        requests.append((port, credential))
        assert (port, credential) == (18834, token)
        return _manifest().to_dict()

    host = service.Host(
        system="Linux", home=tmp_path / "home", uid=501, environ={},
        executable=str(tmp_path / ".venv/bin/python"), cwd=checkout, package_root=checkout, run=forbidden,
        sleep=forbidden, get_status=forbidden, port_in_use=forbidden, get_components=get_components,
    )
    state = service.State(installed=True, loaded=True, running=True, pid=1234, port=18834,
                          log_path=root / "logs/serve.log", runtime_root=root)
    manager = SimpleNamespace(
        unit_argv=lambda: service.looper_command(host.executable) + ["serve"],
        unit_path=unit, state=lambda: state, unit_settings=lambda **kwargs: (18834, state.log_path, root),
        start=forbidden, stop=forbidden, remove=forbidden,
    )
    monkeypatch.setattr(service, "manager", lambda host, label: manager)
    monkeypatch.setattr(service.Host, "current", classmethod(lambda cls: host))
    monkeypatch.setenv("HOME", str(host.home))
    return host, requests


@pytest.mark.parametrize("change, changed_component, expected", [
    ("docs", None, "NOOP"),
    ("web/src", "web", "WEB_ONLY"),
    ("looper/agents/tiers/planner.py", "control_plane", "FULL"),
])
def test_cli_upgrade_dry_run_rfc_examples(upgrade_host, monkeypatch, capsys, change, changed_component, expected):
    host, requests = upgrade_host
    targets = []

    def from_git(repo, ref):
        targets.append((repo, ref))
        return _manifest(changed_component)

    monkeypatch.setattr(components, "manifest_from_git", from_git)

    assert cli.main(["service", "upgrade", "--dry-run", "--to", "candidate"]) == 0
    output = capsys.readouterr().out
    assert expected in output
    assert "reason" in output.lower()
    assert "changed" in output.lower()
    if changed_component:
        assert changed_component in output
    assert targets == [(host.cwd, "candidate")]
    assert len(requests) == 1


def test_cli_upgrade_defaults_to_checkout_origin_main(upgrade_host, monkeypatch, capsys):
    host, _ = upgrade_host
    targets = []

    def from_git(repo, ref):
        targets.append((repo, ref))
        return _manifest()

    monkeypatch.setattr(components, "manifest_from_git", from_git)
    assert cli.main(["service", "upgrade", "--dry-run"]) == 0
    assert "NOOP" in capsys.readouterr().out
    assert targets == [(host.cwd, "origin/main")]


def test_cli_upgrade_can_inspect_a_wheel_without_installing(upgrade_host, monkeypatch, tmp_path, capsys):
    wheel = tmp_path / "looper-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"reader is replaced by a manifest stand-in")
    targets = []

    def from_wheel(path):
        targets.append(path)
        return _manifest("web")

    monkeypatch.setattr(components, "manifest_from_wheel", from_wheel)
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: pytest.fail("wheel must not read git"))
    assert cli.main(["service", "upgrade", "--dry-run", "--to", str(wheel)]) == 0
    assert "WEB_ONLY" in capsys.readouterr().out
    assert targets == [wheel]


@pytest.mark.parametrize("path, needle, replacement", [
    ("looper/agents/ops/__init__.py", '    READ = "read"',
     '    READ = (MARK, "read")[1]'),
    ("looper/agents/ops/__init__.py", "    name: str",
     "    MARK\n    name: str"),
    ("looper/agents/ops/__init__.py", "@dataclass(frozen=True)\nclass Tool:",
     "@(MARK or dataclass(frozen=True))\nclass Tool:"),
    ("looper/agents/ops/__init__.py", '        return {"name": self.name,',
     '        MARK\n        return {"name": self.name,'),
    ("looper/agents/ops/room.py", '"maximum": 120',
     '"maximum": (MARK, 120)[1]'),
    ("looper/agents/ops/room.py", "    if kind is MemberKind.WORK:",
     "    MARK\n    if kind is MemberKind.WORK:"),
    ("looper/agents/ops/vault.py", "async def _never(call: Any)",
     "async def _never(call: Any = MARK)"),
])
def test_cli_upgrade_rejects_executable_wheel_declarations_without_side_effects(
    upgrade_host, tmp_path, capsys, path, needle, replacement,
):
    root = Path(components.__file__).resolve().parent.parent
    sources = {name: (root / name).read_text() for name in (
        "looper/components.toml", "looper/__init__.py", "looper/agents/system_prompt.py",
        *components.SEAT_SOURCES,
    )}
    wheel = tmp_path / "candidate.whl"

    def write_wheel():
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, source in sources.items():
                archive.writestr(name, source)

    # A real, supported wheel must work through the same CLI and manifest reader.
    write_wheel()
    assert cli.main(["service", "upgrade", "--dry-run", "--to", str(wheel)]) == 0
    capsys.readouterr()
    marker = tmp_path / "executed"
    payload = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
    assert needle in sources[path]
    sources[path] = sources[path].replace(needle, replacement.replace("MARK", payload), 1)
    write_wheel()

    result = cli.main(["service", "upgrade", "--dry-run", "--to", str(wheel)])

    assert not marker.exists(), "dry-run executed code from the candidate wheel"
    assert result == 1
    captured = capsys.readouterr()
    assert "cannot read target Worker/Reviewer seat tool contracts" in captured.err + captured.out


@pytest.mark.parametrize("answer", [None, {"status": "unauthorized"}, {"unexpected": "response"}])
def test_upgrade_fails_closed_when_the_running_manifest_is_unavailable(upgrade_host, monkeypatch, answer):
    host, _ = upgrade_host
    host.get_components = lambda port, token: answer
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: _manifest("control_plane"))
    with pytest.raises(service.ServiceError):
        service.upgrade(host, dry_run=True)


@pytest.mark.parametrize("code", [200, 401, 503])
def test_components_request_uses_operator_auth_and_post(monkeypatch, code):
    manifest = _manifest().to_dict()

    def open_request(request, *, timeout):
        assert request.full_url == "http://127.0.0.1:18834/api/v1/system/components"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer preview-token"
        assert timeout == 3
        if code != 200:
            raise urllib.error.HTTPError(request.full_url, code, "refused", {}, None)
        return io.BytesIO(json.dumps({"status": "ok", "result": manifest}).encode())

    monkeypatch.setattr(service.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    if code == 200:
        assert service._get_components(18834, "preview-token") == manifest
    else:
        with pytest.raises(service.ServiceError, match=str(code)):
            service._get_components(18834, "preview-token")


@pytest.mark.parametrize("field,value", [("keeper_protocols", [{}]), ("seat_tools", {}),
                                          ("run_manifest_version", "0")])
def test_upgrade_rejects_malformed_adoption_data(upgrade_host, monkeypatch, field, value):
    host, _ = upgrade_host
    answer = _manifest().to_dict()
    answer["adoption"][field] = value
    host.get_components = lambda port, token: answer
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: _manifest())
    with pytest.raises(service.ServiceError, match="adoption"):
        service.upgrade(host, dry_run=True)
