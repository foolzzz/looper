"""Loopback HTTP proxy: forwarding is independent from the recording sink."""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from ipaddress import ip_address
import json
import hashlib
import logging
import os
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import proxy_bypass_environment
import zlib

import h11
import httpx
import certifi

try:
    import truststore
except ImportError:
    truststore = None

from looper import __version__

from . import ROUTE_FORMAT, ROUTE_ID, ROUTES_DIR, TLS_ENV, RouteFile, code_sha256
from .capture import MIB, build_capture, kind_of, redact_headers, redact_query
from .dialects import DIALECTS, ChatDialect
from .gateway import check_route_token, route_api_key, GatewayKey, GatewayConfig, AliasResolver, Unavailable, request_guard
from .translate import Rejection, RejectionCode, to_chat, from_chat, local_response, error_response, anthropic_usage, resolve_session_id
from .translate_stream import StreamTranslator, sse_events

log = logging.getLogger(__name__)
PING_INTERVAL_S = 15
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
       'transfer-encoding', 'upgrade'}


def _upstream_proxy(route, url):
    target = urlsplit(url)
    # Claude Code connects to loopback directly even when NO_PROXY omits it.
    if target.hostname == 'localhost':
        return None
    try:
        if ip_address(target.hostname).is_loopback:
            return None
    except ValueError:
        pass
    env = route.proxy_env if route.proxy_env is not None else os.environ
    proxies = {name: env.get(f'{name}_proxy', env.get(f'{name.upper()}_PROXY', ''))
               for name in ('http', 'https', 'all', 'no')}
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
    # Keep OS roots (including managed enterprise CAs) and supplement them per route.
    context = (truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if truststore is not None else
               ssl.create_default_context(cafile=certifi.where()))
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
        self.gzip = encoding == 'gzip'
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
            while chunk:
                if self.decoder and self.decoder.eof:
                    # Padding may arrive in a later chunk; keep the completed
                    # decoder until a real next member begins.
                    if self.gzip:
                        chunk = chunk.lstrip(b'\0')
                    if not chunk:
                        break
                    self.decoder = zlib.decompressobj(47)
                data = self.decoder.decompress(chunk, 16 * MIB + 1 - len(self.decoded)) if self.decoder else chunk
                if len(self.decoded) + len(data) > 16 * MIB:
                    self.too_large = True
                    return
                self.decoded.extend(data)
                chunk = self.decoder.unused_data if self.decoder else b''
        except zlib.error:
            self.failed = True

    def finish(self):
        if self.decoder and not self.failed and not self.too_large:
            try:
                self.decoded.extend(self.decoder.flush(16 * MIB + 1 - len(self.decoded)))
                self.failed = not self.decoder.eof
            except zlib.error:
                self.failed = True


class _PrefixHistory:
    """Observational state only: eviction never changes translated request bytes."""
    def __init__(self, limit=256 * MIB):
        self.limit, self.size = limit, 0
        self.items = OrderedDict()

    @staticmethod
    def _prompt(body):
        request = json.loads(body)
        messages = list(request['messages'])
        system = []
        while messages and messages[0].get('role') in ('system', 'developer'):
            system.append(messages.pop(0))
        # Prompt order is independent of the order of keys in the wire JSON.
        parts = [system, request.get('tools') or [], *messages]
        parts = [json.dumps(part, ensure_ascii=False, separators=(',', ':')).encode()
                 if part else b'' for part in parts]
        return request['model'], parts

    def remember(self, route_id, body):
        previous = self.items.pop(route_id, None)
        common = reused = total = None
        if previous is not None:
            self.size -= len(previous)
            common = 0
            for left, right in zip(previous, body):
                if left != right:
                    break
                common += 1
            model, parts = self._prompt(previous)
            current_model, current_parts = self._prompt(body)
            total = sum((len(part) + 3) // 4 for part in parts)
            reused = 0
            if model == current_model:
                for left, right in zip(parts, current_parts):
                    if left != right:
                        break
                    reused += (len(left) + 3) // 4
        if len(body) <= self.limit:
            self.items[route_id] = body
            self.size += len(body)
        while self.size > self.limit:
            _, evicted = self.items.popitem(last=False)
            self.size -= len(evicted)
        return common, reused, total


class _PromptHistory:
    """Bounded per-route usage anchors, without retaining another prompt copy."""
    def __init__(self, limit=1024):
        self.limit = limit
        self.items = OrderedDict()

    def estimate(self, route_id, body):
        previous = self.items.get(route_id)
        if previous is not None:
            count, digest, tokens = previous
            request = json.loads(body)
            prefix = {key: request[key] for key in ('model', 'messages', 'tools') if key in request}
            if len(prefix['messages']) >= count:
                size = len(json.dumps(prefix, ensure_ascii=False, separators=(',', ':')).encode())
                prefix['messages'] = prefix['messages'][:count]
                encoded = json.dumps(prefix, ensure_ascii=False, separators=(',', ':')).encode()
                # A compacted/edited history or changed model/tools needs a fresh estimate.
                if hashlib.sha256(encoded).digest() == digest:
                    return tokens + (size - len(encoded) + 3) // 4
        return (len(body) + 3) // 4

    def remember(self, route_id, body, tokens):
        request = json.loads(body)
        prompt = {key: request[key] for key in ('model', 'messages', 'tools') if key in request}
        encoded = json.dumps(prompt, ensure_ascii=False, separators=(',', ':')).encode()
        self.items.pop(route_id, None)
        self.items[route_id] = (len(prompt['messages']), hashlib.sha256(encoded).digest(), tokens)
        while len(self.items) > self.limit:
            self.items.popitem(last=False)


class Proxy:
    def __init__(self, runtime_root, *, sink, port=0, instance=None, connect_s=10, read_idle_s=None):
        self.root = Path(runtime_root)
        self.sink = sink
        self.port = port
        self.instance = instance or secrets.token_urlsafe(16)
        self.connect_s, self.read_idle_s = connect_s, read_idle_s
        self._stats = dict(requests=0, recorded=0, gaps=0, dropped=0)
        self._gap_reasons = Counter()
        self._prefix_history = _PrefixHistory()
        self._prompt_history = _PromptHistory()
        self._translate_stats = dict(requests=0, errors_by_type=Counter(), stream_errors=0,
            pings=0, usage_missing=0, cache_fields_missing=0, tool_args_invalid=0,
            dropped=Counter(), degraded=Counter(), clamped=Counter(), input_tokens=0,
            cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=0)
        self._gateway_key = GatewayKey(self.root)
        self._gateway_config = GatewayConfig(self.root)
        self._aliases = AliasResolver(lambda: getattr(self.sink, 'pool', None))
        self._auth_failures = 0
        self._alias_usage = {}
        self._routes = {}
        self._handlers = set()
        self._server = None
        self._client = None
        self._code_sha256 = None

    @property
    def stats(self):
        return {**self._stats, 'translate': self._translate_stats,
                'auth_failures': self._auth_failures, 'alias_usage': self._alias_usage, 'recorded': getattr(self.sink, 'recorded', self._stats['recorded']),
                'gaps': getattr(self.sink, 'gaps', self._stats['gaps']),
                'gap_reasons': dict(getattr(self.sink, 'gap_reasons', self._gap_reasons)),
                'write_errors': getattr(self.sink, 'write_errors', 0),
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
                    route = self._route(prefix[2]) if len(prefix) >= 3 and prefix[1] in ('r', 'r-origin') else None
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
            healthy = getattr(self.sink, 'healthy', True)
            await self._json(connection, writer, 200 if healthy else 503,
                dict(instance=self.instance, pid=os.getpid(), port=self.port, writer_healthy=healthy,
                code_sha256=self._code_sha256, route_format=ROUTE_FORMAT, features={'translate': 1}, **self.stats))
            return b''
        if path.path.startswith('/gw/'):
            return await self._gateway_request(connection, reader, writer, request, body, path.path[3:])
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
        if route.kind == 'translate':
            if not check_route_token(request.headers, route.local_token):
                self._auth_failures += 1
                await self._json(connection, writer, 401, {'type': 'error', 'error': {
                    'type': 'authentication_error', 'message': 'Invalid route token'}})
                return b''
            return await self._translated(connection, reader, writer, request, body, stamped, rest)
        upstream = urlsplit(route.upstream)
        # Redirect targets already include the complete path and query at the upstream origin.
        origin_root = prefix[1] == 'r-origin'
        url = urlunsplit((upstream.scheme, upstream.netloc, rest if origin_root else upstream.path.rstrip('/') + rest,
                         path.query if origin_root else '&'.join(query for query in (upstream.query, path.query) if query), ''))
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
                for index, (name, value) in enumerate(headers):
                    if name.lower() != b'location':
                        continue
                    try:
                        location = urlsplit(urljoin(url, value.decode('latin-1')))
                        default_ports = {'http': 80, 'https': 443}
                        same_origin = (location.scheme, location.hostname, location.port or default_ports.get(location.scheme)) == (
                            upstream.scheme, upstream.hostname, upstream.port or default_ports.get(upstream.scheme))
                    except ValueError:
                        continue
                    if same_origin:
                        headers[index] = name, urlunsplit(('', '', f'/r-origin/{route.id}' + (location.path or '/'),
                                                         location.query, location.fragment)).encode('latin-1')
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
                if capture is not None:
                    if self.sink.offer(capture):
                        if capture.record_gap:
                            self._stats['gaps'] += 1
                            self._gap_reasons[capture.record_gap] += 1
                        else:
                            self._stats['recorded'] += 1
                    else:
                        self._stats['dropped'] += 1
            except Exception:
                self._stats['dropped'] += 1
                log.exception('Recording capture failed route=%s', route.id)
            log.info('route=%s path=%s status=%s duration_ms=%s bytes=%s', route.id, upstream_path.path,
                     values['status'], values['duration_ms'], response_bytes)
        return bytes(buffered)

    async def _gateway_request(self, connection, reader, writer, request, body, path):
        config = self._gateway_config.read()
        try:
            if not config['enabled']:
                raise Rejection(404, 'not_found_error', 'Gateway endpoint is disabled')
            if rejection := request_guard(request.headers, self.port):
                raise rejection
            # Claude Code probes connectivity without credentials before its first request.
            if request.method == b'HEAD' and path == '/api/hello':
                writer.write(connection.send(h11.Response(status_code=200, headers=[(b'content-length', b'0')])))
                writer.write(connection.send(h11.EndOfMessage()))
                await writer.drain()
                return b''
            if not self._gateway_key.check(request.headers):
                self._auth_failures += 1
                raise Rejection(401, 'authentication_error', 'Invalid gateway API key')
            if any(name.lower() == b'upgrade' for name, _ in request.headers):
                raise Rejection(400, 'invalid_request_error', 'Gateway upgrades are unsupported')
            if request.method == b'GET' and path == '/v1/models':
                aliases = await self._aliases.list_aliases()
                models = [row['alias'] for row in aliases if row['enabled']]
                await self._json(connection, writer, 200, {'data': [
                    {'type': 'model', 'id': alias, 'display_name': alias,
                     'created_at': '1970-01-01T00:00:00+00:00'} for alias in models],
                    'has_more': False, 'first_id': models[0] if models else None,
                    'last_id': models[-1] if models else None})
                return b''
            if request.method == b'GET' and path == '/api/hello':
                writer.write(connection.send(h11.Response(status_code=200, headers=[(b'content-length', b'0')])))
                writer.write(connection.send(h11.EndOfMessage()))
                await writer.drain()
                return b''
            if request.method != b'POST' or path != '/v1/messages':
                raise Rejection(404, 'not_found_error', 'endpoint is not supported through the Looper gateway')
            try:
                parsed = json.loads(body)
            except (ValueError, UnicodeError):
                raise Rejection(400, 'invalid_request_error', 'Invalid JSON through the Looper gateway')
            if not isinstance(parsed, dict) or not isinstance(parsed.get('model'), str):
                raise Rejection(400, 'invalid_request_error', 'Expected a model alias through the Looper gateway')
            alias = parsed['model']
            target = await self._aliases.resolve(alias)
            if target is None:
                raise Rejection(404, 'not_found_error', 'Unknown model alias through the Looper gateway')
            now = datetime.now(timezone.utc)
            binding = '\0'.join((alias, target.provider_id, target.base_url, target.upstream_model, target.dialect))
            route = RouteFile(id='gw_' + hashlib.sha256(binding.encode()).hexdigest()[:16] + '_' + now.strftime('%Y%m%d'),
                upstream=target.base_url, kind='translate', record=config['record'], api='openai-chat',
                dialect=target.dialect, models={alias: target.upstream_model}, protocol='openai-chat',
                provider_id=target.provider_id, model=alias, agent_id='gateway:' + alias,
                runtime_type='external', created_at=now.isoformat())
            self._stats['requests'] += 1
            stamped = route, self._stats['requests'], now, time.monotonic()
            return await self._translated(connection, reader, writer, request, body, stamped, path,
                                          upstream_auth='Bearer ' + target.api_key)
        except Unavailable:
            rejection = Rejection(503, 'api_error', 'gateway configuration unavailable')
        except Rejection as error:
            rejection = error
        await self._json(connection, writer, rejection.status, {'type': 'error', 'error': {
            'type': rejection.error_type, 'message': rejection.message}})
        return b''

    async def _translated(self, connection, reader, writer, request, body, stamped, path, *, upstream_auth=None):
        route, seq, started_at, started = stamped
        stats = self._translate_stats
        stats['requests'] += 1
        dialect = DIALECTS[ChatDialect(route.dialect)]
        values = dict(route=route, instance=self.instance, seq=seq, record_id=route.record_id,
            after_record_id=route.last_record_id if route.record_id is None else None,
            started_at=started_at, method=request.method.decode(), path=path, kind=kind_of(path),
            request_headers=redact_headers(request.headers), status=None, outcome='completed',
            response_headers={})
        translation = dict(api='openai-chat', dialect=route.dialect, upstream_model=None,
            upstream_path='/chat/completions', upstream_status=None, upstream_request_id=None,
            finish_reason=None, upstream_usage=None, upstream_request_bytes=None,
            upstream_request_sha256=None, common_prefix_bytes=None,
            common_prefix_tokens=None, previous_prompt_tokens=None,
            notes={'dropped': {}, 'degraded': {}, 'clamped': {}}, usage_missing=False)
        buffer = _ResponseBuffer('')
        buffered = bytearray()
        response = None
        response_sent = False
        translator = None
        last_sent = time.monotonic()
        response_bytes = 0
        capture_request = path in ('/v1/messages', '/v1/messages/count_tokens') and request.method == b'POST'

        async def send_bytes(data):
            nonlocal last_sent, response_bytes
            if not data:
                return
            writer.write(connection.send(h11.Data(data=data)))
            # Tee the actual client-side body, including pings and errors.
            buffer.append(data)
            response_bytes += len(data)
            last_sent = time.monotonic()
            if values.get('ttfb_ms') is None:
                values['ttfb_ms'] = round((time.monotonic() - started) * 1000)
            await writer.drain()

        async def send_response(status, headers, data):
            nonlocal response_sent
            fields = [(key.encode('ascii'), value.encode('latin-1')) for key, value in headers.items()
                      if key.lower() not in ('content-length', 'transfer-encoding')]
            fields.append((b'content-length', str(len(data)).encode()))
            writer.write(connection.send(h11.Response(status_code=status, headers=fields)))
            response_sent = True
            values.update(status=status, response_headers=redact_headers(fields, response=True))
            if request.method != b'HEAD':
                await send_bytes(data)
            writer.write(connection.send(h11.EndOfMessage()))
            await writer.drain()

        def count_error(error_type):
            stats['errors_by_type'][error_type] += 1

        async def reject(error):
            count_error(error.error_type)
            if error.code is RejectionCode.TOOL_ARGS_INVALID:
                stats['tool_args_invalid'] += 1
            await send_response(error.status, {'content-type': 'application/json'}, json.dumps({
                'type': 'error', 'error': {'type': error.error_type, 'message': error.message}}).encode())

        async def events(data):
            for chunk in data:
                if chunk.startswith(b'event: ping'):
                    stats['pings'] += 1
                await send_bytes(chunk)

        async def watch_client():
            while True:
                data = await reader.read(65536)
                if not data or len(buffered) + len(data) > 256 * MIB:
                    return
                buffered.extend(data)

        async def exchange():
            nonlocal response, response_sent, translator
            try:
                local = local_response(request.method.decode(), path, route)
                if local is not None:
                    status, headers, data = local
                    if status >= 400:
                        count_error('not_found_error')
                    await send_response(status, headers, data)
                    return
                try:
                    parsed = json.loads(body)
                except (ValueError, UnicodeError):
                    raise Rejection(400, 'invalid_request_error', 'Invalid JSON through the Looper gateway')
                if not isinstance(parsed, dict):
                    raise Rejection(400, 'invalid_request_error', 'Expected an object through the Looper gateway')
                request_headers = dict(request.headers)
                session_header = request_headers.get(b'x-claude-code-session-id')
                session = resolve_session_id(parsed, session_header.decode('utf-8', errors='replace') if session_header else None)
                history_key = (route.id, session or '') if route.runtime_type == 'external' else route.id
                chat = to_chat(parsed, dialect, route.models, session_id=session)
                common, reused, previous = self._prefix_history.remember((route.id, session), chat.body)
                translation.update(upstream_model=chat.upstream_model, notes=chat.notes,
                    upstream_request_bytes=len(chat.body), upstream_request_sha256=hashlib.sha256(chat.body).hexdigest(),
                    common_prefix_bytes=common, common_prefix_tokens=reused, previous_prompt_tokens=previous)
                for category in ('dropped', 'degraded', 'clamped'):
                    stats[category].update(chat.notes.get(category, {}))
                upstream = urlsplit(route.upstream)
                url = urlunsplit(upstream._replace(path=upstream.path.rstrip('/') + '/chat/completions'))
                headers = {'content-type': 'application/json',
                    'accept': 'text/event-stream' if chat.stream else 'application/json',
                    'accept-encoding': 'gzip, deflate', 'user-agent': 'looper-gateway/' + __version__}
                # Client credentials authorize only the local route; upstream credentials come from the registry.
                auth = upstream_auth
                if auth is None:
                    try:
                        auth = 'Bearer ' + await route_api_key(route, self.sink)
                    except Unavailable:
                        raise Rejection(503, 'api_error', 'Route provider credentials unavailable')
                headers['authorization'] = auth
                upstream_request = self._client.build_request('POST', url, headers=headers, content=chat.body,
                    timeout=httpx.Timeout(self.read_idle_s if self.read_idle_s is not None else 600,
                                          connect=self.connect_s, write=60, pool=10),
                    extensions={'llmrec_proxy': _upstream_proxy(route, url),
                        'llmrec_tls': route.tls_env if route.tls_env is not None else
                            {name: os.environ[name] for name in TLS_ENV if name in os.environ}})
                # Only failures while establishing the connection are safe to retry.
                for attempt in range(2):
                    try:
                        response = await self._client.send(upstream_request, stream=True, follow_redirects=False)
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if attempt:
                            raise
                translation.update(upstream_status=response.status_code,
                    upstream_request_id=response.headers.get('x-request-id') or response.headers.get('request-id'))
                if not 200 <= response.status_code < 300:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk[:max(0, MIB - len(raw))])
                        if len(raw) >= MIB:
                            break
                    status, fields, data = error_response(response.status_code, bytes(raw), response.headers, dialect)
                    count_error(json.loads(data)['error']['type'])
                    await send_response(status, fields, data)
                elif chat.stream:
                    start_input_tokens = self._prompt_history.estimate(history_key, chat.body)
                    translation.update(start_usage_estimated=True, start_input_tokens=start_input_tokens)
                    translator = StreamTranslator(dialect, parsed['model'],
                        'msg_lgw_' + hashlib.sha256(f'{self.instance}:{seq}'.encode()).hexdigest()[:24],
                        start_input_tokens=start_input_tokens)
                    fields = [(b'content-type', b'text/event-stream'), (b'cache-control', b'no-cache'),
                              (b'transfer-encoding', b'chunked')]
                    writer.write(connection.send(h11.Response(status_code=200, headers=fields)))
                    response_sent = True
                    values.update(status=200, response_headers=redact_headers(fields, response=True))
                    await events(translator.start())
                    iterator = sse_events(response.aiter_bytes()).__aiter__()
                    pending = None
                    try:
                        while not translator.done:
                            if pending is None:
                                pending = asyncio.create_task(iterator.__anext__())
                            ready, _ = await asyncio.wait({pending}, timeout=max(0, PING_INTERVAL_S - (time.monotonic() - last_sent)))
                            if not ready:
                                await events([translator.ping()])
                                continue
                            try:
                                event = pending.result()
                            except StopAsyncIteration:
                                break
                            finally:
                                pending = None
                            if event is not None:
                                await events(translator.feed(event))
                        await events(translator.finish())
                    finally:
                        if pending is not None:
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                        await iterator.aclose()
                    translation.update(upstream_usage=translator.usage,
                                       finish_reason=translator.finish_reason)
                    if translator.error_type:
                        stats['stream_errors'] += 1
                        count_error(translator.error_type)
                        stats['tool_args_invalid'] += int(translator.tool_args_invalid)
                    writer.write(connection.send(h11.EndOfMessage()))
                    await writer.drain()
                else:
                    data = json.loads(await response.aread())
                    if isinstance(data, dict):
                        translation['upstream_usage'] = data.get('usage')
                        choices = data.get('choices')
                        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                            translation['finish_reason'] = choices[0].get('finish_reason')
                    result = from_chat(data, client_model=parsed['model'], dialect=dialect)
                    await send_response(200, {'content-type': 'application/json'},
                                        json.dumps(result, ensure_ascii=False).encode())
            except Rejection as error:
                if response_sent and translator is not None:
                    stats['stream_errors'] += 1
                    count_error(error.error_type)
                    await events(translator.fail(error.error_type, error.message))
                    writer.write(connection.send(h11.EndOfMessage()))
                    await writer.drain()
                else:
                    await reject(error)
            except asyncio.CancelledError:
                values['outcome'] = 'client_closed'
                raise
            except (BrokenPipeError, ConnectionResetError) as error:
                values.update(outcome='client_closed', error=type(error).__name__)
                writer.transport.abort()
            except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
                values.update(outcome='upstream_error', error='Gateway upstream exchange failed')
                if response_sent and translator is not None:
                    stats['stream_errors'] += 1
                    count_error('api_error')
                    await events(translator.fail('api_error', 'upstream stream interrupted'))
                    writer.write(connection.send(h11.EndOfMessage()))
                    await writer.drain()
                elif not response_sent:
                    await reject(Rejection(502, 'api_error', 'gateway could not reach upstream'))
            finally:
                if translator is not None:
                    translation.update(upstream_usage=translator.usage, finish_reason=translator.finish_reason)
                if response is not None and 200 <= response.status_code < 300:
                    raw_usage = translation['upstream_usage']
                    translation['usage_missing'] = not isinstance(raw_usage, dict) or not raw_usage
                    stats['usage_missing'] += int(translation['usage_missing'])
                    usage, cache_reported = anthropic_usage(raw_usage, dialect)
                    if isinstance(raw_usage, dict) and 'prompt_tokens' in raw_usage and not cache_reported:
                        stats['cache_fields_missing'] += 1
                    prompt_tokens = raw_usage.get('prompt_tokens') if isinstance(raw_usage, dict) else None
                    if type(prompt_tokens) is int and prompt_tokens >= 0:
                        self._prompt_history.remember(history_key, chat.body,
                            sum(usage.get(key, 0) for key in ('input_tokens', 'cache_read_input_tokens',
                                                       'cache_creation_input_tokens')))
                    for key, value in usage.items():
                        stats[key] += value
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
            values.update(duration_ms=round((time.monotonic() - started) * 1000),
                          response_bytes=response_bytes, translation=translation)
            buffer.finish()
            if buffer.too_large:
                values['record_gap'] = 'response_too_large'
            if route.record and capture_request:
                try:
                    capture = await asyncio.to_thread(build_capture, request_body=body,
                        response_data=bytes(buffer.raw), decoded_response=bytes(buffer.decoded), **values)
                    if capture is not None:
                        if not self.sink.offer(capture):
                            self._stats['dropped'] += 1
                        elif capture.record_gap:
                            self._stats['gaps'] += 1
                            self._gap_reasons[capture.record_gap] += 1
                        else:
                            self._stats['recorded'] += 1
                except Exception:
                    self._stats['dropped'] += 1
                    log.error('Gateway capture failed route=%s', route.id)
            usage, _ = anthropic_usage(translation['upstream_usage'], dialect)
            if route.runtime_type == 'external':
                counters = self._alias_usage.setdefault(route.model, dict(requests=0,
                    input_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=0))
                counters['requests'] += 1
                for key, value in usage.items():
                    counters[key] += value
            previous_tokens = translation['previous_prompt_tokens']
            prefix = translation['common_prefix_tokens']
            log.info('route=%s kind=translate upstream_host=%s upstream_model=%s status=%s finish=%s '
                     'ttfb_ms=%s duration_ms=%s prompt=%s cached=%s out=%s prefix_pct=%s', route.id,
                     urlsplit(route.upstream).hostname, translation['upstream_model'], values['status'],
                     translation['finish_reason'], values.get('ttfb_ms'), values['duration_ms'],
                     usage['input_tokens'], usage.get('cache_read_input_tokens'), usage['output_tokens'],
                     round(100 * prefix / previous_tokens, 2) if prefix is not None and previous_tokens else None)
        return bytes(buffered)
