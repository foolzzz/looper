import { useEffect, useId, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SELECT } from "@/components/registry-ui";
import { useRegistry } from "@/lib/use-registry";
import {
  fetchGatewaySettings, updateGatewaySettings, setGatewayAlias, deleteGatewayAlias,
  revealGatewayKey, rotateGatewayKey, type GatewaySettings, type GatewayUsage,
} from "@/lib/api";

function hitRate(usage: GatewayUsage) {
  const total = (usage.input_tokens ?? 0) + (usage.cache_read_input_tokens ?? 0) + (usage.cache_creation_input_tokens ?? 0);
  return total ? `${Math.round((usage.cache_read_input_tokens ?? 0) / total * 100)}%` : "—";
}

/** Operator-only configuration; credentials are fetched only after a deliberate action. */
export function GatewaySettingsSection() {
  const id = useId();
  const { registry, error: registryError, reload } = useRegistry();
  const [settings, setSettings] = useState<GatewaySettings | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [port, setPort] = useState("");
  const [alias, setAlias] = useState("");
  const [model, setModel] = useState("");
  const [exampleAlias, setExampleAlias] = useState("");
  const [key, setKey] = useState("");
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const wasConfirming = useRef(false);
  const cancel = useRef<HTMLButtonElement>(null);
  const rotateButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    let alive = true;
    fetchGatewaySettings().then((value) => { if (alive) {
      setSettings(value); setPort(value.port == null ? "" : String(value.port));
    } }).catch((err: unknown) => { if (alive) setError(String(err)); });
    return () => { alive = false; };
  }, [attempt]);
  useEffect(() => {
    if (confirming) { cancel.current?.focus(); wasConfirming.current = true; }
    else if (wasConfirming.current && !busy) { rotateButton.current?.focus(); wasConfirming.current = false; }
  }, [confirming, busy]);
  const action = async (operation: () => Promise<void>) => {
    setBusy(true); setError(""); setCopied(false);
    try { await operation(); } catch (err) { setError(String(err)); } finally { setBusy(false); }
  };
  const apply = (value: GatewaySettings) => { setSettings(value); setPort(value.port == null ? "" : String(value.port)); };
  const models = registry?.models.filter((entry) => entry.protocol === "openai-chat"
    && registry.providers.some((provider) => provider.provider_id === entry.provider_id
      && provider.auth === "api_key" && provider.api_key && provider.enabled)) ?? [];
  const validPort = port === "" || (Number.isInteger(Number(port)) && Number(port) >= 1 && Number(port) <= 65535);
  const disabled = busy || confirming;
  const selectedAlias = settings?.aliases.some((entry) => entry.alias === exampleAlias) ? exampleAlias : settings?.aliases[0]?.alias;
  const env = settings?.url && selectedAlias ? settings.client_environment[selectedAlias] ?? "" : "";
  return <Card className="min-w-0">
    <CardHeader><CardTitle>Local gateway</CardTitle></CardHeader>
    <CardContent className="min-w-0 space-y-5">
      {settings ? <>
        <div className="space-y-2">
          <div className="flex min-h-11 items-center gap-3 text-sm font-medium">
            <Switch label="Enable local gateway" checked={settings.enabled} disabled={disabled || !validPort}
              onChange={(enabled) => void action(async () => apply(await updateGatewaySettings({ enabled, ...(port ? { port: Number(port) } : {}) })))} />
            <span>Enable local gateway</span>
            <Badge variant={settings.enabled ? "success" : "outline"}>{settings.enabled ? "Enabled" : "Disabled"}</Badge>
          </div>
          <p className="text-xs text-muted-foreground">Accepts authenticated clients on this machine only. Requests follow the Request recording setting.</p>
          <p className="break-all font-mono text-sm">{settings.url ?? (settings.port ? `Port ${settings.port}` : "The URL is assigned when you first enable the gateway.")}</p>
          <form className="flex flex-wrap items-end gap-3" onSubmit={(event) => {
            event.preventDefault();
            if (validPort && port) void action(async () => apply(await updateGatewaySettings({ enabled: settings.enabled, port: Number(port) })));
          }}>
            <div className="space-y-1.5">
              <label htmlFor={`${id}-port`} className="text-sm font-medium">Port</label>
              <Input id={`${id}-port`} type="number" min={1} max={65535} step={1} placeholder="Assigned on first enable"
                value={port} disabled={disabled} onChange={(event) => setPort(event.target.value)} />
            </div>
            <Button type="submit" variant="outline" disabled={disabled || !validPort || !port || Number(port) === settings.port}>Save port</Button>
          </form>
          <p className="text-xs text-muted-foreground">The port stays fixed. If another process uses it, stop that process or choose another port. Active agent routes must finish before changing port.</p>
        </div>
        <section aria-label="Local key" className="space-y-3 border-t border-border pt-4">
          <h3 className="text-sm font-medium">Local key</h3>
          <p className="text-xs text-muted-foreground">{settings.key_exists ? "Stored on this machine. Reveal or copy it only when configuring a client." : "A key is created when the gateway is first enabled."}</p>
          {key && <Input aria-label="Revealed local key" value={key} readOnly className="font-mono" />}
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" disabled={disabled || !settings.key_exists} onClick={() => key ? setKey("") : void action(async () => setKey((await revealGatewayKey()).key))}>{key ? "Hide key" : "Show key"}</Button>
            <Button variant="outline" disabled={disabled || !settings.key_exists} onClick={() => void action(async () => {
              await navigator.clipboard.writeText((await revealGatewayKey()).key); setCopied(true);
            })}>Copy key</Button>
            <Button ref={rotateButton} variant="outline" disabled={disabled || !settings.key_exists} onClick={() => setConfirming(true)}>Rotate key</Button>
          </div>
          {copied && <p role="status" className="text-xs text-muted-foreground">Key copied.</p>}
          {confirming && <div role="group" aria-label="Confirm key rotation" className="space-y-3 rounded-md border border-border p-4">
            <p className="text-sm">Rotate the local key? The previous key stops working immediately. Update every client using it.</p>
            <div className="flex flex-wrap gap-2">
              <Button disabled={busy} onClick={() => void action(async () => {
                await rotateGatewayKey(); setKey(""); setConfirming(false); apply(await fetchGatewaySettings());
              })}>Rotate and invalidate old key</Button>
              <Button ref={cancel} variant="outline" disabled={busy} onClick={() => setConfirming(false)}>Cancel</Button>
            </div>
          </div>}
        </section>
        <section aria-label="Model aliases" className="space-y-3 border-t border-border pt-4">
          <h3 className="text-sm font-medium">Model aliases</h3>
          <p className="text-xs text-muted-foreground">Clients send an alias as their model name. Targets require an enabled API key account and OpenAI Chat Completions.</p>
          {settings.aliases.map((entry) => {
            const usage = settings.alias_usage[entry.alias] ?? {};
            return <div key={entry.alias} className="space-y-2 rounded-md border border-border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 break-all font-mono text-sm">{entry.alias}</span>
                {!entry.enabled && <Badge variant="destructive">Provider disabled</Badge>}
                <Button variant="outline" className="ml-auto" disabled={disabled} aria-label={`Delete alias ${entry.alias}`}
                  onClick={() => void action(async () => apply(await deleteGatewayAlias(entry.alias)))}>Delete</Button>
              </div>
              <select className={SELECT} aria-label={`Model for ${entry.alias}`} value={entry.model_id} disabled={disabled || !registry}
                onChange={(event) => void action(async () => apply(await setGatewayAlias(entry.alias, event.target.value)))}>
                {!models.some((item) => item.model_id === entry.model_id) && <option value={entry.model_id}>{entry.provider_name} / {entry.model_name} (unavailable)</option>}
                {models.map((item) => <option key={item.model_id} value={item.model_id}>{item.provider_name} / {item.name}</option>)}
              </select>
              <p className="text-xs text-muted-foreground">{usage.requests ?? 0} requests · Cache hit {hitRate(usage)} · Input {usage.input_tokens ?? 0} · Cache read {usage.cache_read_input_tokens ?? 0} · Cache creation {usage.cache_creation_input_tokens ?? 0} · Output {usage.output_tokens ?? 0}</p>
            </div>;
          })}
          {!settings.aliases.length && <p className="text-sm text-muted-foreground">No aliases yet. Add one to route client requests.</p>}
          <form className="grid items-end gap-3 sm:grid-cols-2" onSubmit={(event) => {
            event.preventDefault();
            if (alias && model) void action(async () => { apply(await setGatewayAlias(alias, model)); setAlias(""); });
          }}>
            <div className="min-w-0 space-y-1.5"><label htmlFor={`${id}-alias`} className="text-sm font-medium">New alias</label>
              <Input id={`${id}-alias`} required maxLength={64} value={alias}
                placeholder="my-model" disabled={disabled} onChange={(event) => setAlias(event.target.value)} /></div>
            <div className="min-w-0 space-y-1.5"><label htmlFor={`${id}-model`} className="text-sm font-medium">Target model</label>
              <select id={`${id}-model`} className={SELECT} value={model} disabled={disabled || !registry} required onChange={(event) => setModel(event.target.value)}>
                <option value="">Choose a model</option>
                {models.map((item) => <option key={item.model_id} value={item.model_id}>{item.provider_name} / {item.name}</option>)}
              </select></div>
            <Button type="submit" disabled={disabled || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(alias) || !model}>Add alias</Button>
          </form>
          {registry && !models.length && <p className="text-xs text-muted-foreground">Add an OpenAI Chat Completions model with an API key in LLM Providers first.</p>}
          {registryError && <div><p role="alert" className="text-sm text-destructive">{registryError}</p><Button variant="outline" onClick={() => void reload()}>Reload models</Button></div>}
        </section>
        {env && <section aria-label="Client environment" className="min-w-0 space-y-3 border-t border-border pt-4">
          <label htmlFor={`${id}-example`} className="text-sm font-medium">Client environment</label>
          <select id={`${id}-example`} className={SELECT} value={selectedAlias} onChange={(event) => setExampleAlias(event.target.value)}>
            {settings.aliases.map((entry) => <option key={entry.alias}>{entry.alias}</option>)}
          </select>
          <p className="text-xs text-muted-foreground">Replace &lt;local-key&gt; with the copied key. Every model tier uses this alias.</p>
          <pre className="max-w-full overflow-x-auto rounded-md bg-muted p-3 text-xs"><code>{env}</code></pre>
        </section>}
        <section aria-label="Gateway counters" className="space-y-3 border-t border-border pt-4">
          <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-medium">Since proxy startup</h3>
            <Badge variant={settings.proxy.state === "running" ? "success" : "destructive"}>Proxy {settings.proxy.state}</Badge>
            <Button className="ml-auto" variant="outline" disabled={disabled} onClick={() => void action(async () => apply(await fetchGatewaySettings()))}>Refresh</Button></div>
          <p className="text-xs text-muted-foreground">All translated requests through this proxy, including Looper agents and local clients.</p>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {Object.entries({ Requests: settings.translate.requests ?? 0, "Authentication failures": settings.auth_failures,
              "Cache hit": hitRate(settings.translate), "Input tokens": settings.translate.input_tokens ?? 0,
              "Cache read tokens": settings.translate.cache_read_input_tokens ?? 0, "Cache creation tokens": settings.translate.cache_creation_input_tokens ?? 0,
              "Output tokens": settings.translate.output_tokens ?? 0, "Missing usage": settings.translate.usage_missing ?? 0,
              "Missing cache fields": settings.translate.cache_fields_missing ?? 0, "Invalid tool arguments": settings.translate.tool_args_invalid ?? 0,
              "Stream errors": settings.translate.stream_errors ?? 0, Pings: settings.translate.pings ?? 0,
              ...Object.fromEntries(Object.entries(settings.translate.errors_by_type ?? {}).map(([name, value]) => [`Error: ${name}`, value])),
              ...Object.fromEntries(Object.entries(settings.translate.dropped ?? {}).map(([name, value]) => [`Dropped: ${name}`, value])),
              ...Object.fromEntries(Object.entries(settings.translate.degraded ?? {}).map(([name, value]) => [`Degraded: ${name}`, value])),
              ...Object.fromEntries(Object.entries(settings.translate.clamped ?? {}).map(([name, value]) => [`Clamped: ${name}`, value])),
            }).map(([label, value]) => <div key={label} className="min-w-0 space-y-1"><dt className="break-words text-xs text-muted-foreground">{label}</dt><dd className="font-mono text-sm tabular-nums">{value}</dd></div>)}
          </dl>
          {!!settings.translate.cache_fields_missing && <p className="text-xs text-muted-foreground">An upstream endpoint did not report cache hits.</p>}
        </section>
      </> : !error && <p role="status" className="text-sm text-muted-foreground">Loading…</p>}
      {error && <div className="space-y-2"><p role="alert" className="break-words text-sm text-destructive">{error}</p>
        {!settings && <Button variant="outline" onClick={() => { setError(""); setAttempt((n) => n + 1); }}>Retry</Button>}</div>}
    </CardContent>
  </Card>;
}
