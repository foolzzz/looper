import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { fetchChatSettings } from "@/lib/api";
import { ledgerPage } from "@/lib/api-ledger";
import { errorText } from "@/lib/use-action";
import { V1Error } from "@/lib/v1";

interface LedgerState {
  enabled: boolean | null; count: number | null; stale: boolean; error: string | null; revision: number;
  refresh: () => void; changed: () => void;
}
const LedgerContext = createContext<LedgerState>({
  enabled: false, count: null, stale: false, error: null,
  revision: 0, refresh: () => { }, changed: () => { }
});

export const useLedger = () => useContext(LedgerContext);

/** One owner for the switch and badge. A failed refresh preserves the last known count. */

export function LedgerProvider({ children }: { children: ReactNode; }) {
  const [state, setState] = useState<Omit<LedgerState, "refresh" | "changed">>({
    enabled: null, count: null,
    stale: false, error: null, revision: 0
  });
  const current = useRef(0), mounted = useRef(false), pending = useRef<AbortController | null>(null);
  const refresh = useCallback(() => {
    const sequence = ++current.current;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    const valid = () => mounted.current && sequence === current.current;
    void (async () => {
      try {
        const settings = await fetchChatSettings();
        if (!valid()) return;
        if (!settings.ledger_enabled) {
          setState((old) => ({ ...old, enabled: false, count: null, stale: false, error: null }));
          return;
        }
        setState((old) => ({ ...old, enabled: true }));
        const result = await ledgerPage({ section: "attention", limit: 1 }, controller.signal);
        if (valid()) setState((old) => ({ ...old, count: result.attention_count, stale: false, error: null }));
      } catch (err) {
        if (valid()) setState((old) => ({
          ...old, stale: true,
          error: err instanceof V1Error && err.status === 403 ? "无权访问账本" : errorText(err)
        }));
      }
    })();
  }, []);
  const changed = useCallback(() => {
    setState((old) => ({ ...old, revision: old.revision + 1 }));
    refresh();
  }, [refresh]);
  useEffect(() => {
    mounted.current = true;
    refresh();
    const timer = window.setInterval(refresh, 30_000);
    return () => {
      mounted.current = false;
      ++current.current;
      pending.current?.abort();
      window.clearInterval(timer);
    };
  }, [refresh]);
  return <LedgerContext.Provider value={{ ...state, refresh, changed }}>
    {children}
  </LedgerContext.Provider>;
}
