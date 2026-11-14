import { startPolling } from "@/lib/polling";
import { createContext, createElement, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { fetchGates, GATES_CHANGED_EVENT } from "@/lib/api";

const REFRESH_MS = 10_000;
const WaitingGatesContext = createContext<number | null>(null);

export function useWaitingGates(): number | null {
  return useContext(WaitingGatesContext);
}

/**
 * Share how many gates wait for the user, refreshed every 10 s and at once when this dashboard
 * changes a gate. null until the first load succeeds; an error never turns it into 0, it keeps
 * the last known count (or null).
 */
export function WaitingGatesProvider({ children }: { children: ReactNode }) {
  const [count, setCount] = useState<number | null>(null);
  const latest = useRef(0);

  useEffect(() => {
    let alive = true;
    const reload = () => {
      // A slow poll must not overwrite the answer to a newer request.
      const seq = ++latest.current;
      return fetchGates({ state: "open", waitingOn: "user", limit: 1 })
        .then((list) => {
          if (alive && seq === latest.current) setCount(list.counts.waiting_user);
        })
        .catch(() => {
          // Unknown stays unknown: no badge rather than a wrong 0.
        });
    };
    const stop = startPolling(reload, REFRESH_MS);
    window.addEventListener(GATES_CHANGED_EVENT, reload);
    return () => {
      alive = false;
      stop();
      window.removeEventListener(GATES_CHANGED_EVENT, reload);
    };
  }, []);

  return createElement(WaitingGatesContext.Provider, { value: count }, children);
}
