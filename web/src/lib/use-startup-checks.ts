import { useEffect, useState } from "react";
import { fetchStatus, type SelfCheck } from "@/lib/api";

/** Re-read incomplete startup checks without overlapping requests or polling after unmount. */
export function useStartupChecks(): SelfCheck[] | undefined {
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
      if (latest?.some((check) => check.status === "pending")) timer = setTimeout(read, 3000);
    };
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, []);
  return checks;
}
