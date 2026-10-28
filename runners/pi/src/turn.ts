/** An accepted trace has a single terminal update, after every admitted request. */
import { RpcError, scrub, update } from './rpc.ts';
import { sdk } from './sdk.ts';
import { getSession, release } from './session.ts';
import type { Session } from './session.ts';
import { endRequest } from './provider.ts';

export type Turn = {
  trace: string; text: string; started: boolean; settled: boolean; cancel: null | 'cancel' | 'close';
  denial: string | null; ended: number; complete: boolean; inflight: Set<string>; httpStatus?: number;
  transport: boolean; last?: any; settling?: Promise<void>; drained?: () => void;
  failure?: Failure; forced?: boolean; compacting?: boolean; retryPending?: boolean;
  requestKinds?: Map<string, 'turn' | 'retry' | 'compaction'>;
  finished: Promise<void>; finish: () => void;
};
type Failure = { kind: string; message: string };
async function within(promise: Promise<unknown>, ms: number): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout>;
  try { return await Promise.race([promise.then(() => true), new Promise<boolean>(resolve => { timer = setTimeout(() => resolve(false), ms); })]); }
  finally { clearTimeout(timer!); }
}
export function prompt(params: Record<string, any>): object {
  const record = getSession(params);
  if (record.turn && !record.turn.settled) throw new RpcError(-32003, 'session is busy');
  if (typeof params.trace_id !== 'string' || !params.trace_id || typeof params.text !== 'string' || !Array.isArray(params.tools)) {
    throw new RpcError(-32602, 'invalid prompt parameters');
  }
  if (params.tools.length) throw new RpcError(-32007, 'platform tools are not enabled in this runner');
  let finish!: () => void;
  const finished = new Promise<void>(resolve => { finish = resolve; });
  const turn: Turn = { trace: params.trace_id, text: params.text, started: false, settled: false,
    cancel: null, denial: null, ended: 0, complete: true, inflight: new Set(), transport: false, finished, finish };
  record.turn = turn;
  setImmediate(() => { void run(record, turn); });
  return { session_id: record.id, trace_id: turn.trace, accepted: true };
}
async function run(record: Session, turn: Turn): Promise<void> {
  if (turn.settled) return;
  if (turn.cancel) { await settle(record, turn); return; }
  turn.started = true;
  try {
    await record.agent.prompt(turn.text, { expandPromptTemplates: false });
    await record.agent.waitForIdle();
    await settle(record, turn);
  } catch (error) {
    await settle(record, turn, { kind: 'INTERNAL_ERROR', message: scrub(error instanceof Error ? error.message : 'prompt failed').slice(0, 512) });
  }
}
export function cancel(params: Record<string, unknown>): object {
  const record = getSession(params), turn = record.turn;
  if (!turn || turn.settled) throw new RpcError(-32004, 'turn is not running');
  if (params.trace_id !== turn.trace) throw new RpcError(-32005, 'trace mismatch');
  turn.cancel = 'cancel';
  setImmediate(() => { void cancelTurn(record, turn); });
  return { session_id: record.id, trace_id: turn.trace };
}
export async function cancelTurn(record: Session, turn: Turn, deadlineMs = 4000): Promise<void> {
  if (turn.settled) return;
  if (!turn.started) { await settle(record, turn); return; }
  record.agent.clearQueue();
  void record.agent.abort().catch(() => {});
  if (await within(turn.finished, deadlineMs)) return;
  turn.failure = { kind: 'INTERNAL_ERROR', message: 'cancel did not settle; the session was released' };
  turn.forced = true;
  release(record);
  for (const id of turn.inflight) endRequest(record, turn, id, 'aborted', false);
  await settle(record, turn);
}
export function onEvent(record: Session, event: any): void {
  const turn = record.turn;
  if (!turn || turn.settled || turn.settling) return;
  if (event.type === 'auto_retry_start') turn.retryPending = true;
  else if (event.type === 'compaction_start') turn.compacting = true;
  else if (event.type === 'compaction_end') turn.compacting = false;
  else if (event.type === 'message_update' && event.assistantMessageEvent?.type === 'text_delta') {
    void update(record.id, turn.trace, 'text_delta', { text: event.assistantMessageEvent.delta });
  } else if (event.type === 'message_end') {
    const message = event.message;
    if (message.role === 'assistant') {
      if (message.errorMessage) message.errorMessage = scrub(message.errorMessage).slice(0, 512);
      turn.last = message;
      const content = message.content.flatMap((block: any) => block.type === 'text' ? [{ type: 'text', text: block.text }]
        : block.type === 'toolCall' ? [{ type: 'tool_use', id: block.id, name: block.name, input: block.arguments }] : []);
      void update(record.id, turn.trace, 'message', { model: record.model, content });
    } else if (message.role === 'toolResult') {
      void update(record.id, turn.trace, 'tool_result', { call_id: message.toolCallId,
        text: message.content.filter((b: any) => b.type === 'text').map((b: any) => b.text).join('\n'), is_error: message.isError });
    }
  } else if (event.type === 'agent_settled') void settle(record, turn);
}
export function settle(record: Session, turn: Turn, failure?: Failure): Promise<void> {
  if (failure && !turn.settled) turn.failure = failure;
  if (turn.settling) return turn.settling;
  turn.settling = (async () => {
    if (turn.inflight.size) {
      await within(new Promise<void>(resolve => { turn.drained = resolve; }), 1000);
      for (const id of turn.inflight) endRequest(record, turn, id, 'aborted', false);
    }
    let outcome = 'failed', error = turn.failure;
    if (!turn.forced && turn.denial) outcome = turn.denial === 'max_requests' ? 'max_requests' : turn.denial === 'stopped' ? 'cancelled' : 'capped';
    else if (!turn.forced && turn.cancel) outcome = 'cancelled';
    else if (!error) {
      const message = turn.last;
      if (message?.stopReason === 'stop') outcome = 'completed';
      else if (message?.stopReason === 'aborted') outcome = 'cancelled';
      else {
        const kind = message?.stopReason === 'length' ? 'OUTPUT_TRUNCATED'
          : turn.httpStatus === 401 || turn.httpStatus === 403 ? 'PROVIDER_AUTH'
          : message && sdk!.ai.isContextOverflow(message) ? 'CONTEXT_OVERFLOW'
          : (turn.httpStatus !== undefined || turn.transport) ? 'PROVIDER_UNAVAILABLE' : 'INTERNAL_ERROR';
        error = { kind, message: scrub(message?.errorMessage ?? 'agent stopped without a completed response').slice(0, 512) };
      }
    }
    turn.settled = true;
    await update(record.id, turn.trace, 'settled', { outcome, final_text: record.agent.getLastAssistantText() ?? '',
      requests: turn.ended, usage_complete: turn.complete, ...(outcome === 'failed' && error ? { error } : {}) });
    turn.finish();
  })();
  return turn.settling;
}
