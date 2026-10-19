import { useSyncExternalStore } from "react";

let ended = false;
let installed = false;
const listeners = new Set<() => void>();

/** Remembers the first 401 any /api call gets, so the page says once that the session ended instead of every widget
 * showing its own error. From then on an /api call makes no request and never settles (until its signal aborts), so
 * the chat watch, the pollers and any retry loop stop without each of them handling a 401. */
export function installSessionGuard(): void {
  if (installed) return;
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    if (!isApi(input)) return original(input, init);
    if (ended) return settleNever(init?.signal);
    const res = await original(input, init);
    if (res.status !== 401) return res;
    ended = true;
    listeners.forEach((notify) => notify());
    return settleNever(init?.signal);
  };
}

function settleNever(signal?: AbortSignal | null): Promise<Response> {
  return new Promise((_, reject) => {
    signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
  });
}

function isApi(input: RequestInfo | URL): boolean {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  return new URL(url, window.location.origin).pathname.startsWith("/api/");
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  return () => listeners.delete(notify);
}

export function useSessionEnded(): boolean {
  return useSyncExternalStore(subscribe, () => ended, () => false);
}

/** For tests: forget the 401 and the wrapped fetch. */
export function resetSessionGuard(): void {
  ended = false;
  installed = false;
  listeners.clear();
}
