/** Every HTTP attempt is authorized by the host, then ends exactly once. */
import { diagnostic, request, scrub, update } from './rpc.ts';
import { sdk } from './sdk.ts';
import type { Session } from './session.ts';
import type { Turn } from './turn.ts';
import { steersSent } from './turn.ts';

const preRequestErrors = new Set([
  'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'UND_ERR_CONNECT_TIMEOUT',
  // Certificate validation fails during the TLS handshake, before HTTP is sent.
  'CERT_HAS_EXPIRED', 'CERT_NOT_YET_VALID', 'DEPTH_ZERO_SELF_SIGNED_CERT', 'SELF_SIGNED_CERT_IN_CHAIN',
  'UNABLE_TO_GET_ISSUER_CERT', 'UNABLE_TO_GET_ISSUER_CERT_LOCALLY', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
  'CERT_REVOKED', 'CERT_UNTRUSTED', 'CERT_REJECTED', 'ERR_TLS_CERT_ALTNAME_INVALID',
]);

function responseUsage(usage: any) {
  const input = usage?.input_tokens, output = usage?.output_tokens;
  const cacheRead = usage?.input_tokens_details?.cached_tokens ?? 0;
  const cacheWrite = usage?.input_tokens_details?.cache_write_tokens ?? 0;
  const reasoning = usage?.output_tokens_details?.reasoning_tokens ?? 0;
  if (![input, output, cacheRead, cacheWrite, reasoning].every(n => Number.isSafeInteger(n) && n >= 0)
      || cacheRead + cacheWrite > input || reasoning > output
      || (usage.total_tokens != null && usage.total_tokens !== input + output)) return;
  return { input: input - cacheRead - cacheWrite, output, cacheRead, cacheWrite, reasoning,
    totalTokens: input + output };
}

let counter = 0;
export function endRequest(record: Session, turn: Turn, id: string, status: string,
    complete: boolean, usage?: any): void {
  if (turn.settled || !turn.inflight.delete(id)) return;
  turn.lastRequestId = id;
  turn.ended++;
  turn.complete &&= complete;
  const kind = turn.requestKinds?.get(id) ?? 'turn';
  turn.requestKinds?.delete(id);
  void update(record.id, turn.trace, 'request_end', { request_id: id, request_kind: kind, model: record.model,
    status, input_uncached: usage?.input ?? 0, output: usage?.output ?? 0,
    cache_read: usage?.cacheRead ?? 0, cache_write: usage?.cacheWrite ?? 0, reasoning: null, complete });
  if (!turn.inflight.size) turn.drained?.();
}
export async function authorizingStream(record: Session, model: any, context: any, options: any = {}): Promise<any> {
  const turn = record?.turn;
  if (!turn || turn.cancel || turn.settled || turn.settling) {
    diagnostic('usage_unattributed');
    throw new Error('model request has no active turn');
  }
  const id = `q-${++counter}`;
  const kind = turn.compacting ? 'compaction' : turn.retryPending ? 'retry' : 'turn';
  turn.retryPending = false;
  // Authorization cannot be abandoned on abort: the host may already have reserved budget.
  const pending = request('budget/authorize', { session_id: record.id, trace_id: turn.trace,
    request_id: id, kind, estimated_input_tokens: Math.ceil(JSON.stringify(context).length / 4),
    max_output_tokens: options.maxTokens ?? model.maxTokens ?? 32000,
    provider: record.accountId, model: record.model });
  (turn.authorizations ??= new Map()).set(id, pending);
  let authorization: { allow: boolean; reason?: string };
  try { authorization = await pending as typeof authorization; }
  finally { turn.authorizations.delete(id); }
  if (!authorization.allow) {
    if (turn.cancel || options.signal?.aborted) throw new Error('request aborted');
    turn.denial = authorization.reason ?? 'budget_unverifiable';
    void record.agent.abort();
    throw new Error(`Looper denied this model request (${turn.denial})`);
  }
  turn.inflight.add(id);
  (turn.requestKinds ??= new Map()).set(id, kind);
  if (turn.cancel || turn.settling || options.signal?.aborted) {
    endRequest(record, turn, id, 'aborted', true);
    throw new Error('request aborted');
  }
  const output = sdk!.ai.createAssistantMessageEventStream();
  let started = false, dispatched = false, hasUsage = false, status: number | undefined;
  let terminalUsage: ReturnType<typeof responseUsage>;
  turn.httpStatus = undefined;
  turn.transport = false;
  // onResponse only runs for successful responses in Pi 1.3, so capture HTTP errors in fetch as well.
  const fetch = options.fetch ?? globalThis.fetch;
  const wrapped = { ...options,
    onPayload: async (payload: any, payloadModel: any) => {
      // Pi 1.3 serializes the initial prompt as an input message; Responses uses instructions here.
      const first = payload.input?.[0];
      if (model.api === 'openai-responses' && (first?.role === 'system' || first?.role === 'developer')) {
        payload.instructions = first.content;
        payload.input = payload.input.slice(1);
      }
      return await options.onPayload?.(payload, payloadModel) ?? payload;
    },
    onProviderStreamEvent: async (event: any, eventModel: any) => {
      if (model.api === 'openai-responses') {
        // Earlier events may carry usage that Pi never extracts. Trust only validated
        // terminal counts, including response.failed, which Pi throws before copying.
        if (event.type === 'response.completed' || event.type === 'response.incomplete' || event.type === 'response.failed') {
          terminalUsage = responseUsage(event.response?.usage);
        }
      } else if (event.response?.usage || event.usage || event.choices?.some((choice: any) => choice.usage)) hasUsage = true;
      await options.onProviderStreamEvent?.(event, eventModel);
    },
    fetch: async (...args: Parameters<typeof globalThis.fetch>) => {
      if (turn.cancel || turn.settled || turn.settling || options.signal?.aborted) throw new Error('request aborted');
      turn.transport = true;
      dispatched = true;
      try {
        const responsePromise = fetch(...args);
        steersSent(record, turn, context.messages);
        const response = await responsePromise;
        turn.httpStatus = status = response.status;
        return response;
      } catch (error) {
        // These connection failures establish that no HTTP request was sent. Other
        // failures (including a socket closing before headers) leave usage unknown.
        const code = (error as { cause?: { code?: string } })?.cause?.code;
        if (code && preRequestErrors.has(code)) dispatched = false;
        throw error;
      }
    },
    onResponse: async (response: any, responseModel: any) => {
      turn.httpStatus = status = response.status;
      await options.onResponse?.(response, responseModel);
    } };
  void (async () => {
    try {
      if (turn.cancel || turn.settled || options.signal?.aborted) throw new Error('request aborted');
      const source = (model.api === 'openai-responses' ? sdk!.responses : sdk!.completions).streamSimple(model, context, wrapped);
      for await (const event of source) {
        if (event.type === 'done' || event.type === 'error') {
          const message = event.type === 'done' ? event.message : event.error;
          if (terminalUsage) message.usage = { ...message.usage, ...terminalUsage };
          // Pi persists these same objects. Scrub before either SDK storage or protocol output.
          if (message.errorMessage) message.errorMessage = scrub(message.errorMessage).slice(0, 512);
          const aborted = options.signal?.aborted || message.stopReason === 'aborted' || turn.cancel;
          const requestStatus = event.type === 'done' ? 'completed'
            : aborted ? 'aborted' : status && status >= 400 ? `http_${status}` : 'error';
          const usage = model.api === 'openai-responses' ? terminalUsage : message.usage;
          const usageComplete = model.api === 'openai-responses' ? terminalUsage !== undefined : hasUsage;
          const complete = event.type === 'done' ? usageComplete
            : usageComplete || !dispatched || (!started && !aborted && status !== undefined && status >= 400);
          // Let Pi publish message_end first; settlement waits for this accounting frame.
          setImmediate(() => endRequest(record, turn, id, requestStatus, complete, usage));
          output.push(event);
          output.end();
          return;
        }
        started = true;
        output.push(event);
      }
      throw new Error('provider stream ended without a terminal event');
    } catch (error) {
      const aborted = !!(options.signal?.aborted || turn.cancel);
      setImmediate(() => endRequest(record, turn, id, aborted ? 'aborted' : status && status >= 400 ? `http_${status}` : 'error',
        !dispatched || (!started && !aborted && status !== undefined && status >= 400)));
      const message = { role: 'assistant', content: [], api: model.api, provider: model.provider, model: model.id,
        stopReason: aborted ? 'aborted' : 'error', errorMessage: scrub(error instanceof Error ? error.message : 'provider failed').slice(0, 512),
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }, timestamp: Date.now() };
      output.push({ type: 'error', reason: message.stopReason, error: message });
      output.end();
    }
  })();
  return output;
}
