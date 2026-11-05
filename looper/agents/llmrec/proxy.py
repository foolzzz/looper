"""Loopback HTTP proxy: forwarding is independent from the recording sink."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlsplit
from urllib.request import proxy_bypass_environment
import zlib

import h11
import httpx
import certifi

from . import ROUTE_FORMAT, ROUTE_ID, ROUTES_DIR, TLS_ENV, RouteFile, code_sha256
from .capture import MIB, build_capture, kind_of, redact_headers, redact_query

log = logging.getLogger(__name__)
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
       'transfer-encoding', 'upgrade'}


def _upstream_proxy(route, url):
    env = route.proxy_env if route.proxy_env is not None else os.environ
    proxies = {name: env.get(f'{name}_proxy', env.get(f'{name.upper()}_PROXY', ''))
               for name in ('http', 'https', 'all', 'no')}
    target = urlsplit(url)
    if proxy_bypass_environment(target.netloc, proxies):
        return None
    return proxies.get(target.scheme) or proxies['all'] or None


class _ProxyTransport(httpx.AsyncBaseTransport):
    """Pool by upstream proxy and CA configuration; trust must never cross routes."""

    def __init__(self):
        self.transports = {}

    async def handle_async_request(self, request):
        proxy = request.extensions.get('llmrec_proxy')
        tls_env = request.extensions.get('llmrec_tls', {})
        key = proxy, tuple(sorted(tls_env.items()))
        if key not in self.transports:
            context = _tls_context(tls_env)
            upstream_proxy = (httpx.Proxy(proxy, ssl_context=context)
                              if proxy and urlsplit(proxy).scheme == 'https' else proxy)
            self.transports[key] = httpx.AsyncHTTPTransport(proxy=upstream_proxy, verify=context, trust_env=False,
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
        return await self.transports[key].handle_async_request(request)

    async def aclose(self):
        for transport in self.transports.values():
            await transport.aclose()


def _tls_context(env):
    # Custom roots supplement the public bundle, including when TLS terminates at a proxy.
    context = ssl.create_default_context(cafile=certifi.where())
    for name in sorted(TLS_ENV):
        if value := env.get(name):
            context.load_verify_locations(**{'capath' if name == 'SSL_CERT_DIR' else 'cafile': value})
    return context


def _headers(pairs, *, request=False):
    connection = set()
    for key, value in pairs:
        if key.lower() == b'connection':
            connection.update(v.strip().lower() for v in value.decode('latin-1').split(','))
    return [(k, v) for k, v in pairs if k.decode('latin-1').lower() not in HOP | connection
            and not k.lower().startswith(b'proxy-') and (not request or k.lower() != b'host')]


class _ResponseBuffer:
    """Incremental decoding runs beside the raw forwarding stream, with bounded memory."""
    def __init__(self, encoding):
        self.raw = bytearray()
        self.decoded = bytearray()
        self.too_large = False
        self.failed = False
        self.decoder = None
        encoding = (encoding or '').strip().lower()
        if encoding in ('gzip', 'deflate'):
            self.decoder = zlib.decompressobj(47)
        elif encoding not in ('', 'identity'):
            self.failed = True

    def append(self, chunk):
        if len(self.raw) + len(chunk) <= 16 * MIB:
            self.raw.extend(chunk)
        else:
            self.too_large = True
        if self.failed or self.too_large:
            return
        try:
            data = self.decoder.decompress(chunk, 16 * MIB + 1 - len(self.decoded)) if self.decoder else chunk
            if len(self.decoded) + len(data) > 16 * MIB:
                self.too_large = True
            else:
                self.decoded.extend(data)
        except zlib.error:
            self.failed = True

    def finish(self):
        if self.decoder and not self.failed and not self.too_large:
            try:
                self.decoded.extend(self.decoder.flush(16 * MIB + 1 - len(self.decoded)))
                self.failed = not self.decoder.eof
            except zlib.error:
                self.failed = True


class Proxy:
    def __init__(self, runtime_root, *, sink, port=0, instance=None, connect_s=10, read_idle_s=600):
        self.root = Path(runtime_root)
        self.sink = sink
        self.port = port
        self.instance = instance or secrets.token_urlsafe(16)
        self.connect_s, self.read_idle_s = connect_s, read_idle_s
        self._stats = dict(requests=0, recorded=0, dropped=0)
        self._routes = {}
        self._handlers = set()
        self._server = None
        self._client = None
        self._code_sha256 = None

    @property
    def stats(self):
        return {**self._stats, 'write_errors': getattr(self.sink, 'write_errors', 0),
                'pending': getattr(self.sink, 'pending', 0)}

    async def start(self):
        self._code_sha256 = code_sha256()
        self._client = httpx.AsyncClient(
            transport=_ProxyTransport(), trust_env=False,
            timeout=httpx.Timeout(self.read_idle_s, connect=self.connect_s, write=60, pool=10))
        self._client.headers.clear()
        try:
            self._server = await asyncio.start_server(self._handle, '127.0.0.1', self.port)
        except BaseException:
            await self._client.aclose()
            raise
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self, grace_s=30):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        handlers = list(self._handlers)
        if handlers:
            _, pending = await asyncio.wait(handlers, timeout=grace_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
        if self._client:
            await self._client.aclose()

    def _route(self, id):
        if not ROUTE_ID.fullmatch(id):
            return None
        path = self.root / ROUTES_DIR / f'{id}.json'
        try:
            stat = path.stat()
            stamp = stat.st_mtime_ns, stat.st_size
            cached = self._routes.get(id)
            if cached is None or cached[0] != stamp:
                self._routes[id] = stamp, RouteFile.read(path)
            route = self._routes[id][1]
            return route if route and route.id == id else None
        except OSError:
            self._routes.pop(id, None)
            return None

    async def _json(self, connection, writer, status, body):
        data = json.dumps(body).encode()
        writer.write(connection.send(h11.Response(status_code=status, headers=[
            (b'content-type', b'application/json'), (b'content-length', str(len(data)).encode())])))
        writer.write(connection.send(h11.Data(data=data)))
        writer.write(connection.send(h11.EndOfMessage()))
        await writer.drain()

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._handlers.add(task)
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=1024 * 1024)
        try:
            request, body, stamped = None, bytearray(), None
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    chunk = await reader.read(65536)
                    connection.receive_data(chunk)
                    if not chunk:
                        return
                elif isinstance(event, h11.Request):
                    request, body = event, bytearray()
                    parsed = urlsplit(event.target.decode('ascii', errors='surrogateescape'))
                    prefix = parsed.path.split('/', 3)
                    route = self._route(prefix[2]) if len(prefix) >= 3 and prefix[1] == 'r' else None
                    stamped = None
                    if route is not None and not any(k.lower() == b'upgrade' for k, _ in event.headers):
                        self._stats['requests'] += 1
                        stamped = route, self._stats['requests'], datetime.now(timezone.utc), time.monotonic()
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                    if len(body) > 256 * MIB:
                        await self._json(connection, writer, 413, {'type': 'error', 'error': {'type': 'request_too_large', 'message': 'Request exceeds proxy limit'}})
                        return
                elif isinstance(event, h11.EndOfMessage):
                    buffered = await self._request(connection, reader, writer, request, bytes(body), stamped)
                    if connection.our_state is h11.DONE and connection.their_state is h11.DONE:
                        connection.start_next_cycle()
                        if buffered:
                            connection.receive_data(buffered)
                    else:
                        return
                elif isinstance(event, h11.ConnectionClosed) or event is h11.PAUSED:
                    return
        except (ConnectionError, h11.ProtocolError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Recording proxy connection failed')
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError):
                pass
            self._handlers.discard(task)

    async def _request(self, connection, reader, writer, request, body, stamped):
        target = request.target.decode('ascii', errors='surrogateescape')
        path = urlsplit(target)
        if request.method == b'GET' and path.path == '/llmrec/health':
            await self._json(connection, writer, 200, dict(instance=self.instance, pid=os.getpid(), port=self.port,
                code_sha256=self._code_sha256, route_format=ROUTE_FORMAT, **self.stats))
            return b''
        prefix = path.path.split('/', 3)
        route = stamped[0] if stamped else None
        if any(k.lower() == b'upgrade' for k, _ in request.headers):
            await self._json(connection, writer, 501, {'type': 'error', 'error': {
                'type': 'llmrec_unsupported_upgrade', 'message': 'Protocol upgrades are unsupported'}})
            return b''
        if route is None:
            await self._json(connection, writer, 404, {'type': 'error', 'error': {
                'type': 'llmrec_unknown_route', 'message': 'Unknown recording route'}})
            return b''
        rest = '/' + prefix[3] if len(prefix) == 4 else ''
        url = route.upstream.rstrip('/') + rest + ('?' + path.query if path.query else '')
        upstream_path = urlsplit(url)
        record_path = upstream_path.path + ('?' + upstream_path.query if upstream_path.query else '')
        _, seq, started_at, started = stamped
        values = dict(route=route, instance=self.instance, seq=seq,
                      record_id=route.record_id, after_record_id=route.last_record_id if route.record_id is None else None,
                      started_at=started_at, method=request.method.decode(), path=redact_query(record_path),
                      kind=kind_of(record_path),
                      request_headers=redact_headers(request.headers))
        response_sent = False
        response = None
        response_buffer = _ResponseBuffer('')
        response_bytes = 0
        values.update(status=None, outcome='completed', response_headers={})
        buffered = bytearray()

        async def watch_client():
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                if len(buffered) + len(data) > 256 * MIB:
                    return
                buffered.extend(data)

        async def exchange():
            nonlocal response_sent, response, response_buffer, response_bytes
            try:
                upstream_request = self._client.build_request(request.method.decode(), url,
                    headers=_headers(request.headers.raw_items(), request=True), content=body,
                    extensions={'llmrec_proxy': _upstream_proxy(route, url),
                                'llmrec_tls': route.tls_env if route.tls_env is not None else
                                    {name: os.environ[name] for name in TLS_ENV if name in os.environ}})
                response = await self._client.send(upstream_request, stream=True)
                values['status'] = response.status_code
                values['response_headers'] = redact_headers(response.headers.raw, response=True)
                headers = _headers(response.headers.raw)
                if not any(k.lower() == b'content-length' for k, _ in headers):
                    headers.append((b'transfer-encoding', b'chunked'))
                response_buffer = _ResponseBuffer(response.headers.get('content-encoding'))
                writer.write(connection.send(h11.Response(status_code=response.status_code, headers=headers)))
                response_sent = True
                await writer.drain()
                async for chunk in response.aiter_raw():
                    if values.get('ttfb_ms') is None:
                        values['ttfb_ms'] = round((time.monotonic() - started) * 1000)
                    writer.write(connection.send(h11.Data(data=chunk)))
                    await writer.drain()
                    response_bytes += len(chunk)
                    response_buffer.append(chunk)
                writer.write(connection.send(h11.EndOfMessage()))
                await writer.drain()
            except asyncio.CancelledError:
                values['outcome'] = 'client_closed'
                raise
            except (httpx.HTTPError, OSError) as error:
                # Broken client writes are distinct from upstream transport failures.
                values['outcome'] = 'client_closed' if isinstance(error, (BrokenPipeError, ConnectionResetError)) else 'upstream_error'
                values['error'] = type(error).__name__
                if response_sent:
                    writer.transport.abort()
                elif values['outcome'] != 'client_closed':
                    values['status'] = 502
                    await self._json(connection, writer, 502, {'type': 'error', 'error': {
                        'type': 'api_error', 'message': 'Recording proxy could not reach upstream'}})
            except Exception:
                values.update(outcome='proxy_error', error='Internal proxy error')
                log.exception('Recording proxy exchange failed route=%s', route.id)
                if response_sent:
                    writer.transport.abort()
                else:
                    values['status'] = 502
                    await self._json(connection, writer, 502, {'type': 'error', 'error': {
                        'type': 'api_error', 'message': 'Recording proxy failed'}})
            finally:
                if response is not None:
                    await response.aclose()

        forwarding, disconnected = asyncio.create_task(exchange()), asyncio.create_task(watch_client())
        try:
            done, _ = await asyncio.wait({forwarding, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done and not forwarding.done():
                forwarding.cancel()
            await asyncio.gather(forwarding, return_exceptions=True)
        finally:
            disconnected.cancel()
            if not forwarding.done():
                forwarding.cancel()
            await asyncio.gather(forwarding, disconnected, return_exceptions=True)
            values['duration_ms'] = round((time.monotonic() - started) * 1000)
            values['response_bytes'] = response_bytes
            response_buffer.finish()
            if response_buffer.too_large:
                values['record_gap'] = 'response_too_large'
            try:
                capture = await asyncio.to_thread(build_capture, request_body=body,
                    response_data=bytes(response_buffer.raw), decoded_response=bytes(response_buffer.decoded),
                    encoding_gap=response_buffer.failed, **values)
                if self.sink.offer(capture):
                    self._stats['recorded'] += 1
                else:
                    self._stats['dropped'] += 1
            except Exception:
                self._stats['dropped'] += 1
                log.exception('Recording capture failed route=%s', route.id)
            log.info('route=%s path=%s status=%s duration_ms=%s bytes=%s', route.id, upstream_path.path,
                     values['status'], values['duration_ms'], response_bytes)
        return bytes(buffered)
