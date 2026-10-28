/** Per-prompt platform tools use the host's admission, scope and audit boundary. */
import { request, RpcError, scrub } from './rpc.ts';
import type { Session } from './session.ts';

export type PlatformTool = { name: string; description: string; inputSchema: Record<string, any> };
const builtinNames = new Set(['read', 'bash', 'powershell', 'edit', 'write', 'grep', 'find', 'ls']);
function object(value: unknown): value is Record<string, any> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function result(text: string, isError: boolean): object {
  return { content: [{ type: 'text', text }], details: undefined, isError };
}
function definition(record: Session, tool: PlatformTool, hidden = false): object {
  const { name, description, inputSchema: schema } = tool;
  return { name, label: name, description,
    parameters: { ...schema, type: schema.type ?? 'object', properties: schema.properties ?? {} },
    executionMode: 'sequential', ...(hidden ? { exposure: 'hidden' } : {}),
    execute: async (toolCallId: string, args: unknown, signal?: AbortSignal) => {
      const turn = record.turn;
      if (!turn || turn.settled || turn.settling || turn.cancel || signal?.aborted) {
        return result(`Looper stopped this turn before ${name} ran`, true);
      }
      try {
        const answer = await request('tools/call', { session_id: record.id, trace_id: turn.trace,
          call_id: toolCallId, name, arguments: args }, { signal }) as { text: string; is_error: boolean };
        return result(answer.text, answer.is_error);
      } catch (error) {
        // The transport retains aborted request ids and swallows their late replies.
        return result(signal?.aborted
          ? `Stopped before Looper answered the call to ${name}; it may still have run.`
          : `Looper did not run the tool ${name}: ${scrub(error instanceof Error ? error.message : 'tool request failed')}`, true);
      }
    } };
}
export function applyTools(record: Session, snapshot: unknown[]): void {
  const next = new Map<string, PlatformTool>();
  // Validate the entire snapshot before touching the SDK or accepting a trace.
  for (const tool of snapshot) {
    if (!object(tool) || typeof tool.name !== 'string' || !/^[A-Za-z0-9_-]+$/.test(tool.name)
        || next.has(tool.name) || builtinNames.has(tool.name) || typeof tool.description !== 'string'
        || !object(tool.inputSchema) || (tool.inputSchema.type !== undefined && tool.inputSchema.type !== 'object')) {
      throw new RpcError(-32602, 'invalid platform tool snapshot');
    }
    next.set(tool.name, tool as PlatformTool);
  }
  try {
    for (const [name, tool] of next) {
      if (JSON.stringify(record.platformTools.get(name)) !== JSON.stringify(tool)) {
        record.extensionApi.registerTool(definition(record, tool));
      }
    }
    for (const [name, tool] of record.platformTools) {
      if (!next.has(name)) record.extensionApi.registerTool(definition(record, tool, true));
    }
    const active = [...record.builtinTools, ...next.keys()];
    record.extensionApi.setActiveTools(active);
    const actual: string[] = record.agent.getActiveToolNames();
    if (actual.length !== active.length || active.some(name => !actual.includes(name))) {
      throw new Error('active tool set differs from the prompt snapshot');
    }
    record.platformTools = next;
  } catch (error) {
    throw new RpcError(-32602, scrub(error instanceof Error ? error.message : 'platform tool registration failed'));
  }
}
