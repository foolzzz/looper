import { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { callV1 } from "@/lib/v1";

interface RecordingSettings {
  enabled: boolean;
  include_oauth: boolean;
  proxy: { state: string; requests: number; recorded: number; dropped: number; write_errors: number };
}

async function request(action: string, body = {}): Promise<RecordingSettings> {
  const result = await callV1<RecordingSettings>(`/api/v1/llmrec_settings/${action}`, body);
  if ("status" in result) throw new Error("The recording setting was not saved.");
  return result;
}

/** Local diagnostic settings. Changing a switch applies to later launches. */
export function LlmrecSettingsSection() {
  const id = useId();
  const [settings, setSettings] = useState<RecordingSettings | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let alive = true;
    request("get").then((value) => { if (alive) setSettings(value); })
      .catch((err: unknown) => { if (alive) setError(String(err)); });
    return () => { alive = false; };
  }, [attempt]);
  const save = async (value: Partial<Pick<RecordingSettings, "enabled" | "include_oauth">>) => {
    setBusy(true);
    setError("");
    try { setSettings(await request("update", value)); }
    catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  };
  return <Card>
    <CardHeader><CardTitle>Request recording</CardTitle></CardHeader>
    <CardContent className="space-y-4">
      {settings ? <>
        <div className="space-y-1.5">
          <label htmlFor={`${id}-enabled`} className="flex min-h-11 items-center gap-2 text-sm font-medium">
            <input id={`${id}-enabled`} type="checkbox" checked={settings.enabled} disabled={busy}
              aria-describedby={`${id}-help`} onChange={(e) => void save({ enabled: e.target.checked })} />
            Record model requests
          </label>
          <p id={`${id}-help`} className="text-xs text-muted-foreground">
            Stores each model request and response of the runs that go through Looper's recording proxy, for analysis.
          </p>
          <p className="text-xs text-muted-foreground">
            Bodies can contain prompts, code and tool output, including secrets printed by tools. Only operators can read them.
          </p>
        </div>
        <div className="space-y-1.5">
          <label htmlFor={`${id}-oauth`} className="flex min-h-11 items-center gap-2 text-sm font-medium">
            <input id={`${id}-oauth`} type="checkbox" checked={settings.include_oauth} disabled={busy}
              aria-describedby={`${id}-oauth-help`} onChange={(e) => void save({ include_oauth: e.target.checked })} />
            Also record Claude subscription (OAuth) runs
          </label>
          <p id={`${id}-oauth-help`} className="text-xs text-muted-foreground">
            Off by default. Requires Record model requests; other runtime logins go directly to their provider.
          </p>
        </div>
        <p role="status" className="text-xs text-muted-foreground">
          Proxy: {settings.proxy.state}. Recorded: {settings.proxy.recorded}. Dropped: {settings.proxy.dropped}.
          {" "}Write errors: {settings.proxy.write_errors}. Changes apply to future runs.
        </p>
      </> : !error && <p role="status" className="text-sm text-muted-foreground">Loading…</p>}
      {error && <div className="space-y-2">
        <p role="alert" className="break-words text-sm text-destructive">{error}</p>
        {!settings && <Button variant="outline" onClick={() => { setError(""); setAttempt((n) => n + 1); }}>Retry</Button>}
      </div>}
    </CardContent>
  </Card>;
}
