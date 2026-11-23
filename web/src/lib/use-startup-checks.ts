import { useEffect, useState } from "react";
import { fetchStatus, type SelfCheck } from "@/lib/api";

/** Re-read incomplete startup checks, and optionally live problems, without overlapping requests. */
export function useStartupChecks(refreshMs?: number): SelfCheck[] | undefined {
  const [checks, setChecks] = useState<SelfCheck[]>();
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let latest: SelfCheck[] | undefined;
    const read = async () => {
      try {
        const status = await fetchStatus(controller.signal);
        if (controller.signal.aborted) return;
        latest = status.checks ?? [];
        setChecks(latest);
      } catch {
        if (controller.signal.aborted) return;
        if (!latest) setChecks([]);
      }
      // A transient read error must not strand a previously pending check.
      const delay = latest?.some((check) => check.status === "pending") ? 3000 : refreshMs;
      if (delay !== undefined) timer = setTimeout(read, delay);
    };
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [refreshMs]);
  return checks;
}
