/** Per-prompt platform tools execute only across the host's admission boundary. */
import { request, RpcError, scrub } from '../../common/rpc.ts';
import type { Session } from './session.ts';

type PlatformTool = { name: string; description: string; inputSchema: Record<string, any> };
export type Registration = { tool: PlatformTool; dispose: () => void };
const builtinNames = new Set(['read', 'glob', 'grep', 'write', 'edit', 'bash']);
function object(value: unknown): value is Record<string, any> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
export function applyTools(record: Session, snapshot: unknown[]): void {
  const next = new Map<string, PlatformTool>();
  for (const tool of snapshot) {
    if (!object(tool) || typeof tool.name !== 'string' || !/^[A-Za-z0-9_-]+$/.test(tool.name)
        // DSH reserves run_code even when its PTC presentation is disabled.
        || next.has(tool.name) || builtinNames.has(tool.name) || tool.name === 'run_code' || typeof tool.description !== 'string'
        || !object(tool.inputSchema) || (tool.inputSchema.type !== undefined && tool.inputSchema.type !== 'object')) {
      throw new RpcError(-32602, 'invalid platform tool snapshot');
    }
    next.set(tool.name, tool as PlatformTool);
  }
  for (const [name, registration] of record.platformTools) {
    if (JSON.stringify(registration.tool) !== JSON.stringify(next.get(name))) {
      registration.dispose();
      record.platformTools.delete(name);
    }
  }
  for (const [name, tool] of next) {
    if (record.platformTools.has(name)) continue;
    const dispose = record.agentCtx.tools.register({ name, description: tool.description,
      parameters: { ...tool.inputSchema, type: 'object', properties: tool.inputSchema.properties ?? {} },
      output: { schema: { type: 'string' }, render: (_args: any, value: string) => [{ type: 'text', text: value }] },
      isConcurrencySafe: () => false,
      execute: async (args: any, exec: { signal: AbortSignal; callId: string }) => {
        const turn = record.turn;
        if (!turn || turn.settled || turn.settling || turn.cancel || exec.signal.aborted) {
          throw new Error(`Looper stopped this turn before ${name} ran`);
        }
        let answer: { text: string; is_error: boolean };
        try {
          answer = await request('tools/call', { session_id: record.id, trace_id: turn.trace,
            call_id: exec.callId, name, arguments: args }, { signal: exec.signal }) as typeof answer;
        } catch (error) {
          // Retain aborted transport ids for late replies; the host has no rpc/cancel.
          throw new Error(exec.signal.aborted
            ? `Stopped before Looper answered the call to ${name}; it may still have run.`
            : `Looper did not run the tool ${name}: ${scrub(error instanceof Error ? error.message : 'tool request failed')}`);
        }
        if (answer.is_error) throw new Error(answer.text);
        return answer.text;
      } });
    record.platformTools.set(name, { tool, dispose });
  }
  record.allowed = new Set([...record.builtin, ...next.keys()]);
}
