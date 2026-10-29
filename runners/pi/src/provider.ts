/** Every HTTP attempt is authorized by the host, then ends exactly once. */
import { subscribe } from 'node:diagnostics_channel';
import { diagnostic, request, scrub, update } from './rpc.ts';
import { sdk } from './sdk.ts';
import type { Session } from './session.ts';
import type { Turn } from './turn.ts';
import { steersSent } from './turn.ts';

const requests = new Map<string, { sent: boolean }>();
// Pi's OpenAI client uses our fetch, backed by Node's built-in undici. Subscribe
// once, and correlate the raw HTTP headers with the admitted request.
subscribe('undici:client:sendHeaders', (message: any) => {
  const id = /^x-looper-request-id: ([^\r\n]+)\r?$/im.exec(message.headers)?.[1];
  const attempt = id && requests.get(id);
  if (attempt) attempt.sent = true;
});

// Pi's parsed-event hook omits [DONE], and the SDK also accepts a clean EOF
// without it. Observe SSE data lines while forwarding the bytes unchanged.
function chatEnd(onDone: () => void): TransformStream<Uint8Array, Uint8Array> {
  const decoder = new TextDecoder();
  let pending = '', data: string[] = [];
  let afterCR = false;
  function consume(text: string) {
    if (text) {
      // Process CR immediately, including at EOF; a following LF belongs to it.
      const skipLF = afterCR && text.startsWith('\n');
      afterCR = text.endsWith('\r');
      if (skipLF) text = text.slice(1);
    }
    pending += text;
    const lines = pending.split(/\r\n|[\r\n]/);
    pending = lines.pop()!;
    for (const line of lines) {
      if (!line) {
        if (data.join('\n') === '[DONE]') onDone();
        data = [];
      } else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    }
  }
  return new TransformStream({ transform(chunk, controller) {
    consume(decoder.decode(chunk, { stream: true }));
    controller.enqueue(chunk);
  }, flush() {
    // A clean EOF can terminate the final data line and event without a blank line.
    consume(decoder.decode() + '\n\n');
  } });
}

function responseUsage(usage: any) {
  const input = usage?.input_tokens, output = usage?.output_tokens;
  const cacheRead = usage?.input_tokens_details?.cached_tokens ?? 0;
  const cacheWrite = usage?.input_tokens_details?.cache_write_tokens ?? 0;
  const reasoning = usage?.output_tokens_details?.reasoning_tokens ?? 0;
  if (![input, output, cacheRead, cacheWrite, reasoning].every(n => Number.isSafeInteger(n) && n >= 0)) return;
  // Compatible providers can disagree on aggregate totals and detail subsets.
  // Keep the billable buckets; reasoning is already included in output.
  return { input: Math.max(0, input - cacheRead - cacheWrite), output, cacheRead, cacheWrite, reasoning,
    totalTokens: input + output };
}

function chatUsage(usage: any) {
  return responseUsage({ input_tokens: usage?.prompt_tokens, output_tokens: usage?.completion_tokens,
    input_tokens_details: {
      cached_tokens: usage?.prompt_tokens_details?.cached_tokens ?? usage?.prompt_cache_hit_tokens ?? usage?.cached_tokens,
      cache_write_tokens: usage?.prompt_tokens_details?.cache_write_tokens },
    output_tokens_details: { reasoning_tokens: usage?.completion_tokens_details?.reasoning_tokens },
    total_tokens: usage?.total_tokens });
}

let counter = 0;
export function endRequest(record: Session, turn: Turn, id: string, status: string,
    complete: boolean, usage?: any): void {
  if (turn.settled || !turn.inflight.delete(id)) return;
  requests.delete(id);
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
  const attempt = { sent: false };
  requests.set(id, attempt);
  let chatFinished = false, chatDone = false, status: number | undefined;
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
        // Interim usage, and failed/incomplete responses, cannot establish final counts.
        if (event.type === 'response.completed') {
          terminalUsage = responseUsage(event.response?.usage);
        }
      } else {
        // Final Chat usage accompanies the terminal choice or follows in an empty-choices chunk.
        if (event.choices?.length) {
          chatFinished = event.choices.every((choice: any) => typeof choice.finish_reason === 'string' && choice.finish_reason.length > 0);
          terminalUsage = chatFinished ? chatUsage(event.usage ?? event.choices[0]?.usage) : undefined;
        } else if (chatFinished && Array.isArray(event.choices)) terminalUsage = chatUsage(event.usage);
      }
      await options.onProviderStreamEvent?.(event, eventModel);
    },
    fetch: async (...args: Parameters<typeof globalThis.fetch>) => {
      if (turn.cancel || turn.settled || turn.settling || options.signal?.aborted) throw new Error('request aborted');
      turn.transport = true;
      const headers = new Headers(args[1]?.headers ?? (args[0] instanceof Request ? args[0].headers : undefined));
      headers.set('x-looper-request-id', id);
      const responsePromise = fetch(args[0], { ...args[1], headers });
      steersSent(record, turn, context.messages);
      const response = await responsePromise;
      turn.httpStatus = status = response.status;
      if (model.api !== 'openai-responses' && response.ok && response.body) {
        return new Response(response.body.pipeThrough(chatEnd(() => { chatDone = true; })), {
          status: response.status, statusText: response.statusText, headers: response.headers });
      }
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
          if (terminalUsage) message.usage = { ...message.usage, ...terminalUsage };
          // Pi persists these same objects. Scrub before either SDK storage or protocol output.
          if (message.errorMessage) message.errorMessage = scrub(message.errorMessage).slice(0, 512);
          const aborted = options.signal?.aborted || message.stopReason === 'aborted' || turn.cancel;
          const requestStatus = event.type === 'done' ? 'completed'
            : aborted ? 'aborted' : status && status >= 400 ? `http_${status}` : 'error';
          const usage = attempt.sent ? terminalUsage : undefined;
          const complete = !attempt.sent || (event.type === 'done' && !aborted && terminalUsage !== undefined
            && (model.api === 'openai-responses' || chatDone));
          // Let Pi publish message_end first; settlement waits for this accounting frame.
          setImmediate(() => endRequest(record, turn, id, requestStatus, complete, usage));
          output.push(event);
          output.end();
          return;
        }
        output.push(event);
      }
      throw new Error('provider stream ended without a terminal event');
    } catch (error) {
      const aborted = !!(options.signal?.aborted || turn.cancel);
      setImmediate(() => endRequest(record, turn, id, aborted ? 'aborted' : status && status >= 400 ? `http_${status}` : 'error',
        !attempt.sent));
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
