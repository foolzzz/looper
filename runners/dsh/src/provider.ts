/** Authorize each attributed stream before dispatch, reconcile it after durable output. */
import { subscribe } from 'node:diagnostics_channel';
import { diagnostic, request, update } from '../../common/rpc.ts';
import { ctx, dsh } from './sdk.ts';
import { sessions, type Session } from './session.ts';
import { settle, type Turn } from './turn.ts';

type Attempt = { sent: boolean; bodyDone: boolean; status?: number; onSent: () => void };
const attempts = new Map<string, Attempt>();
const owners = new WeakMap<object, string>();
let installed = false, counter = 0;

function validUsage(usage: any): boolean {
  const count = (n: unknown) => Number.isSafeInteger(n) && (n as number) >= 0;
  return usage && [usage.inputTokens, usage.outputTokens].every(count)
    && [usage.cacheReadTokens, usage.cacheWriteTokens, usage.reasoningTokens, usage.totalTokens]
      .every(n => n === undefined || count(n));
}
function finish(kind: string, message: string, code: string): any {
  return { type: 'finish', reason: { kind, failure: { message, code } } };
}
export function installProvider(): void {
  if (installed) return;
  installed = true;
  subscribe('undici:client:sendHeaders', (message: any) => {
    const id = /^x-looper-request-id:\s*([^\r\n]+)\r?$/im.exec(message.headers)?.[1];
    const attempt = id && attempts.get(id);
    if (attempt) { attempt.sent = true; owners.set(message.request, id); attempt.onSent(); }
  });
  subscribe('undici:request:headers', (message: any) => {
    const attempt = attempts.get(owners.get(message.request)!);
    if (attempt) attempt.status = message.response.statusCode;
  });
  subscribe('undici:request:trailers', (message: any) => {
    const attempt = attempts.get(owners.get(message.request)!);
    if (attempt) attempt.bodyDone = true;
  });
  ctx!.on('llm/stream', (options: any, next: () => AsyncIterable<any>) => (async function* () {
    const record = sessions.get(options.sessionId), turn = record?.turn;
    if (!record || !turn || turn.settled || turn.settling || turn.cancel || turn.overflow
        || options.provider !== record.route
        || !(dsh.llm.isAgentLoopRequest(options) || options.purpose === 'compaction')
        || record.activeRequest !== null || turn.authorizations.size) {
      diagnostic(`usage_unattributed session_id=${options.sessionId ?? 'none'}`);
      yield finish('error', 'Looper refused a model request it cannot attribute', 'LOOPER_UNATTRIBUTED');
      return;
    }
    const id = `q-${++counter}`, kind = options.purpose === 'compaction' ? 'compaction' : 'turn';
    const steers = kind === 'turn' ? [...turn.steers.values()].filter(s => !s.applied && !s.dropped
      && s.dshId !== null && options.messages.some((message: any) => message.id === s.dshId)) : [];
    const afterRequest = turn.lastRequestId, afterToolBatch = turn.afterToolBatch;
    const pending = request('budget/authorize', { session_id: record.id, trace_id: turn.trace,
      request_id: id, kind, provider: record.accountId, model: record.model,
      estimated_input_tokens: Math.ceil(JSON.stringify([options.messages, options.tools ?? []]).length / 4),
      max_output_tokens: options.maxTokens ?? dsh.deepseek.DEFAULT_MAX_TOKENS ?? 32000 });
    turn.authorizations.set(id, pending);
    let authorization: any;
    try { authorization = await pending; }
    catch {
      void settle(record, turn, { kind: 'INTERNAL_ERROR', message: 'Looper could not authorize the model request' });
      yield finish('error', 'Looper could not authorize the model request', 'LOOPER_AUTHORIZATION_ERROR');
      return;
    } finally { turn.authorizations.delete(id); }
    if (authorization.allow !== true) {
      const reason = authorization.reason ?? 'budget_unverifiable';
      if (!turn.cancel) {
        turn.denial = reason;
        record.agent.cancel({ kind: 'hook', reason: 'looper budget' });
      }
      yield finish('aborted', `Looper denied this model request (${reason})`, 'LOOPER_DENIED');
      return;
    }
    turn.inflight.add(id);
    turn.requestKinds.set(id, kind);
    if (turn.cancel || options.signal?.aborted) {
      endRequest(record, turn, id, 'aborted', true);
      yield finish('aborted', 'request aborted', 'LOOPER_CANCELLED');
      return;
    }
    const attempt: Attempt = { sent: false, bodyDone: false, onSent: () => {
      for (const s of steers) {
        if (s.applied || s.dropped) continue;
        s.applied = true;
        void update(record.id, turn.trace, 'steer_applied', { message_id: s.message_id,
          after_request_id: afterRequest, after_tool_batch: afterToolBatch, new_turn: false });
      }
    } };
    attempts.set(id, attempt);
    record.activeRequest = id;
    let usage: any, terminalUsage: any, terminal: string | undefined, modelOutput = false;
    try {
      for await (const chunk of next()) {
        if (['block-start', 'text-delta', 'reasoning-delta', 'tool-call-delta', 'block-end'].includes(chunk.type)) modelOutput = true;
        if (chunk.type === 'text-delta' && kind === 'turn' && !turn.settled) {
          void update(record.id, turn.trace, 'text_delta', { text: chunk.text });
        }
        if (chunk.type === 'usage' && validUsage(chunk.usage)) usage = { ...chunk.usage };
        if (chunk.type === 'finish') { terminal = chunk.reason.kind; terminalUsage = usage; }
        yield chunk;
      }
    } finally {
      record.activeRequest = null;
      const rejected = attempt.status !== undefined && attempt.status >= 400 && attempt.bodyDone && !modelOutput;
      const successful = terminal !== undefined && ['stop', 'tool-calls', 'max-tokens'].includes(terminal);
      const complete = !attempt.sent || rejected || (successful && terminalUsage !== undefined && attempt.bodyDone);
      const status = turn.cancel || options.signal?.aborted || terminal === 'aborted' ? 'aborted'
        : attempt.status !== undefined && attempt.status >= 400 ? `http_${attempt.status}`
          : successful ? 'completed' : 'error';
      setImmediate(() => endRequest(record, turn, id, status, complete,
        attempt.sent && !rejected ? usage : undefined));
    }
  })());
}

export function endRequest(record: Session, turn: Turn, id: string, status: string,
    complete: boolean, usage?: any): void {
  if (turn.settled || !turn.inflight.delete(id)) return;
  attempts.delete(id);
  const kind = turn.requestKinds.get(id)!;
  turn.requestKinds.delete(id);
  turn.lastRequestId = id;
  turn.ended++;
  turn.complete &&= complete;
  void update(record.id, turn.trace, 'request_end', { request_id: id, request_kind: kind,
    model: record.model, status, input_uncached: usage?.inputTokens ?? 0, output: usage?.outputTokens ?? 0,
    cache_read: usage?.cacheReadTokens ?? 0, cache_write: usage?.cacheWriteTokens ?? 0,
    reasoning: usage?.reasoningTokens ?? null, complete });
  if (!turn.inflight.size) turn.drained?.();
}
