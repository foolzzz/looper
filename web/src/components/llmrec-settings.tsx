import { useEffect, useId, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { fetchLlmrecSettings, purgeLlmRequests, updateLlmrecSettings,
  type LlmrecSettings, type LlmrecSettingsUpdate, type LlmRequestsPurgeResult } from "@/lib/api";

/** Local diagnostic settings. Changing a switch applies to later launches. */
export function LlmrecSettingsSection() {
  const id = useId();
  const [settings, setSettings] = useState<LlmrecSettings | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [days, setDays] = useState("");
  const [gib, setGib] = useState("");
  const [purgeConfirmation, setPurgeConfirmation] = useState(false);
  const [purged, setPurged] = useState<LlmRequestsPurgeResult | null>(null);
  const cancelPurge = useRef<HTMLButtonElement>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let alive = true;
    fetchLlmrecSettings().then((value) => { if (alive) {
      setSettings(value);
      setDays(String(value.retention_days.value));
      setGib(String(value.max_total_mb.value / 1024));
    } })
      .catch((err: unknown) => { if (alive) setError(String(err)); });
    return () => { alive = false; };
  }, [attempt]);
  useEffect(() => { if (purgeConfirmation) cancelPurge.current?.focus(); }, [purgeConfirmation]);
  const save = async (value: LlmrecSettingsUpdate) => {
    setBusy(true);
    setError("");
    try {
      const saved = await updateLlmrecSettings(value);
      setSettings(saved);
      if ("retention_days" in value) setDays(String(saved.retention_days.value));
      if ("max_total_mb" in value) setGib(String(saved.max_total_mb.value / 1024));
      setConfirming(false);
    }
    catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  };
  const purge = async () => {
    setBusy(true);
    setError("");
    setPurged(null);
    try {
      const result = await purgeLlmRequests();
      setPurgeConfirmation(false);
      setPurged(result);
      setSettings(await fetchLlmrecSettings());
    } catch (err) {
      setError(String(err));
      setPurgeConfirmation(false);
    } finally { setBusy(false); }
  };
  const limitsValid = !!settings && days.trim() !== "" && gib.trim() !== ""
    && Number.isInteger(Number(days)) && Number(days) >= settings.retention_days.minimum
    && Number(days) <= settings.retention_days.maximum && Number(gib) * 1024 >= settings.max_total_mb.minimum
    && Number(gib) * 1024 <= settings.max_total_mb.maximum;
  const limitsDirty = !!settings && (Number(days) !== settings.retention_days.value
    || Number(gib) * 1024 !== settings.max_total_mb.value);
  const disabled = busy || confirming || !!purgeConfirmation;
  return <Card>
    <CardHeader><CardTitle>Request recording</CardTitle></CardHeader>
    <CardContent className="space-y-4">
      {settings ? <>
        <div className="space-y-1.5">
          <div className="flex min-h-11 items-center gap-3 text-sm font-medium">
            <Switch checked={settings.enabled} disabled={disabled} label="Record model requests"
              onChange={(enabled) => enabled ? setConfirming(true) : void save({ enabled: false })} />
            <span>Record model requests</span>
          </div>
          <p className="text-xs text-muted-foreground">
            On by default. Stores model requests and replies for future runs. Running agents are unaffected until they restart.
            {" "}Stored requests stay in the database when recording is turned off.
          </p>
          {confirming && <div role="group" aria-label="Confirm request recording" className="space-y-3 rounded-md border border-border p-4">
            <p className="text-sm">
              Every model request of agents started from now on (prompts, code, tool output, model replies) will be stored in this Looper's database. Only you can read them here or through the CLI. Running agents are not affected until they restart.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button disabled={busy} onClick={() => void save({ enabled: true })}>Turn on recording</Button>
              <Button variant="outline" disabled={busy} onClick={() => { setConfirming(false); setError(""); }}>Cancel</Button>
            </div>
          </div>}
        </div>
        <div className="space-y-1.5">
          <label htmlFor={`${id}-oauth`} className="flex min-h-11 items-center gap-2 text-sm font-medium">
            <input id={`${id}-oauth`} type="checkbox" checked={settings.include_oauth} disabled={disabled}
              aria-describedby={`${id}-oauth-help`} onChange={(e) => void save({ include_oauth: e.target.checked })} />
            Also record Claude subscription (OAuth) runs
          </label>
          <p id={`${id}-oauth-help`} className="text-xs text-muted-foreground">
            Off by default. Requires Record model requests; other runtime logins go directly to their provider.
          </p>
        </div>
        <form className="space-y-3 border-t border-border pt-4" onSubmit={(event) => {
          event.preventDefault();
          if (limitsValid && limitsDirty && !disabled) void save({ retention_days: Number(days), max_total_mb: Number(gib) * 1024 });
        }}>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label htmlFor={`${id}-days`} className="text-sm font-medium">Keep recorded requests for (days)</label>
              <Input id={`${id}-days`} type="number" required min={settings.retention_days.minimum}
                max={settings.retention_days.maximum} step={1} value={days} disabled={disabled}
                aria-describedby={`${id}-days-help`} onChange={(event) => setDays(event.target.value)} />
              <p id={`${id}-days-help`} className="text-xs text-muted-foreground">
                Built-in: {settings.retention_days.built_in} days. Range: {settings.retention_days.minimum}–{settings.retention_days.maximum} days.
              </p>
            </div>
            <div className="space-y-1.5">
              <label htmlFor={`${id}-gib`} className="text-sm font-medium">Keep at most (GiB)</label>
              <Input id={`${id}-gib`} type="number" required min={settings.max_total_mb.minimum / 1024}
                max={settings.max_total_mb.maximum / 1024} step="any" value={gib} disabled={disabled}
                aria-describedby={`${id}-gib-help`} onChange={(event) => setGib(event.target.value)} />
              <p id={`${id}-gib-help`} className="text-xs text-muted-foreground">
                Built-in: {settings.max_total_mb.built_in / 1024} GiB. Range: {settings.max_total_mb.minimum / 1024}–{settings.max_total_mb.maximum / 1024} GiB.
              </p>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">Limits apply from the next hourly cleanup. The oldest requests are deleted first.</p>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={disabled || !limitsValid || !limitsDirty}>Save limits</Button>
            <Button type="button" variant="outline" disabled={disabled}
              onClick={() => void save({ retention_days: null, max_total_mb: null })}>Restore built-in limits</Button>
          </div>
        </form>
        <div className="space-y-3 border-t border-border pt-4">
          <p className="text-sm">{(settings.stored_bytes / (1024 * 1024)).toFixed(1)} MiB of {settings.max_total_mb.value} MiB · {settings.request_count} recorded requests</p>
          {purgeConfirmation ? <div role="group" aria-label="Confirm recorded request deletion" className="space-y-3 rounded-md border border-border p-4">
            <p className="whitespace-pre-wrap break-words text-sm">Permanently delete all {settings.request_count} recorded requests and their routes?</p>
            <p className="text-xs text-muted-foreground">This cannot be undone. Recording continues according to the switch above.</p>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="destructive" disabled={busy} onClick={() => void purge()}>Delete permanently</Button>
              <Button ref={cancelPurge} type="button" variant="outline" disabled={busy}
                onClick={() => { setPurgeConfirmation(false); setError(""); }}>Cancel</Button>
            </div>
          </div> : <Button type="button" variant="outline" disabled={disabled}
            onClick={() => { setPurgeConfirmation(true); setPurged(null); setError(""); }}>Delete recorded requests</Button>}
          {purged && <p role="status" className="text-sm">Deleted {purged.requests} requests and {purged.routes} routes.</p>}
        </div>
        <div role="status" className="space-y-3 border-t border-border pt-4">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span>Proxy</span>
            <Badge variant={settings.proxy.state === "running" ? "success" : settings.proxy.state === "stopped" ? "outline" : "destructive"}>
              {settings.proxy.state}
            </Badge>
            <span className="text-xs text-muted-foreground">Port: {settings.proxy.port ?? "—"}</span>
          </div>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {([ ["Requests", settings.proxy.requests], ["Recorded", settings.proxy.recorded],
              ["Dropped", settings.proxy.dropped], ["Write errors", settings.proxy.write_errors] ] as const).map(([label, value]) =>
              <div key={label} className="space-y-1">
                <dt className="text-xs text-muted-foreground">{label}</dt>
                <dd className="font-mono text-sm tabular-nums">{value}</dd>
              </div>)}
          </dl>
        </div>
      </> : !error && <p role="status" className="text-sm text-muted-foreground">Loading…</p>}
      {error && <div className="space-y-2">
        <p role="alert" className="break-words text-sm text-destructive">{error}</p>
        {!settings && <Button variant="outline" onClick={() => { setError(""); setAttempt((n) => n + 1); }}>Retry</Button>}
      </div>}
    </CardContent>
  </Card>;
}
