from __future__ import annotations

import getpass
import inspect
from pathlib import Path

import pytest

from looper import cli
from looper.agents import auth_commands, operator_auth


@pytest.mark.parametrize("user", ["admin", "ops"])
def test_matching_password_prompts_set_the_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], user: str,
) -> None:
    prompts = []
    password = "a long private password"

    def read_password(prompt: str) -> str:
        prompts.append(prompt)
        return password

    assert auth_commands.set_password(tmp_path, user, read_password=read_password) == 0
    assert prompts == ["New password: ", "Repeat it: "]
    record = operator_auth.load_password(tmp_path)
    assert record is not None and operator_auth.verify(record, user, password)
    output = capsys.readouterr()
    assert output.out == (f"password set for {user} in {tmp_path / operator_auth.PASSWORD_FILE}; "
                          "browsers that are not on this machine sign in with it at /login\n")
    assert output.err == "" and password not in output.out
    assert inspect.signature(auth_commands.set_password).parameters["read_password"].default is getpass.getpass


def test_differing_passwords_leave_the_existing_file_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    operator_auth.set_password(tmp_path, "admin", "the old password")
    path = tmp_path / operator_auth.PASSWORD_FILE
    before = path.read_bytes()
    passwords = iter(["the new password", "a different password"])

    assert auth_commands.set_password(tmp_path, read_password=lambda _: next(passwords)) == 1
    assert path.read_bytes() == before
    output = capsys.readouterr()
    assert "the passwords differ; nothing changed" in output.err and output.out == ""


@pytest.mark.parametrize(("user", "password", "reason"), [
    ("admin", "short", "8"), ("bad/user", "long enough password", "user"),
])
def test_invalid_credentials_leave_the_existing_file_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], user: str, password: str, reason: str,
) -> None:
    operator_auth.set_password(tmp_path, "admin", "the old password")
    path = tmp_path / operator_auth.PASSWORD_FILE
    before = path.read_bytes()

    assert auth_commands.set_password(tmp_path, user, read_password=lambda _: password) == 1
    assert path.read_bytes() == before
    output = capsys.readouterr()
    assert reason in output.err and output.out == ""
    assert password not in output.err


def test_clear_password_reports_removed_and_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    operator_auth.set_password(tmp_path, "admin", "the old password")

    assert auth_commands.clear_password(tmp_path) == 0
    assert operator_auth.load_password(tmp_path) is None
    assert capsys.readouterr().out == f"password removed from {tmp_path / operator_auth.PASSWORD_FILE}\n"
    assert auth_commands.clear_password(tmp_path) == 0
    assert capsys.readouterr().out == f"no password was set in {tmp_path / operator_auth.PASSWORD_FILE}\n"


@pytest.mark.parametrize("command", ["set-password", "clear-password"])
def test_cli_uses_the_environment_and_explicit_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    default, explicit = tmp_path / "environment-root", tmp_path / "explicit"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOPER_RUNTIME_ROOT", str(default))
    monkeypatch.setenv("LOOPX_RUNTIME_ROOT", str(tmp_path / "ignored-retired"))
    received = []
    if command == "set-password":
        monkeypatch.setattr(auth_commands, "set_password", lambda root, user: received.append((root, user)) or 0)
        assert cli.main(["auth", command]) == 0
        assert cli.main(["auth", command, "--user", "ops", "--runtime-root", str(explicit)]) == 0
        assert received == [(default, "admin"), (explicit, "ops")]
    else:
        monkeypatch.setattr(auth_commands, "clear_password", lambda root: received.append(root) or 0)
        assert cli.main(["auth", command]) == 0
        assert cli.main(["auth", command, "--runtime-root", str(explicit)]) == 0
        assert received == [default, explicit]


@pytest.mark.parametrize("argument", ["--password", "--password=secret", "--pass", "secret"])
def test_parser_never_accepts_a_password_argument(argument: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["auth", "set-password", argument])
    assert error.value.code == 2


@pytest.mark.parametrize("command", ["set-password", "clear-password"])
def test_credential_file_errors_have_a_clean_cli_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str,
) -> None:
    def unusable(*_: object) -> None:
        raise operator_auth.OperatorTokenError("the runtime root must be private")

    monkeypatch.setattr(operator_auth, command.replace("-", "_"), unusable)
    result = (auth_commands.set_password(tmp_path, read_password=lambda _: "a private password")
              if command == "set-password" else auth_commands.clear_password(tmp_path))
    assert result == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == "error: the runtime root must be private\n"
