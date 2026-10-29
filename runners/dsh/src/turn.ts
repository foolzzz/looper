/** One host trace owns one DSH turn and exactly one settled update. */
import { diagnostic, RpcError, scrub, update } from '../../common/rpc.ts';
import { dsh } from './sdk.ts';
import { getSession, release, type Session } from './session.ts';
import { endRequest } from './provider.ts';
import { killProcesses } from './exec.ts';
import { applyTools } from './tools.ts';

type Failure = { kind: string; message: string };
type Steer = { message_id: string; text: string; dshId: string | null; applied: boolean; dropped: boolean };
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}
export type Turn = {
  trace: string; promptId: string; number: number | null; started: boolean; settled: boolean;
  cancel: null | 'cancel' | 'close'; denial: string | null; overflow: boolean; ended: number;
  complete: boolean; inflight: Set<string>; authorizations: Map<string, Promise<unknown>>;
  requestKinds: Map<string, 'turn' | 'compaction'>; lastRequestId: string | null; afterToolBatch: boolean;
  endReason?: any; last?: any; opened: ReturnType<typeof deferred>; closed: ReturnType<typeof deferred>;
  finished: ReturnType<typeof deferred>; settling?: Promise<void>; drained?: () => void;
  forced?: boolean; failure?: Failure;
  steers: Map<string, Steer>;
};
async function within(promise: Promise<unknown>, ms: number): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try { return await Promise.race([promise.then(() => true), new Promise<boolean>(resolve => {
    timer = setTimeout(() => resolve(false), ms);
  })]); } finally { clearTimeout(timer); }
}
export function prompt(params: Record<string, any>): object {
  const record = getSession(params);
  if (record.turn && !record.turn.settled) throw new RpcError(-32003, 'session is busy');
  if (typeof params.trace_id !== 'string' || !params.trace_id.trim()
      || typeof params.text !== 'string' || !Array.isArray(params.tools)) throw new RpcError(-32602, 'invalid prompt parameters');
  applyTools(record, params.tools);
  const message = dsh.llm.createUserMessage({ content: [{ type: 'text', text: params.text }], source: { kind: 'looper' } });
  const turn: Turn = { trace: params.trace_id, promptId: message.id, number: null, started: false,
    settled: false, cancel: null, denial: null, overflow: false, ended: 0, complete: true,
    inflight: new Set(), authorizations: new Map(), requestKinds: new Map(), lastRequestId: null,
    afterToolBatch: false, opened: deferred(), closed: deferred(), finished: deferred(), steers: new Map() };
  record.turn = turn;
  // Start after the acceptance frame; controls can cancel an input still in the inbox.
  record.agent.followup(message);
  setImmediate(() => { void run(record, turn); });
  return { session_id: record.id, trace_id: turn.trace, accepted: true };
}
export function steer(params: Record<string, unknown>): object {
  const record = getSession(params), turn = record.turn;
  if (!turn || turn.settled || turn.settling || turn.cancel) throw new RpcError(-32004, 'turn is not running');
  if (params.trace_id !== turn.trace) throw new RpcError(-32005, 'trace mismatch');
  if (params.if_idle !== 'reject' || typeof params.message_id !== 'string' || !params.message_id.trim()
      || typeof params.text !== 'string' || !params.text.trim()) throw new RpcError(-32602, 'invalid steer parameters');
  if (!turn.steers.has(params.message_id)) {
    turn.steers.set(params.message_id, { message_id: params.message_id, text: params.text,
      dshId: null, applied: false, dropped: false });
    forwardSteers(record, turn);
  }
  return { session_id: record.id, trace_id: turn.trace, status: 'queued' };
}
function forwardSteers(record: Session, turn: Turn): void {
  if (record.turn !== turn || !turn.started || turn.settled || turn.settling || turn.cancel
      || turn.overflow || turn.endReason !== undefined || record.agent.status !== 'running') return;
  for (const s of turn.steers.values()) {
    if (s.dshId !== null || s.applied || s.dropped) continue;
    const message = dsh.llm.createUserMessage({ content: [{ type: 'text', text: s.text }], source: { kind: 'looper-steer' } });
    record.agent.steer(message);
    s.dshId = message.id;
  }
}
export function dropSteers(record: Session, turn: Turn): void {
  for (const s of turn.steers.values()) {
    if (s.applied || s.dropped) continue;
    if (s.dshId !== null) record.agent.inbox.remove(s.dshId);
    s.dropped = true;
    void update(record.id, turn.trace, 'steer_dropped', { message_id: s.message_id,
      reason: turn.cancel === 'close' ? 'session_closed' : turn.cancel ? 'cancelled' : 'turn_ended' });
  }
}
async function run(record: Session, turn: Turn): Promise<void> {
  try {
    await turn.opened.promise;
    await turn.closed.promise;
    await record.agent.whenIdle();
    await settle(record, turn);
  } catch (error) {
    await settle(record, turn, { kind: 'INTERNAL_ERROR', message: error instanceof Error ? error.message : 'agent failed' });
  }
}
export function onEvent(record: Session, event: any): void {
  const turn = record.turn;
  if (!turn || turn.settled || turn.settling) return;
  const data = event.data;
  if (event.type === 'turn/start') {
    if (turn.number !== null) { turn.overflow = true; return; }
    turn.number = data.turn;
    turn.started = true;
    turn.opened.resolve();
    // Inbox writes cannot reenter DSH's turn/start event publication.
    setImmediate(() => forwardSteers(record, turn));
  } else if (data.turn === turn.number) {
    if (event.type === 'assistant/message') {
      const content = data.message.content.flatMap((block: any) => {
        if (block.type === 'text') return [{ type: 'text', text: block.text }];
        if (block.type !== 'tool-call') return [];
        let input: any = {};
        try { const parsed = JSON.parse(block.arguments); if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) input = parsed; } catch { /* Invalid arguments remain an empty object. */ }
        return [{ type: 'tool_use', id: block.id, name: block.name, input }];
      });
      turn.last = content;
      turn.afterToolBatch = content.some((block: any) => block.type === 'tool_use');
      void update(record.id, turn.trace, 'message', { model: record.model, content });
    } else if (event.type === 'tool/result') {
      void update(record.id, turn.trace, 'tool_result', { call_id: data.message.toolCallId,
        text: data.message.content.filter((b: any) => b.type === 'text').map((b: any) => b.text).join('\n'),
        is_error: data.message.isError === true });
    } else if (event.type === 'turn/end') {
      turn.endReason = data.reason;
      turn.closed.resolve();
    }
  }
}
export function settle(record: Session, turn: Turn, failure?: Failure): Promise<void> {
  if (turn.settled) return turn.finished.promise;
  if (failure) turn.failure = failure;
  if (turn.settling) return turn.settling;
  turn.settling = (async () => {
    dropSteers(record, turn);
    await Promise.allSettled(turn.authorizations.values());
    if (turn.inflight.size) {
      await within(new Promise<void>(resolve => { turn.drained = resolve; }), 1000);
      for (const id of turn.inflight) endRequest(record, turn, id, 'aborted', false);
    }
    let outcome = 'failed', error = turn.failure;
    if (!turn.forced && turn.denial) outcome = turn.denial === 'max_requests' ? 'max_requests'
      : turn.denial === 'stopped' ? 'cancelled' : 'capped';
    else if (!turn.forced && turn.cancel) outcome = 'cancelled';
    else if (!error) {
      const reason = turn.endReason;
      if (reason?.kind === 'completed') outcome = 'completed';
      else if (reason?.kind === 'aborted') outcome = 'cancelled';
      else if (reason?.kind === 'max-tokens') error = { kind: 'OUTPUT_TRUNCATED', message: 'model response reached its output limit' };
      else if (reason?.kind === 'error') {
        const f = reason.error;
        const kind = f.status === 401 || f.status === 403 || ['INVALID_CREDENTIAL', 'MISSING_CREDENTIAL'].includes(f.code) ? 'PROVIDER_AUTH'
          : f.code === dsh.llm.CONTEXT_WINDOW_EXCEEDED_CODE ? 'CONTEXT_OVERFLOW'
            : f.status === 429 || f.status >= 500
              || (f.status === undefined && ['TRANSPORT', 'TIMEOUT', 'STREAM_CLOSED'].includes(f.code)) ? 'PROVIDER_UNAVAILABLE' : 'INTERNAL_ERROR';
        error = { kind, message: f.message };
      } else error = { kind: 'INTERNAL_ERROR', message: 'agent stopped without a completed response' };
    }
    if (outcome === 'cancelled' || turn.forced) killProcesses(record);
    turn.settled = true;
    await update(record.id, turn.trace, 'settled', { outcome,
      final_text: (turn.last ?? []).filter((b: any) => b.type === 'text').map((b: any) => b.text).join(''),
      requests: turn.ended, usage_complete: turn.complete,
      ...(outcome === 'failed' && error ? { error: { ...error, message: scrub(error.message).replace(/[\r\n]+/g, ' ').slice(0, 512) } } : {}) });
    turn.finished.resolve();
  })();
  return turn.settling;
}
export function cancel(params: Record<string, unknown>): object {
  const record = getSession(params), turn = record.turn;
  if (!turn || turn.settled) throw new RpcError(-32004, 'turn is not running');
  if (params.trace_id !== turn.trace) throw new RpcError(-32005, 'trace mismatch');
  turn.cancel = 'cancel';
  setImmediate(() => { void cancelTurn(record, turn); });
  return { session_id: record.id, trace_id: turn.trace };
}
export async function cancelTurn(record: Session, turn: Turn, deadline = 4000): Promise<void> {
  if (turn.settled) return;
  dropSteers(record, turn);
  if (!turn.started) {
    record.agent.inbox.remove(turn.promptId);
    turn.opened.resolve();
    turn.closed.resolve();
  }
  record.agent.cancel({ kind: 'user' }, { keepInbox: false });
  await Promise.allSettled(turn.authorizations.values());
  if (await within(turn.finished.promise, deadline)) return;
  turn.forced = true;
  killProcesses(record);
  turn.failure = { kind: 'INTERNAL_ERROR', message: 'cancel did not settle; the session was released' };
  try { await release(record, true); }
  catch { diagnostic('forced session disposal failed'); }
  for (const id of turn.inflight) endRequest(record, turn, id, 'aborted', false);
  await settle(record, turn);
}
