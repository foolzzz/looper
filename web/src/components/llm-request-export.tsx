import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { exportLlmRequests, fetchLlmrecSettings, fetchLlmRequests, type LlmRequestFilter } from "@/lib/api";

/** A scoped export owns its cancellation, including navigation away from the scope. */
export function LlmRequestExport({ filter, label = "Export model requests", checkVisibility = false }: {
  filter: LlmRequestFilter; label?: string; checkVisibility?: boolean;
}) {
  const [visible, setVisible] = useState(!checkVisibility);
  const [count, setCount] = useState<number | null>(null);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const scope = JSON.stringify(filter);
  useEffect(() => {
    let alive = true;
    setVisible(!checkVisibility);
    setCount(null);
    setError("");
    if (checkVisibility) {
      Promise.all([fetchLlmrecSettings().catch(() => null), fetchLlmRequests({ ...JSON.parse(scope), limit: 1 }).catch(() => [])])
        .then(([settings, rows]) => { if (alive) setVisible(!!settings?.enabled || rows.length > 0); });
    }
    return () => { alive = false; controller.current?.abort(); controller.current = null; };
  }, [scope, checkVisibility]);
  if (!visible) return null;
  const start = async () => {
    const active = new AbortController();
    controller.current = active;
    setCount(0);
    setError("");
    try { await exportLlmRequests(filter, { signal: active.signal, onProgress: setCount }); }
    catch (err) { if (!active.signal.aborted) setError(err instanceof Error ? err.message : String(err)); }
    finally { if (controller.current === active) { controller.current = null; setCount(null); } }
  };
  return <div className="flex flex-wrap items-center gap-2">
    <Button variant="outline" size="sm" className="min-h-11" disabled={count !== null} onClick={() => void start()}>{label}</Button>
    {count !== null && <>
      <span role="status" className="text-xs text-muted-foreground">Exporting · {count} requests</span>
      <Button variant="ghost" size="sm" className="min-h-11" onClick={() => controller.current?.abort()}>Cancel</Button>
    </>}
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
  </div>;
}
