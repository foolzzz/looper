import { useEffect, useId, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { fetchLlmrecSettings, updateLlmrecSettings, type LlmrecSettings } from "@/lib/api";

/** Local diagnostic settings. Changing a switch applies to later launches. */
export function LlmrecSettingsSection() {
  const id = useId();
  const [settings, setSettings] = useState<LlmrecSettings | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let alive = true;
    fetchLlmrecSettings().then((value) => { if (alive) setSettings(value); })
      .catch((err: unknown) => { if (alive) setError(String(err)); });
    return () => { alive = false; };
  }, [attempt]);
  const save = async (value: Partial<Pick<LlmrecSettings, "enabled" | "include_oauth">>) => {
    setBusy(true);
    setError("");
    try {
      setSettings(await updateLlmrecSettings(value));
      setConfirming(false);
    }
    catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  };
  return <Card>
    <CardHeader><CardTitle>Request recording</CardTitle></CardHeader>
    <CardContent className="space-y-4">
      {settings ? <>
        <div className="space-y-1.5">
          <div className="flex min-h-11 items-center gap-3 text-sm font-medium">
            <Switch checked={settings.enabled} disabled={busy || confirming} label="Record model requests"
              onChange={(enabled) => enabled ? setConfirming(true) : void save({ enabled: false })} />
            <span>Record model requests</span>
          </div>
          <p className="text-xs text-muted-foreground">
            Stores model requests and replies for future runs. Running agents are unaffected until they restart.
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
            <input id={`${id}-oauth`} type="checkbox" checked={settings.include_oauth} disabled={busy || confirming}
              aria-describedby={`${id}-oauth-help`} onChange={(e) => void save({ include_oauth: e.target.checked })} />
            Also record Claude subscription (OAuth) runs
          </label>
          <p id={`${id}-oauth-help`} className="text-xs text-muted-foreground">
            Off by default. Requires Record model requests; other runtime logins go directly to their provider.
          </p>
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
