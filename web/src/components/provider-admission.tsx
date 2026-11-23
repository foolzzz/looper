import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { clearProviderLimits, fetchAdmissionSettings, fetchProviderLimits, updateAdmissionSettings,
  type ProviderAdmissionStatus } from "@/lib/api";
import { errorText } from "@/lib/use-action";
import type { V1PendingConfirmation } from "@/lib/v1";

function Confirmation({ pending, busy, label, confirm, cancel }: {
  pending: V1PendingConfirmation; busy: boolean; label: string; confirm: () => void; cancel: () => void;
}) {
  return <div role="group" aria-label={label} className="space-y-2 rounded-md border border-border p-3">
    <p className="break-words text-xs">{pending.impact}</p>
    <div className="flex flex-wrap gap-2">
      <Button size="sm" disabled={busy} onClick={confirm}>Confirm</Button>
      <Button size="sm" variant="outline" disabled={busy} onClick={cancel}>Cancel</Button>
    </div>
  </div>;
}

/** This global switch has its own confirmation and never edits chat settings. */
export function AdmissionSettings({ onEnabled }: { onEnabled: (enabled: boolean) => void }) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<{ enabled: boolean; confirmation: V1PendingConfirmation } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    fetchAdmissionSettings(controller.signal).then((result) => {
      if (!controller.signal.aborted) { setEnabled(result.enabled); onEnabled(result.enabled); }
    }).catch((err) => { if (!controller.signal.aborted) setError(errorText(err)); });
    return () => controller.abort();
  }, [onEnabled]);
  const save = async (next: boolean, confirmation?: V1PendingConfirmation) => {
    setBusy(true); setError("");
    try {
      const answer = await updateAdmissionSettings(next, confirmation);
      if ("status" in answer) setPending({ enabled: next, confirmation: answer });
      else { setPending(null); setEnabled(answer.enabled); onEnabled(answer.enabled); }
    } catch (err) { setError(errorText(err)); }
    finally { setBusy(false); }
  };
  return <div className="mb-4 space-y-2 rounded-md border border-border p-3">
    <label className="flex items-center gap-2 text-xs">
      <Switch label="Account admission" checked={enabled === true} disabled={enabled === null || busy || !!pending}
        onChange={(next) => void save(next)} />
      Account admission
    </label>
    <p className="text-xs text-muted-foreground">Account slots, priority lanes and quota freezes. Off by default. Planner and Manager turns are not admitted yet.</p>
    {pending && <Confirmation label="Confirm account admission" pending={pending.confirmation} busy={busy}
      confirm={() => void save(pending.enabled, pending.confirmation)} cancel={() => setPending(null)} />}
    {error && <p role="alert" className="break-words text-xs text-destructive">Admission settings unavailable: {error}</p>}
  </div>;
}

/** Admission is visible on the account row only while its independent switch is on. */
export function ProviderAdmission({ providerId, enabled }: { providerId: string; enabled: boolean }) {
  const [status, setStatus] = useState<ProviderAdmissionStatus | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<V1PendingConfirmation | null>(null);
  useEffect(() => {
    if (!enabled) { setStatus(null); setError(""); return; }
    const controller = new AbortController();
    fetchProviderLimits(providerId, controller.signal).then((result) => {
      if (!controller.signal.aborted) { setStatus(result.providers.find((provider) => provider.provider_id === providerId)?.admission ?? null); setError(""); }
    }).catch((err) => { if (!controller.signal.aborted) setError(errorText(err)); });
    const timer = window.setInterval(() => setAttempt((value) => value + 1), 30_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [providerId, enabled, attempt]);
  const clear = async (confirmation?: V1PendingConfirmation) => {
    setBusy(true); setError("");
    try {
      const answer = await clearProviderLimits(providerId, confirmation);
      if ("status" in answer) setPending(answer);
      else { setPending(null); setAttempt((value) => value + 1); }
    } catch (err) { setError(errorText(err)); }
    finally { setBusy(false); }
  };
  if (!enabled) return null;
  // Admission waits an extra minute after the provider resets. Show the provider's reset time.
  const resetsAt = status?.until_known && status.until
    ? new Date(new Date(status.until).getTime() - 60_000).toISOString() : null;
  return <div className="min-w-0 space-y-2 pt-2 text-xs">
    {status && <>
      <div className="flex flex-wrap items-center gap-2">
        <span>Admission</span>
        <Badge variant={status.state === "frozen" ? "warning" : "outline"}>{ {
          open: "Open", frozen: "Frozen", probing: "Probing", degraded: "Degraded",
        }[status.state]}</Badge>
        <span>{Object.values(status.waiting).reduce((total, count) => total + count, 0)} waiting · {status.running} running · {status.limit === null ? "no concurrency limit" : `limit ${status.limit}`}</span>
        <Button size="sm" variant="outline" disabled={busy || !!pending} onClick={() => void clear()}>Check now</Button>
      </div>
      {status.state !== "open" && <p className="text-muted-foreground">{resetsAt
        ? <>Resets <time dateTime={resetsAt}>{new Date(resetsAt).toLocaleString()}</time></> : "Reset time unknown"}</p>}
    </>}
    {pending && <Confirmation label="Confirm account check" pending={pending} busy={busy}
      confirm={() => void clear(pending)} cancel={() => setPending(null)} />}
    {error && <p role="alert" className="break-words text-destructive">Admission unavailable: {error}</p>}
  </div>;
}
