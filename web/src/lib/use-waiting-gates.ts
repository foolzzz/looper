import { useEffect, useRef, useState } from "react";
import { fetchGates, GATES_CHANGED_EVENT } from "@/lib/api";

const REFRESH_MS = 10_000;

/**
 * How many gates wait for the user, refreshed every 10 s and at once when this dashboard
 * changes a gate. null until the first load succeeds; an error never turns it into 0, it keeps
 * the last known count (or null).
 */
export function useWaitingGates(): number | null {
  const [count, setCount] = useState<number | null>(null);
  const latest = useRef(0);

  useEffect(() => {
    let alive = true;
    const reload = () => {
      // A slow poll must not overwrite the answer to a newer request.
      const seq = ++latest.current;
      fetchGates({ state: "open", waitingOn: "user", limit: 1 })
        .then((list) => {
          if (alive && seq === latest.current) setCount(list.counts.waiting_user);
        })
        .catch(() => {
          // Unknown stays unknown: no badge rather than a wrong 0.
        });
    };
    reload();
    const timer = setInterval(reload, REFRESH_MS);
    window.addEventListener(GATES_CHANGED_EVENT, reload);
    return () => {
      alive = false;
      clearInterval(timer);
      window.removeEventListener(GATES_CHANGED_EVENT, reload);
    };
  }, []);

  return count;
}
