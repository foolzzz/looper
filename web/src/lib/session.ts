import { useSyncExternalStore } from "react";

let ended = false;
let installed = false;
const listeners = new Set<() => void>();

/** Remembers the first 401 any /api call gets, so the page says once that the session ended instead of every widget
 * showing "status 401". It only reads the response: nothing is retried. */
export function installSessionGuard(): void {
  if (installed) return;
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const res = await original(input, init);
    if (res.status === 401 && !ended && isApi(input)) {
      ended = true;
      listeners.forEach((notify) => notify());
    }
    return res;
  };
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
