// Install before any imports: SDK diagnostics must never become protocol frames.
let clean = (text: string) => text;
let format = (...args: unknown[]) => args.map(String).join(' ');
for (const name of ['log', 'info', 'debug', 'warn', 'error'] as const) {
  console[name] = (...args: unknown[]) => { process.stderr.write(clean(format(...args)) + '\n'); };
}
export {};
const rpc = await import('../../common/rpc.ts');
clean = rpc.scrub;
format = (await import('node:util')).format;
const { lstat } = await import('node:fs/promises');
const { isAbsolute } = await import('node:path');
const { monitorEventLoopDelay } = await import('node:perf_hooks');
const { load, boot } = await import('./sdk.ts');
const { configureSessions, sessions, newSession, loadSession, closeSession } = await import('./session.ts');
const { installProvider } = await import('./provider.ts');
const { prompt, cancel } = await import('./turn.ts');
const args = process.argv.slice(2);
try {
  if (args.length !== 4 || args[0] !== '--dsh' || !args[1] || args[2] !== '--state-root'
      || !isAbsolute(args[3]) || !(await lstat(args[3])).isDirectory()) throw new Error();
} catch {
  rpc.diagnostic('runner requires --dsh <path> --state-root <absolute real directory>'); process.exit(2);
}
configureSessions(args[3]);
const delay = monitorEventLoopDelay({ resolution: 10 });
delay.enable();
rpc.onShutdown(() => delay.disable());
function ownSignals(): void {
  for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) {
    process.removeAllListeners(signal);
    process.on(signal, () => { void rpc.shutdown(); });
  }
}
ownSignals();
rpc.method('initialize', async params => {
  if (!Array.isArray(params.protocol_versions) || !params.protocol_versions.includes('0.1')) {
    throw new rpc.RpcError(-32009, 'protocol version mismatch; require 0.1');
  }
  const host = params.host_capabilities as Record<string, unknown> | undefined;
  if (host?.tools_call !== true || host?.budget_authorize !== true) {
    throw new rpc.RpcError(-32602, 'tools_call and budget_authorize are required');
  }
  const sdk = await load(args[1]);
  await boot(args[3]);
  installProvider();
  // runProfile installs CLI exit handlers; the transport owns process lifetime.
  ownSignals();
  return { protocol_version: '0.1', runner: { kind: 'dsh', version: '0.1.0' },
    sdk: { name: '@deepseek-ai/dsh', version: sdk.version }, capabilities: {
      max_sessions: 20, per_session_isolation: true, per_session_credentials: true, cancel: true,
      usage: { per_request: true, buckets: true, reasoning_in_output: true },
      authorize_covers: ['turn', 'compaction'],
    } };
});
rpc.method('session/new', newSession, { session: true });
rpc.method('session/load', loadSession, { session: true });
rpc.method('session/close', closeSession, { session: true });
rpc.method('session/prompt', prompt, { session: true });
rpc.method('session/cancel', cancel, { session: true });
rpc.method('runner/health', () => ({ sessions: sessions.size,
  running: [...sessions.values()].filter(record => record.turn && !record.turn.settled).length,
  inflight_requests: [...sessions.values()].reduce((n, record) => n + (record.turn?.inflight.size ?? 0), 0), tool_processes: 0,
  rss_bytes: process.memoryUsage().rss, event_loop_lag_ms: Math.ceil(delay.percentile(99) / 1e6) }));
rpc.start();
