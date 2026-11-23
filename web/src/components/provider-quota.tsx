import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { fetchProviderLimits, updateLimitSignalsSettings, type ProviderLimits, type ProviderLimitWindow } from "@/lib/api";
import { errorText } from "@/lib/use-action";
import type { V1PendingConfirmation } from "@/lib/v1";

const time = (value: string) => new Date(value).toLocaleString();
const number = (value: number) => value.toLocaleString();

function QuotaWindow({ window }: { window: ProviderLimitWindow }) {
  const expired = window.resets_at !== null && new Date(window.resets_at).getTime() <= Date.now();
  const old = Date.now() - new Date(window.observed_at).getTime() > 24 * 60 * 60 * 1000;
  const stale = expired || old;
  const label = expired ? "Window expired" : old ? "Old signal" : {
    allowed: "Allowed", warning: "Warning", rejected: "Rejected",
  }[window.status];
  return <li className="space-y-1 border-t border-border py-2 first:border-0 first:pt-0">
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-xs font-medium">{window.window_name.replaceAll("_", " ")}</span>
      <span className="text-sm font-semibold tabular-nums">{window.utilization === null
        ? "Usage unknown" : `${Math.round(window.utilization * 100)}%${stale ? " last reported" : " used"}`}</span>
      <Badge variant={stale ? "outline" : window.status === "rejected" ? "destructive"
        : window.status === "warning" || (window.utilization ?? 0) >= 0.9 ? "warning" : "outline"}>{label}</Badge>
    </div>
    <p className="text-xs text-muted-foreground">
      {window.resets_at ? <span>Resets <time dateTime={window.resets_at}>{time(window.resets_at)}</time></span> : "Reset time unknown"}
      {window.window_minutes !== null && ` · ${number(window.window_minutes)} minute window`}
    </p>
    <p className="text-xs text-muted-foreground">Observed <time dateTime={window.observed_at}>{time(window.observed_at)}</time></p>
  </li>;
}

/** Read on expansion, independently of provider edits and model fetching. */
export function ProviderQuota({ providerId }: { providerId: string }) {
  const [result, setResult] = useState<ProviderLimits | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [pending, setPending] = useState<{ enabled: boolean; confirmation: V1PendingConfirmation } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setResult(null); setError("");
    fetchProviderLimits(providerId, controller.signal).then((next) => {
      if (!controller.signal.aborted) setResult(next);
    }).catch((err) => {
      if (!controller.signal.aborted) setError(errorText(err));
    });
    return () => controller.abort();
  }, [providerId, attempt]);
  const save = async (enabled: boolean, confirmation?: V1PendingConfirmation) => {
    setSaving(true); setSaveError("");
    try {
      const answer = await updateLimitSignalsSettings(enabled, confirmation);
      if ("status" in answer) setPending({ enabled, confirmation: answer });
      else {
        setPending(null);
        setAttempt((value) => value + 1);
      }
    } catch (err) { setSaveError(errorText(err)); }
    finally { setSaving(false); }
  };
  const provider = result?.providers.find((entry) => entry.provider_id === providerId);
  const usage = provider?.usage ?? [];
  const runs = usage.reduce((sum, day) => sum + day.runs, 0);
  const reportedTokens = usage.flatMap((day) => [day.input_tokens, day.output_tokens,
    day.cache_read_input_tokens, day.cache_creation_input_tokens]).filter((value) => value !== null);
  const tokens = reportedTokens.length ? reportedTokens.reduce((sum, value) => sum + value, 0) : null;
  const failures = usage.some((day) => day.quota_failures === null) ? null
    : usage.reduce((sum, day) => sum + day.quota_failures!, 0);
  const priced = usage.filter((day) => day.cost_usd !== null);
  const cost = priced.reduce((sum, day) => sum + day.cost_usd!, 0);
  return <section aria-label="Quota" className="mt-2 space-y-3 rounded-md border border-border bg-muted/20 p-3">
    <div className="flex flex-wrap items-center gap-2">
      <h4 className="flex-1 text-sm font-medium">Quota</h4>
      {result && <label className="flex items-center gap-2 text-xs">
        <Switch checked={result.enabled} label="Collect rate-limit signals" disabled={saving || !!pending}
          onChange={(enabled) => void save(enabled)} />
        Collect rate-limit signals
      </label>}
    </div>
    {result && <p className="text-xs text-muted-foreground">Collection applies to all providers. Off by default.</p>}
    {pending && <div role="group" aria-label="Confirm rate-limit collection" className="space-y-2 rounded-md border border-border p-3">
      <p className="text-xs">{pending.confirmation.impact}</p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={saving} onClick={() => void save(pending.enabled, pending.confirmation)}>Confirm</Button>
        <Button size="sm" variant="outline" disabled={saving} onClick={() => setPending(null)}>Cancel</Button>
      </div>
    </div>}
    {saveError && <p role="alert" className="text-xs text-destructive">{saveError}</p>}
    {error ? <div className="space-y-2">
      <p role="alert" className="break-words text-xs text-destructive">Quota unavailable: {error}</p>
      <Button size="sm" variant="outline" onClick={() => setAttempt((value) => value + 1)}>Retry</Button>
    </div> : !result ? <p role="status" className="text-xs text-muted-foreground">Loading quota…</p>
      : !result.enabled ? <p className="text-xs text-muted-foreground">Collection is off (default). Enable rate-limit signals to record quota windows.</p>
      : !provider ? <p className="text-xs text-muted-foreground">Quota is unavailable for this provider.</p> : <>
        {provider.windows.length ? <ul>{provider.windows.map((window) => <QuotaWindow key={window.window_name} window={window} />)}</ul>
          : <p className="text-xs text-muted-foreground">No quota signals received. This runtime may not report quota windows.</p>}
        <div className="space-y-1 border-t border-border pt-3 text-xs">
          <p className="font-medium">Last 7 days (UTC)</p>
          <p className="text-muted-foreground">{number(runs)} runs · {tokens === null ? "Tokens unknown" : `${number(tokens)} observed tokens`} · {failures === null
            ? "Quota failures unknown" : `${number(failures)} quota ${failures === 1 ? "failure" : "failures"}`}</p>
          <p className="text-muted-foreground">{priced.length ? `$${cost.toFixed(2)} observed cost${provider.cost_basis === "api_equivalent" ? " at API prices (equivalent, not billed)" : ""}` : "Cost unknown"}</p>
        </div>
      </>}
  </section>;
}
