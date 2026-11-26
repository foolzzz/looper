"""The memory credential grants exactly three local read operations, never operator powers."""

import asyncio

import pytest

from looper.agents import api_v1, operator_auth
from tests.test_api_v1_auth import _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN

PATHS = {f"/api/v1/manager_retrieval/{name}" for name in ("vault_search", "vault_read", "leads_search")}


@pytest.fixture(autouse=True)
def current_operator_token(tmp_path):
    operator_auth.token_path(tmp_path).write_text(TOKEN)


def test_rotation_revokes_the_old_credential_without_restarting(tmp_path, monkeypatch):
    from looper.agents.v1_domains import manager_retrieval

    async def answer(*_args):
        return {"read": True}

    monkeypatch.setattr(manager_retrieval, "run", answer)

    async def run():
        async with _serving(tmp_path) as server:
            async def read(token):
                status, _, _ = await _send(server, "POST /api/v1/manager_retrieval/vault_search",
                                          _bearer(operator_auth.retrieval_token(token)), '{"query":"Atlas"}')
                return status

            assert await read(TOKEN) == 200
            # Replace the on-disk token, exactly as an operator rotation does.
            replacement = tmp_path / "new-token"
            replacement.write_text(TOKEN + "rotated")
            replacement.replace(operator_auth.token_path(tmp_path))
            assert await read(TOKEN) == 401
            assert await read(TOKEN + "rotated") == 200
            operator_auth.token_path(tmp_path).unlink()
            assert await read(TOKEN + "rotated") == 401
    asyncio.run(run())


def test_retrieval_credential_is_domain_separated_and_rotates():
    token = operator_auth.retrieval_token(TOKEN)
    assert token.startswith("lmr_") and TOKEN not in token
    assert token == operator_auth.retrieval_token(TOKEN)
    assert token != operator_auth.retrieval_token(TOKEN + "rotated")


def test_memory_credential_cannot_call_any_other_v1_operation(tmp_path, clients):  # noqa: F811
    async def run():
        async with _serving(tmp_path) as server:
            credential = operator_auth.retrieval_token(TOKEN)
            rejected = []
            for operation in api_v1.operations():
                if operation.path in PATHS:
                    continue
                status, _, answer = await _send(server, f"{operation.method} {operation.path}",
                                                 _bearer(credential), "{}")
                assert status == 403, operation.operation_id
                assert answer["error"]["message"] == "the memory credential allows only the three retrieval reads"
                rejected.append(operation)
            assert any(op.risk == "write" for op in rejected)
            assert any(op.path == "/api/v1/config/import_upload" for op in rejected)
            assert not clients.found and not clients.created and not clients.touched
    asyncio.run(run())


@pytest.mark.parametrize("name", ["vault_search", "vault_read", "leads_search"])
def test_memory_credential_reaches_only_the_retrieval_reads(tmp_path, monkeypatch, name):
    from looper.agents.v1_domains import manager_retrieval

    principals = []

    async def answer(server, operation, arguments, principal, headers):
        principals.append(principal)
        return {"read": operation.operation_id}

    monkeypatch.setattr(manager_retrieval, "run", answer)

    async def run():
        async with _serving(tmp_path) as server:
            status, _, body = await _send(server, f"POST /api/v1/manager_retrieval/{name}",
                _bearer(operator_auth.retrieval_token(TOKEN)), '{"path":"a.md"}' if name == "vault_read"
                else '{"query":"Atlas"}')
            assert status == 200 and body["result"] == {"read": f"manager_retrieval.{name}"}
            assert principals == [api_v1.OPERATOR]
    asyncio.run(run())


@pytest.mark.parametrize("variant", ["cookie", "forwarded", "peer", "rotated", "wrong_method"])
def test_memory_credential_has_no_other_authentication_path(tmp_path, variant):
    async def run():
        async with _serving(tmp_path) as server:
            token = operator_auth.retrieval_token(TOKEN + "old" if variant == "rotated" else TOKEN)
            headers = _bearer(token)
            if variant == "cookie":
                headers = {"Cookie": f"{operator_auth.cookie_name(server._port)}={token}"}
            if variant == "forwarded":
                headers["X-Forwarded-For"] = "192.0.2.1"
            method = "GET" if variant == "wrong_method" else "POST"
            status, _, _ = await _send(server, f"{method} /api/v1/manager_retrieval/vault_search", headers,
                                       '{"query":"Atlas"}', peer="192.0.2.1" if variant == "peer" else "127.0.0.1")
            assert status in (401, 403)
            status, _, _ = await _send(server, "POST /api/chat", _bearer(token), "{}")
            assert status == 401
    asyncio.run(run())
