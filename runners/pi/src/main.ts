// Install before any imports: SDK diagnostics must never become protocol frames.
let clean = (text: string) => text;
let format = (...args: unknown[]) => args.map(String).join(' ');
for (const name of ['log', 'info', 'debug', 'warn', 'error'] as const) {
  console[name] = (...args: unknown[]) => { process.stderr.write(clean(format(...args)) + '\n'); };
}
export {};
const rpc = await import('./rpc.ts');
clean = rpc.scrub;
format = (await import('node:util')).format;
const { monitorEventLoopDelay } = await import('node:perf_hooks');
const { load } = await import('./sdk.ts');
const { sessions, newSession, loadSession, closeSession } = await import('./session.ts');
const { toolProcesses } = await import('./exec.ts');
const { prompt, cancel } = await import('./turn.ts');
const args = process.argv.slice(2);
if (args.length !== 2 || args[0] !== '--pi' || !args[1]) {
  rpc.diagnostic('runner requires --pi <path>'); process.exit(2);
}
const delay = monitorEventLoopDelay({ resolution: 10 });
delay.enable();
rpc.onShutdown(() => delay.disable());
for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) process.on(signal, () => { void rpc.shutdown(); });
rpc.method('initialize', async params => {
  if (!Array.isArray(params.protocol_versions) || !params.protocol_versions.includes('0.1')) {
    throw new rpc.RpcError(-32009, 'protocol version mismatch; require 0.1');
  }
  const host = params.host_capabilities as Record<string, unknown> | undefined;
  if (host?.tools_call !== true || host?.budget_authorize !== true) {
    throw new rpc.RpcError(-32602, 'tools_call and budget_authorize are required');
  }
  const sdk = await load(args[1]);
  return { protocol_version: '0.1', runner: { kind: 'pi', version: '0.1.0' },
    sdk: { name: '@earendil-works/pi-coding-agent', version: sdk.version }, capabilities: {
      max_sessions: 10, per_session_isolation: true, per_session_credentials: true, cancel: true,
      steer: { in_trace: false, boundary: 'after_tool_batch', idle_start: false },
      usage: { per_request: true, buckets: true, reasoning_in_output: true },
      authorize_covers: ['turn', 'retry', 'compaction'],
    } };
});
rpc.method('session/new', newSession, { session: true });
rpc.method('session/load', loadSession, { session: true });
rpc.method('session/close', closeSession, { session: true });
rpc.method('session/prompt', prompt, { session: true });
rpc.method('session/cancel', cancel, { session: true });
rpc.method('runner/health', () => ({ sessions: sessions.size,
  running: [...sessions.values()].filter(r => r.turn && !r.turn.settled).length,
  inflight_requests: [...sessions.values()].reduce((n, r) => n + (r.turn?.inflight.size ?? 0), 0), tool_processes: toolProcesses(),
  rss_bytes: process.memoryUsage().rss, event_loop_lag_ms: Math.ceil(delay.percentile(99) / 1e6) }));
rpc.start();
