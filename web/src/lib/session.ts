import { useSyncExternalStore } from "react";

let ended = false;
let installed = false;
const listeners = new Set<() => void>();

/** Rejects an /api call made after the session ended; polling code can tell it from a real failure. */
export class SessionEndedError extends Error {
  constructor() {
    super("session ended");
    this.name = "SessionEndedError";
  }
}

export function isSessionEnded(): boolean {
  return ended;
}

/** Remembers the first 401 any /api call gets. `App` then shows only the session-ended screen, which unmounts every
 * poller and stream. Every /api call settles: one that gets 401, and any made after, rejects with SessionEndedError
 * and (after the end) sends no request. */
export function installSessionGuard(): void {
  if (installed) return;
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    if (!isApi(input)) return original(input, init);
    if (ended) throw new SessionEndedError();
    const res = await original(input, init);
    if (res.status !== 401) return res;
    if (!ended) {
      ended = true;
      listeners.forEach((notify) => notify());
    }
    throw new SessionEndedError();
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

export { subscribe as subscribeSessionEnded };

export function useSessionEnded(): boolean {
  return useSyncExternalStore(subscribe, () => ended, () => false);
}

/** For tests: forget the 401 and the wrapped fetch. */
export function resetSessionGuard(): void {
  ended = false;
  installed = false;
  listeners.clear();
}
