import { useEffect, useState } from "react";
import { PauseCircle } from "lucide-react";
import { fetchStatus, type SystemStatus } from "@/lib/api";
import { startPolling } from "@/lib/polling";

/** Keep the last observed state visible while the server is restarting. */
export function DrainBanner() {
  const [drain, setDrain] = useState<SystemStatus["drain"]>();

  useEffect(() => startPolling(async (signal) => {
    const status = await fetchStatus(signal);
    if (!signal.aborted) setDrain(status.drain);
  }, 3000), []);

  if (drain?.phase !== "draining" && drain?.phase !== "drained") return null;
  return (
    <div role="status" className="flex shrink-0 items-start gap-2 border-b border-warning/50 bg-warning/10 px-4 py-2 text-sm text-foreground">
      <PauseCircle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
      <span>Upgrading: new turns and runs start after the restart</span>
    </div>
  );
}
