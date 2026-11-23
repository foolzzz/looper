"""Artifact bytes are a browser operator session's, rebuilt from the stored run, never from the URL."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from looper.agents import operator_auth
from looper.agents.acceptance_artifacts import collect
from looper.agents.acceptance_checks import CommandCheck
from looper.agents.http_server import AgentHTTPServer
from looper.db import plans, projects, step_check_runs as runs, tasks
from tests.db_support import run_db
from tests.test_operator_auth import TOKEN, ManagerOps, _request

PNG = b"\x89PNG\r\n\x1a\n" + b"looper-evidence-png"
TEXT = b"evidence note\n"
SECRET = b"SECRET-BYTES-NOT-EVIDENCE"
CLIENT_TOKEN = "lpc_" + "c" * 43


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    def get_extra_info(self, name: str) -> tuple[str, int]:
        return ("127.0.0.1", 9)

    def close(self) -> None:
        pass

    async def drain(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _server(root: Path) -> AgentHTTPServer:
    return AgentHTTPServer(
        port=0, operator_token=TOKEN, static_dir=root, runtime_root=root,
        web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
        manager_ops=ManagerOps())


def _cookie(server: AgentHTTPServer) -> dict[str, str]:
    return {"Cookie": f"{operator_auth.cookie_name(server._port)}={TOKEN}"}


async def _exchange(server: AgentHTTPServer, line: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    reader = asyncio.StreamReader()
    reader.feed_data(_request(server._port, line, headers).encode())
    reader.feed_eof()
    writer = _Writer()
    await server._handle_connection(reader, writer)
    head, _, body = bytes(writer.data).partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    fields = {key.lower(): value for key, value in (line.split(": ", 1) for line in lines[1:])}
    return int(lines[0].split()[1]), fields, body


async def _stored(pool, root: Path) -> tuple[AgentHTTPServer, int]:
    project = await projects.create_project(name="evidence", pool=pool)
    await tasks.create_task(task_id="task-art", requirement="Show the shot", project_id=project["id"],
                            status="failed", pool=pool)
    check = CommandCheck("c1", 1, "true")
    await plans.save("task-art", steps=[{
        "id": "step-art", "index": 0, "key": "shot", "instruction": "Show the shot",
        "acceptance_criteria": ["the shot is saved"], "checks": [check.row()],
    }], pool=pool)
    row = await runs.create_run("task-art", "step-art", check.row(), 1, pool=pool)
    work = root / "work"
    work.mkdir()
    (work / "shot.png").write_bytes(PNG)
    (work / "note.txt").write_text(TEXT.decode())
    meta = collect(work, ("shot.png", "note.txt"), root, {
        "task_id": "task-art", "step_id": "step-art", "attempt": row["attempt"],
        "check_id": row["check_id"], "run": row["run"],
    })
    await runs.finish_run(row["id"], runs.CheckState.PASSED, exit_code=0, artifacts=meta, pool=pool)
    (root / "secret.txt").write_bytes(SECRET)
    return _server(root), row["id"]


def _headers(status: int, fields: dict[str, str], body: bytes, *, mime: str, disposition: str) -> None:
    assert status == 200
    assert fields["content-type"] == mime
    assert fields["x-content-type-options"] == "nosniff"
    assert fields["content-security-policy"] == "sandbox"
    assert fields["content-disposition"] == disposition
    assert fields["cache-control"] == "no-store"
    assert fields["content-length"] == str(len(body))


def test_operator_session_serves_bytes_and_other_callers_get_none(tmp_path: Path, monkeypatch) -> None:
    async def test(pool) -> None:
        server, run_id = await _stored(pool, tmp_path)
        image = f"GET /api/evidence/task-art/{run_id}/0"
        note = f"GET /api/evidence/task-art/{run_id}/1"

        status, fields, body = await _exchange(server, image, _cookie(server))
        _headers(status, fields, body, mime="image/png", disposition="inline")
        assert body == PNG

        status, fields, body = await _exchange(server, note, _cookie(server))
        _headers(status, fields, body, mime="text/plain", disposition='attachment; filename="2.txt"')
        assert body == TEXT

        for headers in ({"Authorization": f"Bearer {TOKEN}"}, {"Authorization": f"Bearer {CLIENT_TOKEN}"}, None):
            status, _, body = await _exchange(server, image, headers)
            assert status in (401, 403)
            assert body != PNG and PNG not in body and SECRET not in body
        status, _, body = await _exchange(server, image, {"Authorization": f"Bearer {TOKEN}"})
        assert status == 403 and json.loads(body) == {"error": "operator session required"}
        status, _, body = await _exchange(server, image, {"Authorization": f"Bearer {CLIENT_TOKEN}"})
        assert status == 401
        status, _, body = await _exchange(server, image, None)
        assert status == 401

    run_db(test, monkeypatch)


def test_traversal_symlink_and_a_missing_file_return_no_bytes(tmp_path: Path, monkeypatch) -> None:
    async def test(pool) -> None:
        server, run_id = await _stored(pool, tmp_path)
        cookie = _cookie(server)
        png = next((tmp_path / "evidence").rglob("1.png"))
        note = next((tmp_path / "evidence").rglob("2.txt"))
        png.unlink()
        status, _, body = await _exchange(server, f"GET /api/evidence/task-art/{run_id}/0", cookie)
        assert status == 404 and json.loads(body) == {"error": "file missing"}

        note.unlink()
        note.symlink_to(tmp_path / "secret.txt")
        status, _, body = await _exchange(server, f"GET /api/evidence/task-art/{run_id}/1", cookie)
        assert status == 404 and json.loads(body) == {"error": "not found"} and SECRET not in body

        await pool.execute(
            "UPDATE step_check_runs SET artifacts = $2::jsonb WHERE id = $1", run_id,
            json.dumps([{"name": "../secret.txt", "mime": "text/plain", "bytes": 3, "sha256": "aa"}]))
        status, fields, body = await _exchange(server, f"GET /api/evidence/task-art/{run_id}/0", cookie)
        assert status == 404 and json.loads(body) == {"error": "not found"} and SECRET not in body
        assert "x-injected" not in fields

        await pool.execute(
            "UPDATE step_check_runs SET artifacts = $2::jsonb WHERE id = $1", run_id,
            json.dumps([{"name": "1.txt", "mime": "text/plain\r\nX-Injected: yes", "bytes": 3, "sha256": "aa"}]))
        status, fields, body = await _exchange(server, f"GET /api/evidence/task-art/{run_id}/0", cookie)
        assert status == 404 and json.loads(body) == {"error": "file missing"}
        assert "x-injected" not in fields and b"X-Injected" not in body and SECRET not in body

        for line in (
            "GET /api/evidence/../secret.txt/1/0",
            "GET /api/evidence/%2e%2e/1/0",
            f"GET /api/evidence/task-art/{run_id}/..",
            "GET /api/evidence/task-art/not-a-run/0",
        ):
            status, _, body = await _exchange(server, line, cookie)
            assert status == 404 and json.loads(body) == {"error": "not found"} and SECRET not in body

    run_db(test, monkeypatch)


def test_a_run_id_past_int64_is_not_found_before_the_query(tmp_path: Path, monkeypatch) -> None:
    async def test(pool) -> None:
        server, run_id = await _stored(pool, tmp_path)
        cookie = _cookie(server)
        seen: list[int] = []
        original = runs.get_run

        async def spy(run_key: int, task_id: str, **kwargs):
            seen.append(run_key)
            return await original(run_key, task_id, **kwargs)

        monkeypatch.setattr(runs, "get_run", spy)
        status, _, body = await _exchange(server, "GET /api/evidence/task-art/9223372036854775808/0", cookie)
        assert status == 404 and json.loads(body) == {"error": "not found"} and seen == []

        status, _, body = await _exchange(server, f"GET /api/evidence/task-art/{2**63 - 1}/0", cookie)
        assert status == 404 and json.loads(body) == {"error": "not found"} and seen == [2**63 - 1]
        assert run_id != 2**63 - 1

    run_db(test, monkeypatch)


def test_a_row_claiming_html_or_svg_is_served_as_its_stored_name(tmp_path: Path, monkeypatch) -> None:
    async def test(pool) -> None:
        server, run_id = await _stored(pool, tmp_path)
        cookie = _cookie(server)
        await pool.execute(
            "UPDATE step_check_runs SET artifacts = $2::jsonb WHERE id = $1", run_id,
            json.dumps([
                {"name": "1.png", "mime": "text/html", "bytes": 3, "sha256": "aa"},
                {"name": "2.txt", "mime": "image/svg+xml", "bytes": 3, "sha256": "bb"},
            ]))
        status, fields, body = await _exchange(server, f"GET /api/evidence/task-art/{run_id}/0", cookie)
        _headers(status, fields, body, mime="image/png", disposition="inline")
        assert body == PNG and "text/html" not in fields["content-type"]

        status, fields, body = await _exchange(server, f"GET /api/evidence/task-art/{run_id}/1", cookie)
        _headers(status, fields, body, mime="text/plain", disposition='attachment; filename="2.txt"')
        assert body == TEXT and fields["content-type"] != "image/svg+xml"
        assert "svg" not in fields["content-type"]

    run_db(test, monkeypatch)
