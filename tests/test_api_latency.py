"""Request latency diagnostics at the HTTP dispatch boundary."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from looper.agents import api_v1, http_server
from looper.agents.http_server import AgentHTTPServer


class Writer:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass


def server(**kwargs):
    return AgentHTTPServer(web_adapter=None, task_router=None, supervisor=None, **kwargs)


def slow_domain(monkeypatch, delay):
    # A synthetic domain handler lives under the package, like real handlers.
    namespace = {"asyncio": asyncio, "delay": delay}
    exec(compile("async def named_wait():\n    await asyncio.sleep(delay)\n"
                 "async def domain(*args, **kwargs):\n    await named_wait()\n"
                 "    return '200 OK', {'status': 'ok', 'result': {}}\n",
                 str(Path(http_server.__file__).with_name("latency_test_domain.py")), "exec"), namespace)
    monkeypatch.setattr(http_server, "API_SLOW_SECONDS", 0.05)
    return namespace["domain"]


def test_slow_request_captures_waiting_helper_and_resets_context(monkeypatch, caplog):
    async def run():
        from looper.db.timing import current_api_timing
        s = server()
        s._v1_domain = slow_domain(monkeypatch, 0.09)
        w = Writer()
        await s._handle_v1("POST", "/api/v1/system/status", {}, {}, b"{}", w, api_v1.OPERATOR)
        assert current_api_timing.get() is None
        assert b'"status": "ok"' in w.data
        assert len(s._api_latencies["system.status"]) == 1
    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    slow = [r.message for r in caplog.records if r.message.startswith("api slow ")]
    assert len(slow) == 1
    assert "op=system.status status=ok" in slow[0]
    assert "db_ms=0 db_queries=0" in slow[0]
    assert "named_wait" in slow[0].split("awaiting=", 1)[1]
    assert "FutureIter" in slow[0] or "Future" in slow[0]


def test_stream_records_first_byte_and_is_excluded_from_slow_counts(monkeypatch, caplog):
    async def run():
        s = server()
        monkeypatch.setattr(http_server, "API_SLOW_SECONDS", 0.01)
        async def stream(*args, writer, **kwargs):
            await asyncio.sleep(0.02)
            writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
            await asyncio.sleep(0.06)
        s._v1_domain = stream
        await s._handle_v1("POST", "/api/v1/operator_chat/thread_events", {}, {}, b"{}", Writer(), api_v1.OPERATOR)
        assert s._api_latencies == {}
    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert not any(r.message.startswith("api slow ") for r in caplog.records)
    info = next(r.message for r in caplog.records if r.message.startswith("api client="))
    fields = dict(field.split("=", 1) for field in info.split()[1:])
    assert 10 <= int(fields["first_byte_ms"]) < int(fields["ms"])


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_failed_request_cleans_timer_and_context(monkeypatch, caplog, failure):
    async def run():
        from looper.db.timing import current_api_timing
        monkeypatch.setattr(http_server, "API_SLOW_SECONDS", 0.01)
        s = server()
        async def fail(*args, **kwargs):
            await asyncio.sleep(0.02)
            raise failure()
        s._v1_domain = fail
        with pytest.raises(failure):
            await s._handle_v1("POST", "/api/v1/system/status", {}, {}, b"{}", Writer(), api_v1.OPERATOR)
        assert current_api_timing.get() is None
    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert any(r.message.startswith("api slow ") for r in caplog.records)


def test_summary_percentiles_order_cap_and_window_reset(caplog):
    now = [0.0]
    s = server(clock=lambda: now[0])
    for value in [100, 200, 300, 2000, 2100]:
        s._record_api_latency("a", value)
    s._record_api_latency("b", 3000)
    for _ in range(20000):
        s._record_api_latency("c", 1)
    s._record_api_latency("c", 4000)
    assert len(s._api_latencies["c"]) == 20000
    now[0] = 599
    s._log_api_summary()
    assert not caplog.records
    now[0] = 600
    with caplog.at_level(logging.INFO):
        s._log_api_summary()
    lines = [r.message for r in caplog.records if r.message.startswith("api summary window=")]
    assert lines[0] == "api summary window=600s op=b n=1 p50=3000 p95=3000 p99=3000 max=3000 over_2s=1"
    assert lines[1] == "api summary window=600s op=a n=5 p50=300 p95=2100 p99=2100 max=2100 over_2s=1"
    assert "op=c n=20001" in lines[2] and "max=4000 over_2s=1" in lines[2]
    assert any("dropped=1" in r.message for r in caplog.records)
    caplog.clear()
    now[0] = 1200
    s._log_api_summary()
    assert not caplog.records and s._api_latencies == {}


def test_summary_task_is_owned_by_http_server(monkeypatch):
    async def run():
        s = server(port=0)
        await s.start()
        task = s._summary_task
        try:
            assert task is not None and not task.done()
        finally:
            await s.stop()
        assert task.done()
    asyncio.run(run())
