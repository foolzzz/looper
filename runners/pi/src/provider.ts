/** Every HTTP attempt is authorized by the host, then ends exactly once. */
import { diagnostic, request, scrub, update } from './rpc.ts';
import { sdk } from './sdk.ts';
import type { Session } from './session.ts';
import type { Turn } from './turn.ts';

let counter = 0;
export function endRequest(record: Session, turn: Turn, id: string, status: string,
    complete: boolean, usage?: any): void {
  if (turn.settled || !turn.inflight.delete(id)) return;
  turn.ended++;
  turn.complete &&= complete;
  void update(record.id, turn.trace, 'request_end', { request_id: id, request_kind: 'turn', model: record.model,
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
  const authorization = await request('budget/authorize', { session_id: record.id, trace_id: turn.trace,
    request_id: id, kind: 'turn', estimated_input_tokens: Math.ceil(JSON.stringify(context).length / 4),
    max_output_tokens: options.maxTokens ?? model.maxTokens ?? 32000,
    provider: record.accountId, model: record.model }, { signal: options.signal }) as { allow: boolean; reason?: string };
  if (!authorization.allow) {
    turn.denial = authorization.reason ?? 'budget_unverifiable';
    void record.agent.abort();
    throw new Error(`Looper denied this model request (${turn.denial})`);
  }
  turn.inflight.add(id);
  const output = sdk!.ai.createAssistantMessageEventStream();
  let started = false, hasUsage = false, status: number | undefined;
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
      if (event.response?.usage || event.usage || event.choices?.some((choice: any) => choice.usage)) hasUsage = true;
      await options.onProviderStreamEvent?.(event, eventModel);
    },
    fetch: async (...args: Parameters<typeof globalThis.fetch>) => {
      turn.transport = true;
      const response = await fetch(...args);
      turn.httpStatus = status = response.status;
      return response;
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
          // Pi persists these same objects. Scrub before either SDK storage or protocol output.
          if (message.errorMessage) message.errorMessage = scrub(message.errorMessage).slice(0, 512);
          const aborted = options.signal?.aborted || message.stopReason === 'aborted' || turn.cancel;
          const requestStatus = event.type === 'done' ? 'completed'
            : aborted ? 'aborted' : status && status >= 400 ? `http_${status}` : 'error';
          const complete = event.type === 'done' ? hasUsage : !started && !aborted;
          // Let Pi publish message_end first; settlement waits for this accounting frame.
          setImmediate(() => endRequest(record, turn, id, requestStatus, complete, message.usage));
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
      setImmediate(() => endRequest(record, turn, id, aborted ? 'aborted' : status && status >= 400 ? `http_${status}` : 'error', !started && !aborted));
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
