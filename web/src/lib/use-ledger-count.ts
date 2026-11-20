import { useEffect, useState } from "react";
import { fetchLedgerState } from "@/lib/ledger-api";
import { startPolling } from "@/lib/polling";

/**
 * How many ledger items wait for the user (the sidebar's number), refreshed every 30 s. null until the first answer;
 * an error or a ledger that is off never turns into a number, so a wrong 0 is never shown.
 */
export function useLedgerCount(): number | null {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => startPolling(async (signal) => {
    try {
      const state = await fetchLedgerState(signal);
      setCount(state.enabled ? state.needs_you : null);
    } catch {
      /* unknown stays unknown */
    }
  }, 30_000), []);
  return count;
}
