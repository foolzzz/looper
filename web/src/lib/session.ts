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

const onEnd = new Set<() => void>();
const intervals = new Set<ReturnType<typeof setInterval>>();

export function isSessionEnded(): boolean {
  return ended;
}

/** Runs `stop` when the session ends (an open stream closes itself); returns a function that forgets it. */
export function onSessionEnded(stop: () => void): () => void {
  onEnd.add(stop);
  return () => onEnd.delete(stop);
}

function endSession(): void {
  if (ended) return;
  ended = true;
  intervals.forEach((id) => clearInterval(id));
  intervals.clear();
  [...onEnd].forEach((stop) => stop());
  onEnd.clear();
  listeners.forEach((notify) => notify());
}

/** Remembers the first 401 any /api call gets, so the page says once that the session ended instead of every widget
 * showing its own error. Then every polling interval is cleared, every open stream closed, and an /api call makes no
 * request: it rejects with SessionEndedError. The calls already in flight when the 401 landed never settle, so
 * their widgets show no error of their own. */
export function installSessionGuard(): void {
  if (installed) return;
  installed = true;
  const original = window.fetch.bind(window);
  const setIntervalOriginal = window.setInterval.bind(window);
  window.setInterval = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    const id = setIntervalOriginal(handler, timeout, ...args);
    if (!ended) intervals.add(id);
    return id;
  }) as typeof window.setInterval;
  window.fetch = async (input, init) => {
    if (!isApi(input)) return original(input, init);
    if (ended) throw new SessionEndedError();
    const res = await original(input, init);
    if (res.status !== 401) return res;
    endSession();
    return new Promise<Response>((_, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    });
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
  onEnd.clear();
  intervals.clear();
}
